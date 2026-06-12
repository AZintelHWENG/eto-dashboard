"""
Schedule builder for 6th Annual Phoenix Wushu Nationals
Pure functions: parse CSV, classify rows, assign rings, build time-slotted schedule.
"""

from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

EVENT_DAYS = ["Saturday", "Sunday"]
DAY_START = "09:00"
DAY_END = "18:00"
LUNCH_START = "12:00"
LUNCH_END = "13:00"
SLOT_MINUTES = 5  # default: 5 min total per athlete (performance + transition)

# Per-ring slot overrides. Rings not listed here use SLOT_MINUTES.
RING_SLOT_MINUTES = {
    "Lion Dance Stage": 15,
}

# Rings that should split their athletes evenly across the 2 event days
# instead of filling Saturday first.
BALANCE_RINGS = {"Sanda Ring"}


def slot_for_ring(ring):
    return RING_SLOT_MINUTES.get(ring, SLOT_MINUTES)

FIXED_RINGS = {
    "Sanda": "Sanda Ring",
    "Open Martial Arts": "Open Mat",
    "Lion Dance": "Lion Dance Stage",
}

AUTO_RINGS = ["Ring 1", "Ring 2", "Ring 3", "Ring 4", "Ring 5"]
ALL_RINGS = list(FIXED_RINGS.values()) + AUTO_RINGS

NON_COMPETITION_PATTERNS = [
    "Registration Fee",
    "Spectator Admissions",
    "Merchandise",
    "Grand Champion",
    "Product Removed",
]


def load_registrations(csv_path):
    """Read registration CSV, drop non-competition rows, return clean DataFrame."""
    try:
        df = pd.read_csv(csv_path, encoding="utf-8")
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path, encoding="latin-1")
    df.columns = [c.strip() for c in df.columns]

    # The export CSV has a trailing comma in every row, so the *Status* and
    # *OrderNumber* columns are shifted right by one. The OrderDate header
    # is correct (holds the division). Remap so Status holds "Completed".
    if "Unnamed: 10" in df.columns:
        df["_RegistrationDate"] = df["Status"]
        df["Status"] = df["OrderNumber"]
        df["OrderNumber"] = df["Unnamed: 10"]

    keep = pd.Series([True] * len(df))
    for pattern in NON_COMPETITION_PATTERNS:
        keep &= ~df["Event"].astype(str).str.contains(pattern, case=False, na=False)
        keep &= ~df["OrderDate"].astype(str).str.contains(pattern, case=False, na=False)

    df = df[keep].copy()
    df = df[df["Status"].astype(str).str.strip().str.lower() == "completed"].copy()

    df["event_category"] = df["Event"].astype(str).str.strip()
    df["division"] = df["OrderDate"].astype(str).str.strip()
    df["athlete"] = df["Athlete"].astype(str).str.strip()
    df["school"] = df["School"].fillna("").astype(str).str.strip()
    df["dob"] = df["DoB"].astype(str).str.strip()
    df["age_group"] = df["dob"].apply(compute_age_group)

    df = df[df["athlete"].str.len() > 0]
    df = df[df["division"].str.len() > 0]

    df = df.reset_index(drop=True)
    df["entry_id"] = df.index
    return df[["entry_id", "athlete", "school", "dob", "age_group", "event_category", "division"]]


# Age-group banding per the tournament's rule sheet. (low, high_inclusive, label)
AGE_GROUP_BANDS = [
    (0, 6, "G1 (≤6)"),
    (7, 8, "G2 (7-8)"),
    (9, 10, "G3 (9-10)"),
    (11, 12, "G4 (11-12)"),
    (13, 14, "G5 (13-14)"),
    (15, 16, "G6 (15-16)"),
    (17, 18, "G7 (17-18)"),
    (19, 25, "G8 (19-25)"),
    (26, 30, "G9 (26-30)"),
    (31, 36, "G10 (31-36)"),
    (37, 55, "G11 (37-55)"),
    (56, 999, "G12 (56+)"),
]
# Map each age-group label to a 1..12 rank for sorting (youngest first).
_AGE_GROUP_RANK = {label: i for i, (_, _, label) in enumerate(AGE_GROUP_BANDS)}


def _parse_dob(dob_str):
    """Try the common DOB string formats. Returns a date or None."""
    s = (dob_str or "").strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    # Pandas fallback handles a wide variety of inputs.
    try:
        ts = pd.to_datetime(s, errors="coerce")
        if pd.notna(ts):
            return ts.date()
    except (ValueError, TypeError):
        pass
    return None


def compute_age_group(dob_str, ref_date=None):
    """Return the G1..G12 label for an athlete's date of birth.
    ref_date defaults to today; pass an explicit date to anchor on the event."""
    if ref_date is None:
        ref_date = datetime.now().date()
    dob = _parse_dob(dob_str)
    if dob is None:
        return ""
    age = ref_date.year - dob.year - ((ref_date.month, ref_date.day) < (dob.month, dob.day))
    if age < 0:
        return ""
    for lo, hi, label in AGE_GROUP_BANDS:
        if lo <= age <= hi:
            return label
    return ""


def classify_ring(event_category):
    """Map an event category string to a fixed ring, or None for auto-assignment."""
    cat = (event_category or "").lower()
    if "sanda event" in cat or cat.startswith("sanda"):
        return "Sanda Ring"
    if "open martial arts" in cat:
        return "Open Mat"
    if "lion dance" in cat:
        return "Lion Dance Stage"
    return None


def _ring_capacity(slot_minutes=None, ring=None):
    """How many athletes one ring can hold across both event days."""
    if slot_minutes is None:
        slot_minutes = slot_for_ring(ring) if ring else SLOT_MINUTES
    day_start_min = _time_to_minutes(DAY_START)
    day_end_min = _time_to_minutes(DAY_END)
    lunch_minutes = _time_to_minutes(LUNCH_END) - _time_to_minutes(LUNCH_START)
    minutes_per_day = (day_end_min - day_start_min) - lunch_minutes
    return (minutes_per_day // slot_minutes) * len(EVENT_DAYS)


def auto_assign_rings(df, allow_division_split=True):
    """
    Assign event categories to Ring 4-8 using bin-packing on athlete count.

    When a single category exceeds one ring's 2-day capacity, split it at the
    division boundary across multiple rings (keeping each division contiguous
    on one ring whenever possible). If a single division still exceeds one
    ring's capacity, split the division itself across rings.
    """
    df = df.copy()
    df["ring"] = df["event_category"].apply(classify_ring)

    unassigned_mask = df["ring"].isna()
    unassigned = df[unassigned_mask].copy()
    if unassigned.empty:
        return df

    cap = _ring_capacity()
    ring_loads = {ring: 0 for ring in AUTO_RINGS}

    # Build (category, division, count) triples sorted by category size desc.
    cat_sizes = unassigned.groupby("event_category").size().sort_values(ascending=False)

    # Map each entry_id to a ring.
    entry_to_ring = {}

    for cat in cat_sizes.index:
        cat_rows = unassigned[unassigned["event_category"] == cat]
        # Sort divisions within the category by size (large first) so big
        # divisions get placed before small ones.
        div_groups = cat_rows.groupby("division", sort=False)
        div_list = sorted(div_groups, key=lambda kv: -len(kv[1]))

        for div, div_rows in div_list:
            div_size = len(div_rows)
            target = min(ring_loads, key=ring_loads.get)
            remaining = cap - ring_loads[target]

            if div_size <= remaining or not allow_division_split:
                # Place whole division on the least-loaded ring (may exceed
                # cap if allow_division_split is False — overflow handled
                # later by build_schedule).
                for eid in div_rows["entry_id"]:
                    entry_to_ring[eid] = target
                ring_loads[target] += div_size
            else:
                # Split this division across rings, filling least-loaded
                # rings first until all athletes placed.
                ids = list(div_rows["entry_id"])
                while ids:
                    target = min(ring_loads, key=ring_loads.get)
                    free = cap - ring_loads[target]
                    if free <= 0:
                        # All rings full — dump remainder into least-loaded
                        # ring; build_schedule will mark them OVERFLOW.
                        for eid in ids:
                            entry_to_ring[eid] = target
                        ring_loads[target] += len(ids)
                        ids = []
                        break
                    chunk = ids[:free]
                    for eid in chunk:
                        entry_to_ring[eid] = target
                    ring_loads[target] += len(chunk)
                    ids = ids[free:]

    df.loc[unassigned_mask, "ring"] = df.loc[unassigned_mask, "entry_id"].map(entry_to_ring)
    return df


def _time_to_minutes(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _minutes_to_time(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _split_point_for_balance(n_athletes, ring_slot):
    """
    Return how many athletes belong on day 1 (Saturday) when balancing across
    both event days. Ceiling-split so day 1 gets the larger half if odd, but
    cap at the per-day capacity.
    """
    day_start_min = _time_to_minutes(DAY_START)
    day_end_min = _time_to_minutes(DAY_END)
    lunch_minutes = _time_to_minutes(LUNCH_END) - _time_to_minutes(LUNCH_START)
    minutes_per_day = (day_end_min - day_start_min) - lunch_minutes
    per_day_capacity = minutes_per_day // ring_slot

    half = (n_athletes + 1) // 2
    return min(half, per_day_capacity)


def build_schedule(df, slot_minutes=None):
    """
    Walk each ring's queue, assign Day + start time to every athlete.
    Skip 12:00-13:00 lunch hour. Spread across both days if needed.
    Per-ring slot overrides come from slot_for_ring() (e.g. Lion Dance = 15 min).
    Rings in BALANCE_RINGS split their athletes evenly across both days.
    """
    df = df.copy()
    if "ring" not in df.columns:
        df = auto_assign_rings(df)

    day_start_min = _time_to_minutes(DAY_START)
    day_end_min = _time_to_minutes(DAY_END)
    lunch_start_min = _time_to_minutes(LUNCH_START)
    lunch_end_min = _time_to_minutes(LUNCH_END)

    rows = []
    for ring in ALL_RINGS:
        ring_df = df[df["ring"] == ring].copy()
        if ring_df.empty:
            continue

        ring_slot = slot_minutes if slot_minutes is not None else slot_for_ring(ring)

        ring_df["__div_letter"] = ring_df["division"].astype(str).str.strip().str[:1].str.upper()
        # Within a single division, group athletes by age band (youngest first)
        # so heats run G1 → G12. Athletes without a parseable DOB sort last
        # (rank 999) so they don't disrupt the age ordering.
        if "age_group" not in ring_df.columns:
            ring_df["age_group"] = ring_df.get("dob", "").apply(compute_age_group)
        ring_df["__age_rank"] = ring_df["age_group"].map(_AGE_GROUP_RANK).fillna(999).astype(int)
        ring_df = ring_df.sort_values(
            ["__div_letter", "division", "__age_rank", "athlete"]
        ).drop(columns=["__div_letter", "__age_rank"]).reset_index(drop=True)

        balance = ring in BALANCE_RINGS
        split_at = _split_point_for_balance(len(ring_df), ring_slot) if balance else None

        day_idx = 0
        cursor = day_start_min
        for i, (_, row) in enumerate(ring_df.iterrows()):
            # Force a day change at the split point for balancing rings.
            if balance and i == split_at and day_idx == 0:
                day_idx = 1
                cursor = day_start_min

            placed = False
            while day_idx < len(EVENT_DAYS):
                if cursor < lunch_end_min and cursor + ring_slot > lunch_start_min:
                    cursor = lunch_end_min
                if cursor + ring_slot > day_end_min:
                    day_idx += 1
                    cursor = day_start_min
                    continue
                rows.append({
                    **row.to_dict(),
                    "day": EVENT_DAYS[day_idx],
                    "start_time": _minutes_to_time(cursor),
                    "end_time": _minutes_to_time(cursor + ring_slot),
                    "order_in_ring": len(rows),
                })
                cursor += ring_slot
                placed = True
                break
            if not placed:
                rows.append({
                    **row.to_dict(),
                    "day": "OVERFLOW",
                    "start_time": "--:--",
                    "end_time": "--:--",
                    "order_in_ring": len(rows),
                })

    schedule = pd.DataFrame(rows)
    return schedule


def renumber_ring(schedule, ring, slot_minutes=None):
    """Recompute day + start_time for every athlete in one ring after a reorder."""
    schedule = schedule.copy()
    ring_mask = schedule["ring"] == ring
    ring_df = schedule[ring_mask].sort_values("order_in_ring").reset_index()

    if slot_minutes is None:
        slot_minutes = slot_for_ring(ring)

    day_start_min = _time_to_minutes(DAY_START)
    day_end_min = _time_to_minutes(DAY_END)
    lunch_start_min = _time_to_minutes(LUNCH_START)
    lunch_end_min = _time_to_minutes(LUNCH_END)

    balance = ring in BALANCE_RINGS
    split_at = _split_point_for_balance(len(ring_df), slot_minutes) if balance else None

    day_idx = 0
    cursor = day_start_min
    for i, (_, row) in enumerate(ring_df.iterrows()):
        orig_idx = row["index"]

        if balance and i == split_at and day_idx == 0:
            day_idx = 1
            cursor = day_start_min

        placed = False
        while day_idx < len(EVENT_DAYS):
            if cursor < lunch_end_min and cursor + slot_minutes > lunch_start_min:
                cursor = lunch_end_min
            if cursor + slot_minutes > day_end_min:
                day_idx += 1
                cursor = day_start_min
                continue
            schedule.at[orig_idx, "day"] = EVENT_DAYS[day_idx]
            schedule.at[orig_idx, "start_time"] = _minutes_to_time(cursor)
            schedule.at[orig_idx, "end_time"] = _minutes_to_time(cursor + slot_minutes)
            cursor += slot_minutes
            placed = True
            break
        if not placed:
            schedule.at[orig_idx, "day"] = "OVERFLOW"
            schedule.at[orig_idx, "start_time"] = "--:--"
            schedule.at[orig_idx, "end_time"] = "--:--"

    return schedule


def reorder_ring(schedule, ring, new_entry_id_order):
    """
    Apply a new ordering of athletes to a ring (e.g. from drag-and-drop).
    new_entry_id_order is a list of entry_id values in the desired order.
    Recomputes time slots after the reorder.
    """
    schedule = schedule.copy()
    ring_mask = schedule["ring"] == ring
    base_orders = sorted(schedule.loc[ring_mask, "order_in_ring"].tolist())

    eid_to_new_order = {eid: base_orders[i] for i, eid in enumerate(new_entry_id_order)}
    for eid, new_order in eid_to_new_order.items():
        schedule.loc[(schedule["ring"] == ring) & (schedule["entry_id"] == eid), "order_in_ring"] = new_order

    return renumber_ring(schedule, ring)


def reorder_divisions(schedule, ring, new_division_order):
    """Apply a new division ordering on a ring (drag-and-drop division blocks)."""
    schedule = schedule.copy()
    ring_mask = schedule["ring"] == ring

    div_to_rank = {div: i for i, div in enumerate(new_division_order)}

    ring_view = schedule.loc[ring_mask].copy()
    ring_view["__div_rank"] = ring_view["division"].map(div_to_rank)
    ring_view = ring_view.sort_values(["__div_rank", "order_in_ring"])

    base_orders = sorted(schedule.loc[ring_mask, "order_in_ring"].tolist())
    new_orders = dict(zip(ring_view.index.tolist(), base_orders))

    for orig_idx, new_order in new_orders.items():
        schedule.at[orig_idx, "order_in_ring"] = new_order

    return renumber_ring(schedule, ring)


def move_athlete(schedule, ring, current_order, direction):
    """Swap an athlete with the one above (direction=-1) or below (direction=+1) in the same ring."""
    schedule = schedule.copy()
    ring_mask = schedule["ring"] == ring
    ring_orders = sorted(schedule.loc[ring_mask, "order_in_ring"].tolist())

    if current_order not in ring_orders:
        return schedule
    pos = ring_orders.index(current_order)
    new_pos = pos + direction
    if new_pos < 0 or new_pos >= len(ring_orders):
        return schedule

    other_order = ring_orders[new_pos]

    schedule.loc[(schedule["ring"] == ring) & (schedule["order_in_ring"] == current_order), "order_in_ring"] = -1
    schedule.loc[(schedule["ring"] == ring) & (schedule["order_in_ring"] == other_order), "order_in_ring"] = current_order
    schedule.loc[(schedule["ring"] == ring) & (schedule["order_in_ring"] == -1), "order_in_ring"] = other_order

    return renumber_ring(schedule, ring)


def move_division(schedule, ring, division, direction):
    """Move an entire division block up (-1) or down (+1) past the adjacent division on this ring."""
    schedule = schedule.copy()
    ring_mask = schedule["ring"] == ring
    ring_df = schedule[ring_mask].sort_values("order_in_ring")

    divisions_in_order = []
    seen = set()
    for div in ring_df["division"]:
        if div not in seen:
            divisions_in_order.append(div)
            seen.add(div)

    if division not in divisions_in_order:
        return schedule
    pos = divisions_in_order.index(division)
    new_pos = pos + direction
    if new_pos < 0 or new_pos >= len(divisions_in_order):
        return schedule

    other = divisions_in_order[new_pos]
    new_div_order = divisions_in_order.copy()
    new_div_order[pos], new_div_order[new_pos] = new_div_order[new_pos], new_div_order[pos]

    div_to_rank = {div: i for i, div in enumerate(new_div_order)}

    ring_df = ring_df.copy()
    ring_df["__div_rank"] = ring_df["division"].map(div_to_rank)
    ring_df = ring_df.sort_values(["__div_rank", "order_in_ring"]).reset_index()

    base_orders = sorted(schedule.loc[ring_mask, "order_in_ring"].tolist())
    for new_order, row in zip(base_orders, ring_df.itertuples()):
        schedule.at[row.index, "order_in_ring"] = new_order

    return renumber_ring(schedule, ring)


def move_division_to_ring(schedule, division, source_ring, dest_ring, position="end"):
    """
    Move every athlete in `division` from source_ring to dest_ring as a block.

    position:
        "end"   - append the block to the end of dest_ring (default)
        "start" - prepend the block to the start of dest_ring
        int     - insert the block at this 0-based division index in dest_ring

    Both rings are renumbered (time slots recomputed) after the move.
    """
    if source_ring == dest_ring:
        return schedule

    schedule = schedule.copy()
    block_mask = (schedule["ring"] == source_ring) & (schedule["division"] == division)
    if not block_mask.any():
        return schedule

    # Reassign ring on the moving block.
    schedule.loc[block_mask, "ring"] = dest_ring

    # Decide where in dest_ring the block lands.
    dest_mask = (schedule["ring"] == dest_ring) & ~block_mask & (schedule["entry_id"].isin(schedule.loc[block_mask, "entry_id"]))
    # Build current dest ring order (excluding the just-moved block).
    dest_ring_df = schedule[schedule["ring"] == dest_ring].copy()
    moved_eids = set(schedule.loc[schedule["division"].eq(division) & schedule["ring"].eq(dest_ring), "entry_id"])
    existing = dest_ring_df[~dest_ring_df["entry_id"].isin(moved_eids)].sort_values("order_in_ring")
    moved = dest_ring_df[dest_ring_df["entry_id"].isin(moved_eids)].sort_values("order_in_ring")

    # Build the new ordered list of (existing divisions in original order) and decide where to splice.
    existing_divs = []
    seen = set()
    for d in existing["division"]:
        if d not in seen:
            existing_divs.append(d)
            seen.add(d)

    if position == "start":
        insert_idx = 0
    elif position == "end" or position is None:
        insert_idx = len(existing_divs)
    else:
        insert_idx = max(0, min(int(position), len(existing_divs)))

    pre_divs = existing_divs[:insert_idx]
    post_divs = existing_divs[insert_idx:]

    pre_df = existing[existing["division"].isin(pre_divs)].sort_values("order_in_ring")
    post_df = existing[existing["division"].isin(post_divs)].sort_values("order_in_ring")

    new_dest_order = pd.concat([pre_df, moved, post_df]).reset_index(drop=True)
    new_dest_order["__new_order"] = range(len(new_dest_order))

    # Apply new order_in_ring values to dest ring.
    base = schedule.loc[schedule["ring"] == dest_ring].index.tolist()
    sorted_base = sorted(schedule.loc[schedule["ring"] == dest_ring, "order_in_ring"].tolist())
    eid_to_new = dict(zip(new_dest_order["entry_id"], sorted_base))
    schedule.loc[schedule["ring"] == dest_ring, "order_in_ring"] = (
        schedule.loc[schedule["ring"] == dest_ring, "entry_id"].map(eid_to_new).astype(int)
    )

    schedule = renumber_ring(schedule, source_ring)
    schedule = renumber_ring(schedule, dest_ring)
    return schedule


def add_athlete(schedule, athlete, school, division, ring=None, event_category=None, dob=""):
    """
    Add a new athlete to the schedule, placed at the end of the matching
    division block on the appropriate ring. Times are recomputed for that
    ring after insertion.

    If `ring` is None, the ring is inferred from any existing entries in
    the same division. If the division does not exist yet, the ring must
    be provided. The new athlete is inserted immediately after the last
    existing athlete in that division (so they sit at the end of the
    division's block, before the next division starts).
    """
    schedule = schedule.copy()

    # Locate existing rows in this division.
    div_rows = schedule[schedule["division"] == division]

    if ring is None:
        if div_rows.empty:
            raise ValueError(
                f"Division {division!r} not found in schedule. "
                "You must specify ring= when adding an athlete to a brand-new division."
            )
        # All entries in a division should be on the same ring; pick the most common.
        ring = div_rows["ring"].mode().iloc[0]

    if event_category is None:
        if not div_rows.empty:
            event_category = div_rows["event_category"].mode().iloc[0]
        else:
            event_category = "Manual Entry"

    new_entry_id = int(schedule["entry_id"].max()) + 1 if len(schedule) else 0

    # Find the position to insert (just after the last existing athlete in this division on this ring).
    on_ring = schedule[schedule["ring"] == ring].sort_values("order_in_ring")
    if not on_ring.empty:
        match = on_ring[on_ring["division"] == division]
        if not match.empty:
            insert_after_order = int(match["order_in_ring"].max())
        else:
            # Brand-new division on this ring — append after everything.
            insert_after_order = int(on_ring["order_in_ring"].max())
    else:
        insert_after_order = -1

    # Bump order_in_ring for every entry on this ring that comes after the insert point.
    bump_mask = (schedule["ring"] == ring) & (schedule["order_in_ring"] > insert_after_order)
    schedule.loc[bump_mask, "order_in_ring"] = schedule.loc[bump_mask, "order_in_ring"] + 1

    dob_str = (dob or "").strip()
    new_row = {
        "entry_id": new_entry_id,
        "athlete": athlete.strip(),
        "school": (school or "").strip(),
        "dob": dob_str,
        "age_group": compute_age_group(dob_str) if dob_str else "",
        "event_category": event_category,
        "division": division,
        "ring": ring,
        "order_in_ring": insert_after_order + 1,
        "day": "Saturday",        # placeholder, renumber_ring will overwrite
        "start_time": "--:--",
        "end_time": "--:--",
    }
    schedule = pd.concat([schedule, pd.DataFrame([new_row])], ignore_index=True)
    return renumber_ring(schedule, ring)


def detect_conflicts(schedule):
    """Find athletes scheduled at overlapping times in different rings."""
    valid = schedule[schedule["day"].isin(EVENT_DAYS)].copy()
    if valid.empty:
        return pd.DataFrame(columns=["athlete", "day", "ring_a", "time_a", "ring_b", "time_b"])

    valid["start_min"] = valid["start_time"].apply(_time_to_minutes)
    valid["end_min"] = valid["end_time"].apply(_time_to_minutes)

    conflicts = []
    for athlete, group in valid.groupby("athlete"):
        if len(group) < 2:
            continue
        rows = group.to_dict("records")
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                a, b = rows[i], rows[j]
                if a["day"] != b["day"]:
                    continue
                if a["start_min"] < b["end_min"] and b["start_min"] < a["end_min"]:
                    conflicts.append({
                        "athlete": athlete,
                        "day": a["day"],
                        "ring_a": a["ring"],
                        "time_a": f"{a['start_time']}-{a['end_time']}",
                        "event_a": f"{a['event_category']} / {a['division']}",
                        "ring_b": b["ring"],
                        "time_b": f"{b['start_time']}-{b['end_time']}",
                        "event_b": f"{b['event_category']} / {b['division']}",
                    })

    return pd.DataFrame(conflicts)


def schedule_to_dict(schedule):
    """Serialize for JSON persistence."""
    return schedule.to_dict(orient="records")


def schedule_from_dict(records):
    """Deserialize from JSON."""
    return pd.DataFrame(records)


def division_colors(divisions_in_order):
    """
    Assign a light pastel hex color to each division so adjacent divisions are
    always visually distinct. Cycles through a palette of distinct light hues
    (green / blue / red / yellow / purple / orange / teal / pink), shifting
    by 3 positions on each rotation so even consecutive cycles look different.
    All colors are light enough that black text reads well.
    """
    n = len(divisions_in_order)
    if n == 0:
        return {}

    palette = [
        "#C8F0C8",  # light green
        "#C8DCF5",  # light blue
        "#F5CDCD",  # light red / pink
        "#FFF1B8",  # light yellow
        "#E0CDF0",  # light purple
        "#FFD9B8",  # light orange
        "#C8EFE8",  # light teal
        "#F4C8DC",  # light rose
    ]

    out = {}
    for i, div in enumerate(divisions_in_order):
        # Walk by 3 each step so adjacent indices land far apart in the palette
        # while still cycling through every color before repeating.
        idx = (i * 3) % len(palette)
        out[div] = palette[idx]
    return out

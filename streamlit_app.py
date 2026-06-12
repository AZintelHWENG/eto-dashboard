"""
6th Annual Phoenix Wushu Nationals - Schedule Dashboard
Single-file Streamlit app for deployment to Streamlit Community Cloud.

Repo layout for deployment:
    streamlit_app.py        <- this file
    registrations.csv       <- registration export (must be in the same folder)
    requirements.txt        <- streamlit, pandas, streamlit-sortables

Optional:
    schedule_state.json     <- saved schedule (created on first save).
                               Include if you want a deployed instance to start
                               from a pre-edited state.
"""

import json
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_sortables import sort_items

# =============================================================================
# Schedule builder (was schedule_builder.py)
# =============================================================================

EVENT_DAYS = ["Saturday", "Sunday"]
DAY_START = "09:00"
DAY_END = "18:00"
LUNCH_START = "12:00"
LUNCH_END = "13:00"
SLOT_MINUTES = 5

# Per-ring slot overrides. Rings not listed use SLOT_MINUTES.
RING_SLOT_MINUTES = {
    "Lion Dance Stage": 15,
}

# Rings that should split their athletes evenly across the 2 event days
# instead of filling Saturday first.
BALANCE_RINGS = {"Sanda Ring"}


def slot_for_ring(ring):
    return RING_SLOT_MINUTES.get(ring, SLOT_MINUTES)


def _split_point_for_balance(n_athletes, ring_slot):
    day_start_min = _time_to_minutes(DAY_START)
    day_end_min = _time_to_minutes(DAY_END)
    lunch_minutes = _time_to_minutes(LUNCH_END) - _time_to_minutes(LUNCH_START)
    minutes_per_day = (day_end_min - day_start_min) - lunch_minutes
    per_day_capacity = minutes_per_day // ring_slot
    half = (n_athletes + 1) // 2
    return min(half, per_day_capacity)


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


def _time_to_minutes(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _minutes_to_time(m):
    return f"{m // 60:02d}:{m % 60:02d}"


def _ring_capacity(slot_minutes=None, ring=None):
    if slot_minutes is None:
        slot_minutes = slot_for_ring(ring) if ring else SLOT_MINUTES
    day_start_min = _time_to_minutes(DAY_START)
    day_end_min = _time_to_minutes(DAY_END)
    lunch_minutes = _time_to_minutes(LUNCH_END) - _time_to_minutes(LUNCH_START)
    minutes_per_day = (day_end_min - day_start_min) - lunch_minutes
    return (minutes_per_day // slot_minutes) * len(EVENT_DAYS)


def load_registrations(csv_path):
    try:
        df = pd.read_csv(csv_path, encoding="utf-8")
    except UnicodeDecodeError:
        df = pd.read_csv(csv_path, encoding="latin-1")
    df.columns = [c.strip() for c in df.columns]

    # Export CSV has a trailing comma so Status/OrderNumber are shifted right.
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

    df = df[df["athlete"].str.len() > 0]
    df = df[df["division"].str.len() > 0]
    df = df.reset_index(drop=True)
    df["entry_id"] = df.index
    return df[["entry_id", "athlete", "school", "dob", "event_category", "division"]]


def classify_ring(event_category):
    cat = (event_category or "").lower()
    if "sanda event" in cat or cat.startswith("sanda"):
        return "Sanda Ring"
    if "open martial arts" in cat:
        return "Open Mat"
    if "lion dance" in cat:
        return "Lion Dance Stage"
    return None


def auto_assign_rings(df, allow_division_split=True):
    df = df.copy()
    df["ring"] = df["event_category"].apply(classify_ring)

    unassigned_mask = df["ring"].isna()
    unassigned = df[unassigned_mask].copy()
    if unassigned.empty:
        return df

    cap = _ring_capacity()
    ring_loads = {ring: 0 for ring in AUTO_RINGS}
    cat_sizes = unassigned.groupby("event_category").size().sort_values(ascending=False)
    entry_to_ring = {}

    for cat in cat_sizes.index:
        cat_rows = unassigned[unassigned["event_category"] == cat]
        div_groups = cat_rows.groupby("division", sort=False)
        div_list = sorted(div_groups, key=lambda kv: -len(kv[1]))

        for div, div_rows in div_list:
            div_size = len(div_rows)
            target = min(ring_loads, key=ring_loads.get)
            remaining = cap - ring_loads[target]

            if div_size <= remaining or not allow_division_split:
                for eid in div_rows["entry_id"]:
                    entry_to_ring[eid] = target
                ring_loads[target] += div_size
            else:
                ids = list(div_rows["entry_id"])
                while ids:
                    target = min(ring_loads, key=ring_loads.get)
                    free = cap - ring_loads[target]
                    if free <= 0:
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


def build_schedule(df, slot_minutes=None):
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
        ring_df = ring_df.sort_values(
            ["__div_letter", "division", "athlete"]
        ).drop(columns="__div_letter").reset_index(drop=True)

        balance = ring in BALANCE_RINGS
        split_at = _split_point_for_balance(len(ring_df), ring_slot) if balance else None

        day_idx = 0
        cursor = day_start_min
        for i, (_, row) in enumerate(ring_df.iterrows()):
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
    return pd.DataFrame(rows)


def renumber_ring(schedule, ring, slot_minutes=None):
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
    schedule = schedule.copy()
    ring_mask = schedule["ring"] == ring
    base_orders = sorted(schedule.loc[ring_mask, "order_in_ring"].tolist())
    eid_to_new_order = {eid: base_orders[i] for i, eid in enumerate(new_entry_id_order)}
    for eid, new_order in eid_to_new_order.items():
        schedule.loc[(schedule["ring"] == ring) & (schedule["entry_id"] == eid), "order_in_ring"] = new_order
    return renumber_ring(schedule, ring)


def reorder_divisions(schedule, ring, new_division_order):
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


def move_division_to_ring(schedule, division, source_ring, dest_ring, position="end"):
    if source_ring == dest_ring:
        return schedule

    schedule = schedule.copy()
    block_mask = (schedule["ring"] == source_ring) & (schedule["division"] == division)
    if not block_mask.any():
        return schedule

    schedule.loc[block_mask, "ring"] = dest_ring

    dest_ring_df = schedule[schedule["ring"] == dest_ring].copy()
    moved_eids = set(schedule.loc[schedule["division"].eq(division) & schedule["ring"].eq(dest_ring), "entry_id"])
    existing = dest_ring_df[~dest_ring_df["entry_id"].isin(moved_eids)].sort_values("order_in_ring")
    moved = dest_ring_df[dest_ring_df["entry_id"].isin(moved_eids)].sort_values("order_in_ring")

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
    sorted_base = sorted(schedule.loc[schedule["ring"] == dest_ring, "order_in_ring"].tolist())
    eid_to_new = dict(zip(new_dest_order["entry_id"], sorted_base))
    schedule.loc[schedule["ring"] == dest_ring, "order_in_ring"] = (
        schedule.loc[schedule["ring"] == dest_ring, "entry_id"].map(eid_to_new).astype(int)
    )

    schedule = renumber_ring(schedule, source_ring)
    schedule = renumber_ring(schedule, dest_ring)
    return schedule


def add_athlete(schedule, athlete, school, division, ring=None, event_category=None):
    schedule = schedule.copy()
    div_rows = schedule[schedule["division"] == division]

    if ring is None:
        if div_rows.empty:
            raise ValueError(
                f"Division {division!r} not found in schedule. "
                "You must specify ring= when adding an athlete to a brand-new division."
            )
        ring = div_rows["ring"].mode().iloc[0]

    if event_category is None:
        if not div_rows.empty:
            event_category = div_rows["event_category"].mode().iloc[0]
        else:
            event_category = "Manual Entry"

    new_entry_id = int(schedule["entry_id"].max()) + 1 if len(schedule) else 0

    on_ring = schedule[schedule["ring"] == ring].sort_values("order_in_ring")
    if not on_ring.empty:
        match = on_ring[on_ring["division"] == division]
        if not match.empty:
            insert_after_order = int(match["order_in_ring"].max())
        else:
            insert_after_order = int(on_ring["order_in_ring"].max())
    else:
        insert_after_order = -1

    bump_mask = (schedule["ring"] == ring) & (schedule["order_in_ring"] > insert_after_order)
    schedule.loc[bump_mask, "order_in_ring"] = schedule.loc[bump_mask, "order_in_ring"] + 1

    new_row = {
        "entry_id": new_entry_id,
        "athlete": athlete.strip(),
        "school": (school or "").strip(),
        "dob": "",
        "event_category": event_category,
        "division": division,
        "ring": ring,
        "order_in_ring": insert_after_order + 1,
        "day": "Saturday",
        "start_time": "--:--",
        "end_time": "--:--",
    }
    schedule = pd.concat([schedule, pd.DataFrame([new_row])], ignore_index=True)
    return renumber_ring(schedule, ring)


def detect_conflicts(schedule):
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
    return schedule.to_dict(orient="records")


def schedule_from_dict(records):
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
        idx = (i * 3) % len(palette)
        out[div] = palette[idx]
    return out


# =============================================================================
# Streamlit dashboard
# =============================================================================

APP_DIR = Path(__file__).parent
CSV_PATH = APP_DIR / "registrations.csv"
STATE_PATH = APP_DIR / "schedule_state.json"

st.set_page_config(
    page_title="Terry's Event Schedule",
    page_icon="🥋",
    layout="wide",
)


def load_or_build_schedule():
    if STATE_PATH.exists():
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            records = json.load(f)
        return schedule_from_dict(records)
    df = load_registrations(str(CSV_PATH))
    df = auto_assign_rings(df)
    return build_schedule(df)


def save_schedule(schedule):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(schedule_to_dict(schedule), f, indent=2)


def rebuild_schedule():
    df = load_registrations(str(CSV_PATH))
    df = auto_assign_rings(df)
    schedule = build_schedule(df)
    save_schedule(schedule)
    return schedule


if "schedule" not in st.session_state:
    st.session_state.schedule = load_or_build_schedule()

# Ensure the schedule carries a "score" column.
if "score" not in st.session_state.schedule.columns:
    st.session_state.schedule["score"] = ""
else:
    st.session_state.schedule["score"] = st.session_state.schedule["score"].fillna("")

# Promote pending widget values that were staged on a previous run.
for src, dst in [
    ("pending_view_rings", "view_rings"),
    ("pending_view_day", "view_day"),
    ("pending_ath_ring", "ath_ring"),
    ("pending_div_ring", "div_ring"),
]:
    if src in st.session_state:
        st.session_state[dst] = st.session_state.pop(src)

schedule = st.session_state.schedule


# ----- Ring button styling -----
RING_ICONS = {
    "Sanda Ring": "🥊",
    "Open Mat": "✊",
    "Lion Dance Stage": "🐉",
    "Ring 1": "🔴",
    "Ring 2": "🟠",
    "Ring 3": "🟡",
    "Ring 4": "🟢",
    "Ring 5": "🔵",
}

RING_BORDER_COLORS = {
    "Ring 1": "#E74C3C",
    "Ring 2": "#E67E22",
    "Ring 3": "#F1C40F",
    "Ring 4": "#27AE60",
    "Ring 5": "#3498DB",
}


def _inject_ring_button_styles():
    if st.session_state.get("_ring_button_css_done"):
        return
    css_rules = [
        '[class*="st-key-ringbtn-"] button {'
        '  border-width: 3px !important;'
        '  border-radius: 14px !important;'
        '  font-weight: 600 !important;'
        '  padding: 0.5rem 0.4rem !important;'
        '  min-height: 3.2rem !important;'
        '}',
    ]
    for ring, color in RING_BORDER_COLORS.items():
        slug = ring.replace(" ", "_")
        css_rules.append(
            f'[class*="st-key-ringbtn-{slug}"] button {{'
            f'  border-color: {color} !important;'
            f'}}'
        )
        css_rules.append(
            f'[class*="st-key-ringbtn-{slug}"] button:hover {{'
            f'  background-color: {color}22 !important;'
            f'  border-color: {color} !important;'
            f'  color: inherit !important;'
            f'}}'
        )
    st.markdown(f"<style>\n{chr(10).join(css_rules)}\n</style>", unsafe_allow_html=True)
    st.session_state._ring_button_css_done = True


def _label_for_ring(ring):
    icon = RING_ICONS.get(ring, "")
    return f"{icon}  {ring}".strip()


def ring_jump_buttons(state_key, prefix):
    _inject_ring_button_styles()
    cols = st.columns(len(ALL_RINGS))
    for col, ring in zip(cols, ALL_RINGS):
        with col:
            slug = ring.replace(" ", "_")
            btn_key = f"ringbtn-{slug}-{prefix}"
            if st.button(_label_for_ring(ring), key=btn_key, width="stretch"):
                st.session_state[state_key] = ring
                st.rerun()


def _clock_face_svg(time_str, label, ring_color="#444"):
    """Render an analog clock SVG for the given HH:MM time. label appears below."""
    import math
    if not time_str or time_str == "--:--":
        hour, minute = 0, 0
        display = "—"
    else:
        try:
            hh, mm = time_str.split(":")
            hour = int(hh) % 12
            minute = int(mm)
            display = time_str
        except (ValueError, AttributeError):
            hour, minute = 0, 0
            display = time_str

    minute_angle = (minute / 60.0) * 360
    hour_angle = ((hour + minute / 60.0) / 12.0) * 360
    cx, cy, r = 50, 50, 42

    def hand_xy(angle_deg, length):
        rad = math.radians(angle_deg - 90)
        return cx + length * math.cos(rad), cy + length * math.sin(rad)

    mx, my = hand_xy(minute_angle, 32)
    hx, hy = hand_xy(hour_angle, 22)

    ticks_svg = ""
    for i in range(12):
        rad = math.radians(i * 30 - 90)
        x1 = cx + (r - 3) * math.cos(rad)
        y1 = cy + (r - 3) * math.sin(rad)
        x2 = cx + r * math.cos(rad)
        y2 = cy + r * math.sin(rad)
        ticks_svg += f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="#666" stroke-width="1.5"/>'

    svg = f'''
    <div style="display:flex;flex-direction:column;align-items:center;gap:4px;">
      <svg width="100" height="100" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
        <circle cx="{cx}" cy="{cy}" r="{r}" fill="#fafafa" stroke="{ring_color}" stroke-width="3"/>
        {ticks_svg}
        <line x1="{cx}" y1="{cy}" x2="{hx:.1f}" y2="{hy:.1f}" stroke="#222" stroke-width="3.5" stroke-linecap="round"/>
        <line x1="{cx}" y1="{cy}" x2="{mx:.1f}" y2="{my:.1f}" stroke="#222" stroke-width="2.2" stroke-linecap="round"/>
        <circle cx="{cx}" cy="{cy}" r="2.5" fill="#222"/>
      </svg>
      <div style="font-size:14px;font-weight:700;color:#222;">{display}</div>
      <div style="font-size:11px;color:#555;text-align:center;line-height:1.2;">{label}</div>
    </div>
    '''
    return svg


def _hhmm_to_min(t):
    try:
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _min_to_hhmm(m):
    return f"{int(m // 60) % 24:02d}:{int(m % 60):02d}"


def _live_eta_for_ring(ring, day, schedule):
    ring_rows = schedule[(schedule["ring"] == ring) & (schedule["day"] == day)]
    if ring_rows.empty:
        return ("--:--", None, "--:--")
    sched_end = ring_rows["end_time"].max()
    sched_end_min = _hhmm_to_min(sched_end)

    sim_state = st.session_state.get("sim_ring_state") or {}
    sim_wall_start = st.session_state.get("sim_wall_start")
    if sim_wall_start is None or ring not in sim_state:
        return (sched_end, None, sched_end)

    import time as _time
    sim_running = st.session_state.get("sim_running", False)
    sim_speed = st.session_state.get("sim_speed", 1)
    paused_offset = st.session_state.get("sim_paused_offset", 0)
    if sim_running:
        elapsed_real = _time.time() - sim_wall_start
        sim_seconds = paused_offset + elapsed_real * sim_speed
    else:
        sim_seconds = paused_offset
    total_min = sim_seconds / 60.0
    SIM_BASE = 9 * 60
    if total_min < 24 * 60:
        sim_day = "Saturday"
        sim_now_min = SIM_BASE + total_min
    else:
        sim_day = "Sunday"
        sim_now_min = SIM_BASE + (total_min - 24 * 60)

    if day != sim_day:
        return (sched_end, None, sched_end)

    rs = sim_state[ring]
    cur_idx = rs.get("current_idx", 0)
    started_at = rs.get("current_started_min")
    pending = rs.get("pending_score")
    queue = ring_rows.sort_values("order_in_ring").reset_index(drop=True)
    if cur_idx >= len(queue):
        return (_min_to_hhmm(sim_now_min), None, sched_end)

    ring_slot = slot_for_ring(ring)
    remaining_after_current = max(0, len(queue) - cur_idx - 1)

    if pending is not None:
        projected_end_of_current = max(int(sim_now_min), int(pending["finished_min"]))
    elif started_at is not None and started_at <= sim_now_min:
        projected_end_of_current = started_at + ring_slot
    else:
        sched_start = _hhmm_to_min(queue.iloc[cur_idx]["start_time"]) or int(sim_now_min)
        projected_end_of_current = max(int(sim_now_min), sched_start) + ring_slot

    eta_min = projected_end_of_current + remaining_after_current * ring_slot
    drift = (eta_min - sched_end_min) if sched_end_min is not None else None
    return (_min_to_hhmm(eta_min), drift, sched_end)


def _projected_times_for_sim_day(schedule):
    """Walk each ring once and project actual+projected times for the live day.
    Returns {entry_id: (proj_start_min, proj_end_min, marker)}. Empty when sim
    hasn't started. Cursor jumps when athletes finish early or are absent, so
    every downstream athlete pulls earlier."""
    sim_state = st.session_state.get("sim_ring_state") or {}
    sim_wall_start = st.session_state.get("sim_wall_start")
    if sim_wall_start is None or not sim_state:
        return {}

    import time as _time
    sim_running = st.session_state.get("sim_running", False)
    sim_speed = st.session_state.get("sim_speed", 1)
    paused_offset = st.session_state.get("sim_paused_offset", 0)
    if sim_running:
        elapsed_real = _time.time() - sim_wall_start
        sim_seconds = paused_offset + elapsed_real * sim_speed
    else:
        sim_seconds = paused_offset
    total_min = sim_seconds / 60.0
    SIM_BASE = 9 * 60
    if total_min < 24 * 60:
        sim_day = "Saturday"
        sim_now_min = SIM_BASE + total_min
    else:
        sim_day = "Sunday"
        sim_now_min = SIM_BASE + (total_min - 24 * 60)
    sim_now_min = int(sim_now_min)

    overrides = {}

    for ring in ALL_RINGS:
        if ring not in sim_state:
            continue
        rs = sim_state[ring]
        ring_slot = slot_for_ring(ring)
        log_by_eid = {e["entry_id"]: e for e in rs.get("log", [])}
        pending = rs.get("pending_score")
        started_at = rs.get("current_started_min")
        cur_idx = rs.get("current_idx", 0)

        ring_rows = (
            schedule[(schedule["ring"] == ring) & (schedule["day"] == sim_day)]
            .sort_values("order_in_ring")
            .reset_index(drop=True)
        )

        cursor = None
        for i in range(len(ring_rows)):
            row = ring_rows.iloc[i]
            eid = int(row["entry_id"])
            sched_start = _hhmm_to_min(row["start_time"])

            # Only bridge real structural gaps (lunch ≥ 60 min). Smaller gaps
            # are just accumulated drift from absences/early finishes and must
            # propagate forward.
            LUNCH_GAP_MIN = 30
            if cursor is not None and sched_start is not None and sched_start - cursor >= LUNCH_GAP_MIN:
                cursor = sched_start

            if eid in log_by_eid:
                entry = log_by_eid[eid]
                if entry["status"] == "absent":
                    overrides[eid] = (None, None, "❌")
                    if cursor is None or entry["finished_min"] > cursor:
                        cursor = entry["finished_min"]
                else:
                    overrides[eid] = (entry["started_min"], entry["finished_min"], "✅")
                    cursor = entry["finished_min"]
            elif pending is not None and pending["entry_id"] == eid:
                overrides[eid] = (pending["started_min"], pending["finished_min"], "📝")
                cursor = max(sim_now_min, int(pending["finished_min"]))
            elif i == cur_idx:
                if started_at is not None and started_at <= sim_now_min:
                    overrides[eid] = (started_at, started_at + ring_slot, "🟢")
                    cursor = started_at + ring_slot
                else:
                    base = cursor if cursor is not None else sim_now_min
                    proj_start = max(base, sim_now_min)
                    overrides[eid] = (proj_start, proj_start + ring_slot, "⏳")
                    cursor = proj_start + ring_slot
            else:
                base = cursor if cursor is not None else (sched_start or sim_now_min)
                proj_start = base
                overrides[eid] = (proj_start, proj_start + ring_slot, "")
                cursor = proj_start + ring_slot

    return overrides


def apply_sim_time_overrides(view_df, schedule):
    """Replace start_time/end_time with sim's actual or projected values + marker."""
    overrides = _projected_times_for_sim_day(schedule)
    if not overrides:
        return view_df
    out = view_df.copy()
    for eid, (sm, em, marker) in overrides.items():
        mask = out["entry_id"] == eid
        if not mask.any():
            continue
        if sm is None:
            out.loc[mask, "start_time"] = f"{marker} —" if marker else "—"
            out.loc[mask, "end_time"] = "—"
        else:
            start_str = _min_to_hhmm(sm)
            end_str = _min_to_hhmm(em)
            out.loc[mask, "start_time"] = f"{marker} {start_str}".strip() if marker else start_str
            out.loc[mask, "end_time"] = end_str
    return out


def render_end_time_clocks(schedule):
    for day in EVENT_DAYS:
        st.markdown(f"#### 🗓️ {day}")
        cols = st.columns(len(ALL_RINGS))
        for col, ring in zip(cols, ALL_RINGS):
            eta_str, drift, sched_end = _live_eta_for_ring(ring, day, schedule)
            ring_color = RING_BORDER_COLORS.get(ring, "#666")
            icon = RING_ICONS.get(ring, "")
            label = f"{icon} {ring}"
            with col:
                st.markdown(_clock_face_svg(eta_str, label, ring_color=ring_color), unsafe_allow_html=True)
                if drift is None:
                    st.markdown(
                        f'<div style="text-align:center;font-size:10px;color:#888;margin-top:-4px;">'
                        f'sched {sched_end}</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    if drift > 0:
                        color, sign = "#c0392b", "+"
                    elif drift < 0:
                        color, sign = "#27ae60", ""
                    else:
                        color, sign = "#7f8c8d", ""
                    st.markdown(
                        f'<div style="text-align:center;font-size:10px;color:#888;margin-top:-4px;">'
                        f'sched {sched_end} • <span style="color:{color};font-weight:600;">'
                        f'{sign}{drift}m {"late" if drift > 0 else ("ahead" if drift < 0 else "on time")}</span></div>',
                        unsafe_allow_html=True,
                    )

with st.sidebar:
    st.markdown("# 🥋 PWN 2026")
    st.markdown("**6th Annual Phoenix Wushu Nationals**")
    st.caption("Saturday & Sunday | 9 AM - 6 PM")
    st.divider()

    if st.button("🔄 Rebuild from CSV", help="Wipes manual edits and rebuilds from registrations.csv"):
        st.session_state.schedule = rebuild_schedule()
        st.success("Schedule rebuilt.")
        st.rerun()

    st.divider()
    st.markdown("### 📊 Stats")
    total = len(schedule)
    overflow = (schedule["day"] == "OVERFLOW").sum()
    conflicts_df = detect_conflicts(schedule)
    n_athletes = schedule["athlete"].nunique()

    st.metric("Total entries", total)
    st.metric("Unique athletes", n_athletes)
    st.metric("Overflow (unscheduled)", int(overflow))
    st.metric("Time conflicts", len(conflicts_df))

    st.divider()
    st.markdown("### 📥 Export")
    csv_export = schedule.sort_values(["day", "ring", "order_in_ring"]).to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download Schedule CSV",
        data=csv_export,
        file_name="PWN_2026_schedule.csv",
        mime="text/csv",
    )

st.title("🥋 Terry's Event Schedule")
st.caption("Interactive Schedule Dashboard | 8 Rings × 2 Days")

tab_view, tab_athletes, tab_divisions, tab_move, tab_add, tab_search, tab_sim, tab_conflicts = st.tabs(
    ["📋 Schedule View", "👤 Edit Athletes", "🏷️ Edit Divisions",
     "🔀 Move Division to Another Ring", "➕ Add Athlete", "🔍 Search Athlete",
     "🎬 Simulate", "⚠️ Conflicts"]
)

with tab_view:
    st.subheader("Master Schedule")

    with st.expander("⏰ Estimated end times by ring (click to expand/collapse)", expanded=True):
        eta_tick = "1s" if st.session_state.get("sim_running") else None

        @st.fragment(run_every=eta_tick)
        def _eta_panel():
            sim_running_now = st.session_state.get("sim_running", False)
            sim_started = st.session_state.get("sim_wall_start") is not None
            if sim_running_now:
                st.caption("🟢 Live ETA — projected from current pace; updates every second.")
            elif sim_started:
                st.caption("🟡 Simulator paused — ETA frozen at last sim minute.")
            else:
                st.caption("⚪ Simulator idle — showing static scheduled end times. Start the sim on the 🎬 Simulate tab to see live ETA.")
            render_end_time_clocks(schedule)

        _eta_panel()
    st.divider()

    st.markdown("**Jump to ring:**")
    _inject_ring_button_styles()
    btn_cols = st.columns(len(ALL_RINGS) + 1)
    for col, ring in zip(btn_cols[:-1], ALL_RINGS):
        with col:
            slug = ring.replace(" ", "_")
            if st.button(_label_for_ring(ring), key=f"ringbtn-{slug}-view", width="stretch"):
                st.session_state.view_rings = [ring]
                st.rerun()
    with btn_cols[-1]:
        if st.button("⭐ All rings", key="ringbtn-all-view", width="stretch"):
            st.session_state.view_rings = list(ALL_RINGS)
            st.rerun()

    col1, col2 = st.columns([1, 3])
    with col1:
        day_filter = st.selectbox("Day", ["Both", "Saturday", "Sunday"], key="view_day")
    with col2:
        ring_filter = st.multiselect("Rings (or pick from dropdown)", ALL_RINGS, default=ALL_RINGS, key="view_rings")

    color_map = {}
    for ring in ALL_RINGS:
        ring_view = schedule[schedule["ring"] == ring].sort_values("order_in_ring")
        divs_in_order = []
        seen = set()
        for d in ring_view["division"]:
            if d not in seen:
                divs_in_order.append(d)
                seen.add(d)
        ring_colors = division_colors(divs_in_order)
        for div, color in ring_colors.items():
            color_map[(ring, div)] = color

    highlight_eid = st.session_state.get("highlight_entry_id")
    # Tick whenever the sim has state, not only while running, so absences
    # logged via the Simulate tab show up here even when paused.
    grid_tick = "1s" if st.session_state.get("sim_wall_start") is not None else None

    @st.fragment(run_every=grid_tick)
    def _schedule_grid():
        live_df = schedule.copy()
        live_df = apply_sim_time_overrides(live_df, schedule)

        view_df = live_df
        if day_filter != "Both":
            view_df = view_df[view_df["day"] == day_filter]
        if ring_filter:
            view_df = view_df[view_df["ring"].isin(ring_filter)]
        view_df = view_df.sort_values(["day", "ring", "order_in_ring"]).reset_index(drop=True)

        if highlight_eid is not None:
            match_rows = view_df[view_df["entry_id"] == highlight_eid]
            if not match_rows.empty:
                r = match_rows.iloc[0]
                st.info(f"🎯 Highlighting **{r['athlete']}** on **{r['ring']}** ({r['day']} {r['start_time']}-{r['end_time']}).")
                if st.button("Clear highlight", key="clear_hl"):
                    st.session_state.highlight_entry_id = None
                    st.rerun()

        display_cols = ["entry_id", "day", "ring", "start_time", "end_time", "athlete", "school", "event_category", "division", "score"]
        if "score" not in view_df.columns:
            view_df = view_df.assign(score="")
        display_df = view_df[display_cols].rename(columns={
            "entry_id": "_eid",
            "day": "Day", "ring": "Ring", "start_time": "Start", "end_time": "End",
            "athlete": "Athlete", "school": "School", "event_category": "Event", "division": "Division",
            "score": "Score",
        })

        def _row_style(row):
            color = color_map.get((row["Ring"], row["Division"]), "#FFFFFF")
            base = f"background-color: {color}; color: #000000"
            if highlight_eid is not None and row["_eid"] == highlight_eid:
                base += "; outline: 3px solid #D81B60; outline-offset: -3px; font-weight: 700"
            return [base] * len(row)

        styled = display_df.style.apply(_row_style, axis=1).hide(["_eid"], axis=1)
        if st.session_state.get("sim_wall_start") is not None:
            st.caption("Time slots reflect the live simulator: ✅ done • 🟢 in progress • 📝 awaiting score • ⏳ ready • ❌ absent. Future rows reproject when athletes finish early or are marked absent.")
        st.dataframe(styled, width="stretch", hide_index=True, height=600)

    _schedule_grid()

    st.markdown("### Ring Capacity")
    capacity = schedule.groupby(["ring", "day"]).size().unstack(fill_value=0).reindex(ALL_RINGS)
    st.dataframe(capacity, width="stretch")

with tab_athletes:
    st.subheader("Reorder Individual Athletes")
    st.caption("Drag athletes up or down to reorder. Click **Apply Reorder** to commit changes (times auto-recalculate).")

    st.markdown("**🔍 Find an athlete and jump to their position:**")
    sc1, sc2, sc3 = st.columns([4, 1.5, 1.5])
    with sc1:
        ath_search = st.text_input(
            "Athlete name (partial match)",
            key="ath_search",
            placeholder="Type a name and press Enter",
            label_visibility="collapsed",
        )
    with sc2:
        find_clicked = st.button("🔍 Find", key="ath_find_btn", width="stretch")
    with sc3:
        next_clicked = st.button(
            "⏭️ Next match (Space)",
            key="ath_next_btn",
            width="stretch",
            help="Tab to focus this button, then press Space to cycle to the next match.",
        )

    matches_df = pd.DataFrame()
    if ath_search and ath_search.strip():
        q = ath_search.strip().lower()
        matches_df = schedule[schedule["athlete"].astype(str).str.lower().str.contains(q, na=False)].copy()
        ring_size_map = schedule.groupby("ring").size().to_dict()
        positions = []
        for _, m in matches_df.iterrows():
            ring_queue = (
                schedule[schedule["ring"] == m["ring"]]
                .sort_values("order_in_ring")
                .reset_index(drop=True)
            )
            pos = ring_queue.index[ring_queue["entry_id"] == m["entry_id"]].tolist()
            positions.append(pos[0] + 1 if pos else None)
        matches_df["position_in_ring"] = positions
        matches_df["ring_size"] = matches_df["ring"].map(ring_size_map)
        matches_df = matches_df.sort_values(["ring", "order_in_ring"]).reset_index(drop=True)

    if st.session_state.get("ath_search_last") != ath_search:
        st.session_state.ath_search_last = ath_search
        st.session_state.ath_match_idx = 0

    if (find_clicked or next_clicked) and not matches_df.empty:
        if next_clicked:
            st.session_state.ath_match_idx = (st.session_state.get("ath_match_idx", 0) + 1) % len(matches_df)
        else:
            st.session_state.ath_match_idx = 0

        target = matches_df.iloc[st.session_state.ath_match_idx]
        st.session_state.highlight_athlete_eid = int(target["entry_id"])

        if st.session_state.get("ath_ring") != target["ring"]:
            st.session_state.pending_ath_ring = target["ring"]
            st.rerun()

    if (find_clicked or next_clicked) and matches_df.empty and ath_search and ath_search.strip():
        st.warning(f"No athletes matching '{ath_search}'.")

    if not matches_df.empty:
        cur_idx = st.session_state.get("ath_match_idx", 0) % len(matches_df)
        cur = matches_df.iloc[cur_idx]
        st.success(
            f"Found **{len(matches_df)} entry/entries** matching '{ath_search}'. "
            f"Currently focused on match **{cur_idx + 1} of {len(matches_df)}** — "
            f"**{cur['athlete']}** at **{cur['ring']}** position **{cur['position_in_ring']} of {cur['ring_size']}** "
            f"({cur['day']} {cur['start_time']}–{cur['end_time']})."
        )

        st.markdown("**All matches** — click **Show** to focus that entry, or press Space to cycle.")
        hcols = st.columns([0.5, 2.2, 1.6, 1.8, 1.6, 3, 1.2])
        for c, h in zip(hcols, ["", "Athlete", "Ring", "Position", "Day / Time", "Division", ""]):
            c.markdown(f"**{h}**")

        for i, m in matches_df.iterrows():
            cur_marker = "🎯" if i == cur_idx else ""
            cols = st.columns([0.5, 2.2, 1.6, 1.8, 1.6, 3, 1.2])
            cols[0].markdown(f"**{cur_marker}**")
            cols[1].markdown(f"**{m['athlete']}**")
            cols[2].markdown(f"📍 {m['ring']}")
            cols[3].markdown(f"**{m['position_in_ring']} / {m['ring_size']}**")
            cols[4].markdown(f"{m['day']} {m['start_time']}")
            cols[5].caption(m["division"])
            with cols[6]:
                if st.button("Show", key=f"show_match_{int(m['entry_id'])}", width="stretch"):
                    st.session_state.ath_match_idx = i
                    st.session_state.highlight_athlete_eid = int(m["entry_id"])
                    if st.session_state.get("ath_ring") != m["ring"]:
                        st.session_state.pending_ath_ring = m["ring"]
                    st.rerun()

    if not matches_df.empty and len(matches_df) > 1:
        space_js = """
        <script>
        (function() {
            const win = window.parent;
            const doc = win.document;
            if (win.__pwnSpaceHandlerInstalled) return;
            win.__pwnSpaceHandlerInstalled = true;
            doc.addEventListener("keydown", (e) => {
                if (e.code !== "Space" && e.key !== " ") return;
                const tag = (e.target && e.target.tagName) || "";
                if (tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable)) return;
                const buttons = doc.querySelectorAll('button');
                for (const b of buttons) {
                    if (b.innerText && b.innerText.includes("Next match")) {
                        e.preventDefault();
                        b.click();
                        return;
                    }
                }
            }, true);
        })();
        </script>
        """
        st.components.v1.html(space_js, height=0)

    st.markdown("**Jump to ring:**")
    ring_jump_buttons("ath_ring", "ath_jump")

    ring_pick = st.selectbox("Ring (or pick from dropdown)", ALL_RINGS, key="ath_ring")
    ring_df = schedule[schedule["ring"] == ring_pick].sort_values("order_in_ring").reset_index(drop=True)

    if ring_df.empty:
        st.info(f"No athletes assigned to {ring_pick}.")
    else:
        st.markdown(f"**{len(ring_df)} athletes on {ring_pick}** (across both days)")

        all_items = []
        eid_to_pos = {}
        for idx, (_, row) in enumerate(ring_df.iterrows()):
            eid = int(row["entry_id"])
            day_short = "Sat" if row["day"] == "Saturday" else ("Sun" if row["day"] == "Sunday" else "OVR")
            label = f"[{day_short} {row['start_time']}] {row['athlete']} — {row['division']}"
            all_items.append(f"{eid}|{label}")
            eid_to_pos[eid] = idx

        hl_eid_local = st.session_state.get("highlight_athlete_eid")
        window_mode = (
            hl_eid_local is not None
            and any(int(row["entry_id"]) == hl_eid_local for _, row in ring_df.iterrows())
        )

        if window_mode:
            center = eid_to_pos[hl_eid_local]
            start = max(0, center - 10)
            end = min(len(all_items), center + 11)
            items = all_items[start:end]
            window_offset = start
            st.warning(
                f"🎯 Showing rows **{start + 1}–{end}** of {len(all_items)} on **{ring_pick}** "
                f"(centered on the highlighted athlete). "
                f"Drag-and-drop is disabled in this view — clear the search at the top to re-enable."
            )
        else:
            items = all_items
            window_offset = 0

        ring_divs_in_order = []
        seen_divs = set()
        for d in ring_df["division"]:
            if d not in seen_divs:
                ring_divs_in_order.append(d)
                seen_divs.add(d)
        div_color_map = division_colors(ring_divs_in_order)

        item_css = []
        for visible_idx, item in enumerate(items, start=1):
            eid = int(item.split("|", 1)[0])
            row = ring_df[ring_df["entry_id"] == eid].iloc[0]
            color = div_color_map.get(row["division"], "#FFFFFF")
            extra = ""
            if hl_eid_local is not None and eid == hl_eid_local:
                extra = "outline: 4px solid #D81B60 !important; outline-offset: -4px !important; font-weight: 800 !important;"
            item_css.append(
                f'.sortable-component .sortable-item:nth-of-type({visible_idx})'
                f',.sortable-component .sortable-item:nth-of-type({visible_idx}):hover'
                f' {{ background-color: {color} !important; color: #000000 !important; '
                f'border: 1px solid #888 !important; {extra} }}'
            )
        custom_style = "\n".join(item_css)

        suffix = f"_win{window_offset}" if window_mode else ""
        sortable_key = f"sortable_{ring_pick}{suffix}"
        new_order = sort_items(items, direction="vertical", key=sortable_key, custom_style=custom_style)

        col_a, col_b = st.columns([1, 5])
        with col_a:
            apply_disabled = window_mode
            if st.button(
                "✅ Apply Reorder",
                key=f"apply_{ring_pick}{suffix}",
                type="primary",
                disabled=apply_disabled,
                help="Disabled while a search is active. Clear the search to reorder the full ring." if apply_disabled else None,
            ):
                new_eids = [int(s.split("|", 1)[0]) for s in new_order]
                st.session_state.schedule = reorder_ring(schedule, ring_pick, new_eids)
                save_schedule(st.session_state.schedule)
                st.success(f"Reordered {len(new_eids)} athletes on {ring_pick}.")
                st.rerun()
        with col_b:
            st.caption("Drag above, then click Apply to lock in.")

with tab_divisions:
    st.subheader("Reorder Entire Divisions")
    st.caption("Drag whole divisions (blocks of athletes) up or down. Times update automatically.")

    st.markdown("**🔍 Find a division and jump to its position:**")
    dsc1, dsc2, dsc3 = st.columns([4, 1.5, 1.5])
    with dsc1:
        div_search = st.text_input(
            "Division name (partial match)",
            key="div_search",
            placeholder="e.g. C100A, Pudao, Taiji",
            label_visibility="collapsed",
        )
    with dsc2:
        div_find_clicked = st.button("🔍 Find", key="div_find_btn", width="stretch")
    with dsc3:
        div_next_clicked = st.button(
            "⏭️ Next match (Space)",
            key="div_next_btn",
            width="stretch",
            help="Tab to focus this button, then press Space to cycle to the next match.",
        )

    div_matches_df = pd.DataFrame()
    if div_search and div_search.strip():
        q = div_search.strip().lower()
        unique_pairs = (
            schedule[schedule["division"].astype(str).str.lower().str.contains(q, na=False)]
            .groupby(["ring", "division"])
            .agg(
                size=("entry_id", "size"),
                first_order=("order_in_ring", "min"),
                first_day=("day", "first"),
                first_start=("start_time", "first"),
                last_end=("end_time", "last"),
            )
            .reset_index()
        )

        positions = []
        ring_div_count = {}
        for ring in unique_pairs["ring"].unique():
            ring_view = schedule[schedule["ring"] == ring].sort_values("order_in_ring")
            ordered_divs = []
            seen = set()
            for d in ring_view["division"]:
                if d not in seen:
                    ordered_divs.append(d)
                    seen.add(d)
            ring_div_count[ring] = len(ordered_divs)
            for _, row in unique_pairs[unique_pairs["ring"] == ring].iterrows():
                idx = ordered_divs.index(row["division"]) + 1 if row["division"] in ordered_divs else None
                positions.append((row["ring"], row["division"], idx))
        pos_lookup = {(r, d): p for r, d, p in positions}
        unique_pairs["position_in_ring"] = unique_pairs.apply(
            lambda r: pos_lookup.get((r["ring"], r["division"])), axis=1
        )
        unique_pairs["divs_in_ring"] = unique_pairs["ring"].map(ring_div_count)
        div_matches_df = unique_pairs.sort_values(["ring", "first_order"]).reset_index(drop=True)

    if st.session_state.get("div_search_last") != div_search:
        st.session_state.div_search_last = div_search
        st.session_state.div_match_idx = 0

    if (div_find_clicked or div_next_clicked) and not div_matches_df.empty:
        if div_next_clicked:
            st.session_state.div_match_idx = (st.session_state.get("div_match_idx", 0) + 1) % len(div_matches_df)
        else:
            st.session_state.div_match_idx = 0

        target = div_matches_df.iloc[st.session_state.div_match_idx]
        st.session_state.highlight_division = (target["ring"], target["division"])
        if st.session_state.get("div_ring") != target["ring"]:
            st.session_state.pending_div_ring = target["ring"]
            st.rerun()

    if (div_find_clicked or div_next_clicked) and div_matches_df.empty and div_search and div_search.strip():
        st.warning(f"No divisions matching '{div_search}'.")

    if not div_matches_df.empty:
        cur_idx = st.session_state.get("div_match_idx", 0) % len(div_matches_df)
        cur = div_matches_df.iloc[cur_idx]
        st.success(
            f"Found **{len(div_matches_df)} division(s)** matching '{div_search}'. "
            f"Currently focused on match **{cur_idx + 1} of {len(div_matches_df)}** — "
            f"**{cur['division']}** at **{cur['ring']}** position **{cur['position_in_ring']} of {cur['divs_in_ring']}** "
            f"({cur['first_day']} starting {cur['first_start']}, {cur['size']} athletes)."
        )

        st.markdown("**All matches** — click **Show** to focus that division, or press Space to cycle.")
        hcols = st.columns([0.5, 3, 1.6, 1.8, 1.6, 1, 1.2])
        for c, h in zip(hcols, ["", "Division", "Ring", "Position", "Day / Time", "Athletes", ""]):
            c.markdown(f"**{h}**")

        for i, m in div_matches_df.iterrows():
            cur_marker = "🎯" if i == cur_idx else ""
            cols = st.columns([0.5, 3, 1.6, 1.8, 1.6, 1, 1.2])
            cols[0].markdown(f"**{cur_marker}**")
            cols[1].markdown(f"**{m['division']}**")
            cols[2].markdown(f"📍 {m['ring']}")
            cols[3].markdown(f"**{m['position_in_ring']} / {m['divs_in_ring']}**")
            cols[4].markdown(f"{m['first_day']} {m['first_start']}")
            cols[5].markdown(f"{m['size']}")
            with cols[6]:
                if st.button("Show", key=f"show_div_{m['ring']}_{m['division']}", width="stretch"):
                    st.session_state.div_match_idx = i
                    st.session_state.highlight_division = (m["ring"], m["division"])
                    if st.session_state.get("div_ring") != m["ring"]:
                        st.session_state.pending_div_ring = m["ring"]
                    st.rerun()

    if not div_matches_df.empty and len(div_matches_df) > 1:
        space_js = """
        <script>
        (function() {
            const win = window.parent;
            const doc = win.document;
            if (win.__pwnDivSpaceHandlerInstalled) return;
            win.__pwnDivSpaceHandlerInstalled = true;
            doc.addEventListener("keydown", (e) => {
                if (e.code !== "Space" && e.key !== " ") return;
                const tag = (e.target && e.target.tagName) || "";
                if (tag === "INPUT" || tag === "TEXTAREA" || (e.target && e.target.isContentEditable)) return;
                const buttons = doc.querySelectorAll('button');
                for (const b of buttons) {
                    if (b.innerText && b.innerText.includes("Next match")) {
                        e.preventDefault();
                        b.click();
                        return;
                    }
                }
            }, true);
        })();
        </script>
        """
        st.components.v1.html(space_js, height=0)

    st.markdown("**Jump to ring:**")
    ring_jump_buttons("div_ring", "div_jump")

    ring_pick_d = st.selectbox("Ring (or pick from dropdown)", ALL_RINGS, key="div_ring")
    ring_df = schedule[schedule["ring"] == ring_pick_d].sort_values("order_in_ring")

    if ring_df.empty:
        st.info(f"No divisions assigned to {ring_pick_d}.")
    else:
        divisions_in_order = []
        seen = set()
        for div in ring_df["division"]:
            if div not in seen:
                divisions_in_order.append(div)
                seen.add(div)

        st.markdown(f"**{len(divisions_in_order)} divisions on {ring_pick_d}**")

        all_div_labels = []
        label_to_div = {}
        for div in divisions_in_order:
            div_rows = ring_df[ring_df["division"] == div]
            n = len(div_rows)
            label = f"{div}  ({n} athletes)"
            all_div_labels.append(label)
            label_to_div[label] = div

        hl_div_pair = st.session_state.get("highlight_division")
        div_window_mode = (
            hl_div_pair is not None
            and hl_div_pair[0] == ring_pick_d
            and hl_div_pair[1] in divisions_in_order
        )

        if div_window_mode:
            center = divisions_in_order.index(hl_div_pair[1])
            start = max(0, center - 5)
            end = min(len(all_div_labels), center + 6)
            div_labels = all_div_labels[start:end]
            div_window_offset = start
            st.warning(
                f"🎯 Showing divisions **{start + 1}–{end}** of {len(all_div_labels)} on **{ring_pick_d}** "
                f"(centered on the highlighted division). "
                f"Drag-and-drop is disabled in this view — clear the search to re-enable."
            )
        else:
            div_labels = all_div_labels
            div_window_offset = 0

        st.caption("Drag a division to a new position. Times for the moved block and every block below it update automatically.")

        commit_counter_key = f"div_commits_{ring_pick_d}"
        if commit_counter_key not in st.session_state:
            st.session_state[commit_counter_key] = 0
        suffix = f"_win{div_window_offset}" if div_window_mode else ""
        sortable_key = f"sortable_div_{ring_pick_d}_{st.session_state[commit_counter_key]}{suffix}"

        div_color_map_d = division_colors(divisions_in_order)
        item_css_d = []
        for visible_idx, lbl in enumerate(div_labels, start=1):
            div = label_to_div[lbl]
            color = div_color_map_d.get(div, "#FFFFFF")
            extra = ""
            if div_window_mode and div == hl_div_pair[1]:
                extra = "outline: 4px solid #D81B60 !important; outline-offset: -4px !important; font-weight: 800 !important;"
            item_css_d.append(
                f'.sortable-component .sortable-item:nth-of-type({visible_idx})'
                f',.sortable-component .sortable-item:nth-of-type({visible_idx}):hover'
                f' {{ background-color: {color} !important; color: #000000 !important; '
                f'border: 1px solid #888 !important; {extra} }}'
            )
        custom_style_d = "\n".join(item_css_d)

        new_label_order = sort_items(div_labels, direction="vertical", key=sortable_key, custom_style=custom_style_d)

        if (
            not div_window_mode
            and new_label_order
            and new_label_order != div_labels
        ):
            new_div_order = [label_to_div[lbl] for lbl in new_label_order]
            st.session_state.schedule = reorder_divisions(schedule, ring_pick_d, new_div_order)
            save_schedule(st.session_state.schedule)
            st.session_state[commit_counter_key] += 1
            st.rerun()

        st.markdown("**Current times after most recent reorder:**")
        time_rows = []
        live = st.session_state.schedule
        live_ring = live[live["ring"] == ring_pick_d].sort_values("order_in_ring")
        seen_t = set()
        live_divs_in_order = []
        for _, r in live_ring.iterrows():
            if r["division"] in seen_t:
                continue
            seen_t.add(r["division"])
            live_divs_in_order.append(r["division"])
            div_rows = live_ring[live_ring["division"] == r["division"]]
            time_rows.append({
                "Division": r["division"],
                "Athletes": len(div_rows),
                "Day": div_rows.iloc[0]["day"],
                "Starts": div_rows.iloc[0]["start_time"],
                "Ends": div_rows.iloc[-1]["end_time"],
            })
        time_df = pd.DataFrame(time_rows)
        ring_colors = division_colors(live_divs_in_order)

        def _time_row_style(row):
            color = ring_colors.get(row["Division"], "#FFFFFF")
            return [f"background-color: {color}; color: #000000"] * len(row)

        st.dataframe(time_df.style.apply(_time_row_style, axis=1), width="stretch", hide_index=True)

with tab_move:
    st.subheader("Move a Division to Another Ring")
    st.caption("Pick a division on a source ring, choose a destination ring, and decide where in the destination ring it should land. Both rings recompute their schedules automatically.")

    st.markdown("**Jump to source ring:**")
    ring_jump_buttons("mv_src", "mv_jump")

    col1, col2 = st.columns(2)
    with col1:
        source_ring = st.selectbox("Source ring", ALL_RINGS, key="mv_src")
    with col2:
        dest_ring = st.selectbox("Destination ring", [r for r in ALL_RINGS if r != source_ring], key="mv_dest")

    src_df = schedule[schedule["ring"] == source_ring].sort_values("order_in_ring")
    if src_df.empty:
        st.info(f"No divisions on {source_ring}.")
    else:
        src_divs_in_order = []
        seen = set()
        for d in src_df["division"]:
            if d not in seen:
                src_divs_in_order.append(d)
                seen.add(d)
        div_size = src_df.groupby("division").size().to_dict()
        div_options = {f"{d}  ({div_size[d]} athletes)": d for d in src_divs_in_order}

        chosen_label = st.selectbox(f"Division to move from {source_ring}", list(div_options.keys()), key="mv_div")
        chosen_div = div_options[chosen_label]

        dest_df = schedule[schedule["ring"] == dest_ring].sort_values("order_in_ring")
        dest_divs_in_order = []
        seen = set()
        for d in dest_df["division"]:
            if d not in seen:
                dest_divs_in_order.append(d)
                seen.add(d)

        position_choices = ["End of ring (after last division)", "Start of ring (before first division)"]
        for d in dest_divs_in_order:
            position_choices.append(f"Before: {d}")

        position_label = st.selectbox(f"Where in {dest_ring} should it land?", position_choices, key="mv_pos")

        if position_label.startswith("End"):
            position_arg = "end"
        elif position_label.startswith("Start"):
            position_arg = "start"
        else:
            target_div = position_label.replace("Before: ", "", 1)
            position_arg = dest_divs_in_order.index(target_div)

        src_size_after = len(src_df) - div_size[chosen_div]
        dest_size_after = len(dest_df) + div_size[chosen_div]
        cap = _ring_capacity()
        col_a, col_b = st.columns(2)
        with col_a:
            st.metric(f"{source_ring} after move", src_size_after, delta=-div_size[chosen_div])
            if src_size_after > cap:
                st.caption(f"⚠️ exceeds 2-day capacity of {cap}")
        with col_b:
            st.metric(f"{dest_ring} after move", dest_size_after, delta=div_size[chosen_div])
            if dest_size_after > cap:
                st.caption(f"⚠️ exceeds 2-day capacity of {cap} - {dest_size_after - cap} will overflow")

        if st.button(f"🔀 Move '{chosen_div}' from {source_ring} to {dest_ring}", type="primary"):
            st.session_state.schedule = move_division_to_ring(schedule, chosen_div, source_ring, dest_ring, position=position_arg)
            save_schedule(st.session_state.schedule)
            st.success(f"Moved {div_size[chosen_div]} athletes in '{chosen_div}' to {dest_ring}.")
            st.rerun()

with tab_add:
    st.subheader("Add a New Athlete")
    st.caption("Manually add an athlete to the schedule. They will be placed at the end of the chosen division's block on the appropriate ring, and times will recompute automatically.")

    existing_divs = sorted(schedule["division"].dropna().unique().tolist())

    with st.form("add_athlete_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        with col1:
            new_name = st.text_input("Athlete name", placeholder="e.g. Jane Doe")
            new_school = st.text_input("School (optional)", placeholder="e.g. Phoenix Wushu Academy")
        with col2:
            div_mode = st.radio(
                "Division",
                ["Existing division", "New division"],
                horizontal=True,
                key="add_div_mode",
            )
            if div_mode == "Existing division":
                chosen_div = st.selectbox("Pick existing division", existing_divs, key="add_existing_div")
                chosen_ring = None
                chosen_event = None
            else:
                chosen_div = st.text_input("New division name", placeholder="e.g. T201 - Custom Form")
                chosen_ring = st.selectbox("Ring (required for new division)", ALL_RINGS, key="add_new_ring")
                chosen_event = st.text_input(
                    "Event category (optional)",
                    placeholder="e.g. Wushu Taolu Event: Traditional Wushu Hand Forms",
                )
                if not chosen_event:
                    chosen_event = None

        submitted = st.form_submit_button("➕ Add Athlete", type="primary")

    if submitted:
        if not new_name.strip():
            st.error("Athlete name is required.")
        elif not chosen_div or not chosen_div.strip():
            st.error("Division is required.")
        else:
            try:
                st.session_state.schedule = add_athlete(
                    schedule,
                    athlete=new_name,
                    school=new_school,
                    division=chosen_div.strip(),
                    ring=chosen_ring,
                    event_category=chosen_event,
                )
                save_schedule(st.session_state.schedule)
                placed = st.session_state.schedule[
                    (st.session_state.schedule["athlete"] == new_name.strip())
                    & (st.session_state.schedule["division"] == chosen_div.strip())
                ]
                if not placed.empty:
                    row = placed.iloc[-1]
                    st.success(
                        f"✅ Added **{new_name}** to **{chosen_div}** on **{row['ring']}** "
                        f"— scheduled {row['day']} {row['start_time']}–{row['end_time']}"
                    )
                else:
                    st.success(f"Added {new_name} to {chosen_div}.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))


with tab_search:
    st.subheader("Search for an Athlete")
    st.caption("Type any part of an athlete's name. The matching schedule entries (event, division, ring, day, time) will appear below.")

    query = st.text_input("Athlete name (partial match, case-insensitive)", key="search_query", placeholder="e.g. Brad Wu, Smith, Jorge")

    if query and query.strip():
        q = query.strip().lower()
        matches = schedule[schedule["athlete"].astype(str).str.lower().str.contains(q, na=False)]

        if matches.empty:
            st.warning(f"No athletes matching '{query}'.")
        else:
            unique_athletes = sorted(matches["athlete"].unique())
            st.success(f"Found {len(unique_athletes)} athlete(s) matching '{query}' with {len(matches)} scheduled entries.")

            for athlete in unique_athletes:
                ath_rows = matches[matches["athlete"] == athlete].sort_values(["day", "start_time"])
                st.markdown(f"### 👤 {athlete}")
                school = ath_rows.iloc[0]["school"]
                if school:
                    st.caption(f"School: {school}")

                div_color_map_s = {}
                for ring in ath_rows["ring"].unique():
                    ring_view = schedule[schedule["ring"] == ring].sort_values("order_in_ring")
                    divs_in_order = []
                    seen = set()
                    for d in ring_view["division"]:
                        if d not in seen:
                            divs_in_order.append(d)
                            seen.add(d)
                    for div, color in division_colors(divs_in_order).items():
                        div_color_map_s[(ring, div)] = color

                for _, ath_row in ath_rows.iterrows():
                    entry_id = int(ath_row["entry_id"])
                    color = div_color_map_s.get((ath_row["ring"], ath_row["division"]), "#FFFFFF")
                    cols = st.columns([1.2, 1.2, 1.2, 2, 4, 3, 1.2])
                    style = f"background-color:{color};color:#000;padding:6px 8px;border-radius:6px;display:block;"
                    with cols[0]:
                        st.markdown(f'<div style="{style}">{ath_row["day"]}</div>', unsafe_allow_html=True)
                    with cols[1]:
                        st.markdown(f'<div style="{style}">{ath_row["start_time"]}</div>', unsafe_allow_html=True)
                    with cols[2]:
                        st.markdown(f'<div style="{style}">{ath_row["end_time"]}</div>', unsafe_allow_html=True)
                    with cols[3]:
                        st.markdown(f'<div style="{style}">{ath_row["ring"]}</div>', unsafe_allow_html=True)
                    with cols[4]:
                        st.markdown(f'<div style="{style}">{ath_row["event_category"]}</div>', unsafe_allow_html=True)
                    with cols[5]:
                        st.markdown(f'<div style="{style}">{ath_row["division"]}</div>', unsafe_allow_html=True)
                    with cols[6]:
                        if st.button("📋 Jump", key=f"jump_{entry_id}", help="Set Schedule View filters to this row and highlight it. Then click the 📋 Schedule View tab."):
                            st.session_state.pending_view_rings = [ath_row["ring"]]
                            st.session_state.pending_view_day = ath_row["day"]
                            st.session_state.highlight_entry_id = entry_id
                            st.toast(f"Filters set — click the 📋 Schedule View tab to see {athlete} on {ath_row['ring']} at {ath_row['start_time']}.", icon="✅")
                            st.rerun()

                ath_conflicts = conflicts_df[conflicts_df["athlete"] == athlete] if not conflicts_df.empty else pd.DataFrame()
                if not ath_conflicts.empty:
                    st.warning(f"⚠️ {len(ath_conflicts)} time conflict(s) for {athlete}. See the Conflicts tab.")


with tab_sim:
    st.subheader("🎬 Tournament Simulation")
    st.caption(
        "Click **Start** to simulate the tournament beginning at 9:00 AM Saturday. "
        "Mark each athlete **Complete** (or **Absent**) on each ring as they finish. "
        "Early/late completions shift downstream times automatically."
    )

    if "sim_running" not in st.session_state:
        st.session_state.sim_running = False
    if "sim_wall_start" not in st.session_state:
        st.session_state.sim_wall_start = None
    if "sim_speed" not in st.session_state:
        st.session_state.sim_speed = 1
    if "sim_paused_offset" not in st.session_state:
        st.session_state.sim_paused_offset = 0
    if "sim_ring_state" not in st.session_state:
        st.session_state.sim_ring_state = {}

    SIM_DAY_START_HOUR = 9
    SIM_SAT_BASE_MIN = SIM_DAY_START_HOUR * 60

    def _min_to_clock(m):
        h = int(m // 60) % 24
        mi = int(m % 60)
        return f"{h:02d}:{mi:02d}"

    def _sim_seconds_now():
        if st.session_state.sim_wall_start is None:
            return 0
        if st.session_state.sim_running:
            import time as _time
            elapsed_real = _time.time() - st.session_state.sim_wall_start
            return st.session_state.sim_paused_offset + elapsed_real * st.session_state.sim_speed
        return st.session_state.sim_paused_offset

    def _sim_minutes_in_day_now(sim_seconds):
        total_min = sim_seconds / 60.0
        if total_min < 24 * 60:
            return "Saturday", SIM_SAT_BASE_MIN + total_min
        return "Sunday", SIM_SAT_BASE_MIN + (total_min - 24 * 60)

    def _ring_state(ring):
        if ring not in st.session_state.sim_ring_state:
            st.session_state.sim_ring_state[ring] = {
                "current_idx": 0,
                "current_started_min": None,
                "log": [],
                "pending_score": None,
            }
        rs = st.session_state.sim_ring_state[ring]
        rs.setdefault("pending_score", None)
        return rs

    def _time_to_min_safe(t):
        try:
            hh, mm = str(t).split(":")
            return int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            return None

    def _start_athlete_on_ring(ring, sim_now_min, queue):
        rs = _ring_state(ring)
        if rs["current_idx"] >= len(queue):
            return
        sched_start = _time_to_min_safe(queue.iloc[rs["current_idx"]]["start_time"]) or sim_now_min
        rs["current_started_min"] = max(int(sim_now_min), sched_start)

    ctrl_a, ctrl_b, ctrl_c, ctrl_d = st.columns([1.2, 1.2, 1.2, 2.4])
    with ctrl_a:
        if not st.session_state.sim_running:
            if st.button("▶️ Start", key="sim_start", type="primary", width="stretch"):
                import time as _time
                st.session_state.sim_running = True
                st.session_state.sim_wall_start = _time.time()
                st.session_state.sim_paused_offset = 0
                st.session_state.sim_ring_state = {}
                st.rerun()
        else:
            if st.button("⏸️ Pause", key="sim_pause", width="stretch"):
                import time as _time
                elapsed_real = _time.time() - st.session_state.sim_wall_start
                st.session_state.sim_paused_offset += elapsed_real * st.session_state.sim_speed
                st.session_state.sim_running = False
                st.rerun()

    with ctrl_b:
        if st.button("⏹️ Reset", key="sim_reset", width="stretch"):
            st.session_state.sim_running = False
            st.session_state.sim_wall_start = None
            st.session_state.sim_paused_offset = 0
            st.session_state.sim_ring_state = {}
            st.rerun()

    with ctrl_c:
        st.session_state.sim_speed = st.selectbox(
            "Speed",
            [1, 5, 10, 30, 60, 300],
            index=[1, 5, 10, 30, 60, 300].index(st.session_state.sim_speed),
            format_func=lambda x: f"{x}× real time",
            key="sim_speed_pick",
        )

    with ctrl_d:
        st.markdown(
            "Use **✅ Complete** when an athlete finishes (early or late updates downstream times). "
            "Use **❌ Absent** to skip them."
        )

    # Live panel wrapped in st.fragment — only this block reruns each second,
    # so the Start/Pause/Reset/Speed controls above stay live (not dimmed).
    tick_interval = "1s" if st.session_state.sim_running else None

    @st.fragment(run_every=tick_interval)
    def _sim_live_panel():
        sim_seconds = _sim_seconds_now()
        sim_day_label, minutes_into_day = _sim_minutes_in_day_now(sim_seconds)
        sim_clock_str = _min_to_clock(minutes_into_day)
        sim_now_min = int(minutes_into_day)

        cdt1, cdt2 = st.columns([1, 3])
        with cdt1:
            st.markdown(
                _clock_face_svg(sim_clock_str, f"{sim_day_label}", ring_color="#D81B60"),
                unsafe_allow_html=True,
            )
        with cdt2:
            if st.session_state.sim_running:
                status = "🟢 **RUNNING**"
            elif st.session_state.sim_wall_start is not None:
                status = "🟡 **PAUSED**"
            else:
                status = "⚪ **IDLE** (click Start to begin)"
            st.markdown(f"### {status}")
            st.markdown(f"**Simulated time:** {sim_day_label} {sim_clock_str}")
            st.markdown(f"**Speed:** {st.session_state.sim_speed}× real time")

        st.divider()

        if st.session_state.sim_wall_start is not None:
            st.markdown("### 🎯 Active queue per ring")

            for ring in ALL_RINGS:
                ring_color = RING_BORDER_COLORS.get(ring, "#666")
                icon = RING_ICONS.get(ring, "")

                queue = (
                    schedule[(schedule["ring"] == ring) & (schedule["day"] == sim_day_label)]
                    .sort_values("order_in_ring")
                    .reset_index(drop=True)
                )

                rs = _ring_state(ring)
                cur_idx = rs["current_idx"]

                # The user explicitly starts each athlete; we never auto-start.
                current_row = queue.iloc[cur_idx] if cur_idx < len(queue) else None
                next_row = queue.iloc[cur_idx + 1] if cur_idx + 1 < len(queue) else None

                ring_slot = slot_for_ring(ring)

                rcol1, rcol2 = st.columns([1, 6])
                with rcol1:
                    st.markdown(
                        f'<div style="border-left:6px solid {ring_color};padding:6px 12px;'
                        f'border-radius:4px;background:#f5f5f5;color:#222;font-weight:700;">'
                        f'{icon} {ring}</div>',
                        unsafe_allow_html=True,
                    )
                with rcol2:
                    done = sum(1 for e in rs["log"] if e["status"] in ("complete", "absent"))
                    total = len(queue)
                    st.markdown(
                        f"Progress: **{done}/{total}** — {len([e for e in rs['log'] if e['status'] == 'absent'])} absent."
                    )

                if current_row is None:
                    st.markdown(
                        f'<div style="background:#f0f0f0;color:#555;padding:6px 10px;'
                        f'border-radius:6px;">No more athletes on {ring} today.</div>',
                        unsafe_allow_html=True,
                    )
                    st.write("")
                    continue

                started_at = rs["current_started_min"]
                sched_start = _time_to_min_safe(current_row["start_time"]) or sim_now_min
                projected_start = started_at if started_at is not None else max(sim_now_min, sched_start)
                projected_end = projected_start + ring_slot

                in_progress = (started_at is not None and started_at <= sim_now_min)

                # Athletes are capped at the ring's slot duration. Once their
                # elapsed time hits the slot length, auto-flip into score-entry.
                if in_progress and sim_now_min >= started_at + ring_slot and rs["pending_score"] is None:
                    rs["pending_score"] = {
                        "entry_id": int(current_row["entry_id"]),
                        "athlete": current_row["athlete"],
                        "division": current_row["division"],
                        "started_min": started_at,
                        "finished_min": started_at + ring_slot,
                        "duration_min": ring_slot,
                        "auto_capped": True,
                    }
                    st.rerun(scope="fragment")

                pending = rs["pending_score"]

                if pending is not None:
                    cap_msg = " (auto-completed at cap)" if pending.get("auto_capped") else ""
                    scols = st.columns([5, 2.2, 1.6])
                    with scols[0]:
                        st.markdown(
                            f'<div style="background:#e3f0ff;color:#000;padding:8px 12px;'
                            f'border-radius:6px;border:2px solid #1F5FBF;">'
                            f'<b>📝 ENTER SCORE:</b> {pending["athlete"]} — {pending["division"]}<br/>'
                            f'<small>Finished at {_min_to_clock(pending["finished_min"])} • Duration {pending["duration_min"]} min{cap_msg}</small>'
                            f'</div>',
                            unsafe_allow_html=True,
                        )
                    with scols[1]:
                        score_val = st.text_input(
                            "Score",
                            key=f"sim_score_input_{ring}_{cur_idx}",
                            placeholder="e.g. 9.45",
                            label_visibility="collapsed",
                        )
                    with scols[2]:
                        if st.button("💾 Save Score", key=f"sim_save_score_{ring}_{cur_idx}",
                                     type="primary", width="stretch",
                                     help="Save the score and advance to the next athlete."):
                            score_str = (score_val or "").strip()
                            sched = st.session_state.schedule
                            sched.loc[sched["entry_id"] == pending["entry_id"], "score"] = score_str
                            save_schedule(sched)
                            rs["log"].append({
                                "entry_id": pending["entry_id"],
                                "athlete": pending["athlete"],
                                "division": pending["division"],
                                "status": "complete",
                                "started_min": pending["started_min"],
                                "finished_min": pending["finished_min"],
                                "duration_min": pending["duration_min"],
                                "score": score_str,
                            })
                            rs["current_idx"] += 1
                            rs["current_started_min"] = None
                            rs["pending_score"] = None
                            st.rerun(scope="fragment")
                else:
                    ccols = st.columns([5, 1.5, 1.5, 1.5])
                    with ccols[0]:
                        if in_progress:
                            elapsed = max(0, sim_now_min - started_at)
                            remaining = max(0, ring_slot - elapsed)
                            st.markdown(
                                f'<div style="background:#dff5e0;color:#000;padding:8px 12px;'
                                f'border-radius:6px;border:2px solid #2E7D32;">'
                                f'<b>NOW:</b> {current_row["athlete"]} — {current_row["division"]}<br/>'
                                f'<small>Started {_min_to_clock(started_at)} • Sched {current_row["start_time"]}–{current_row["end_time"]}</small><br/>'
                                f'<small>⏱️ {elapsed}/{ring_slot} min ({remaining} min remaining — auto-completes at cap)</small>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                        else:
                            st.markdown(
                                f'<div style="background:#fffceb;color:#000;padding:8px 12px;'
                                f'border-radius:6px;border:2px solid #B58900;">'
                                f'<b>READY:</b> {current_row["athlete"]} — {current_row["division"]}<br/>'
                                f'<small>Sched {current_row["start_time"]}–{current_row["end_time"]} • click ▶️ Start when this athlete begins</small>'
                                f'</div>',
                                unsafe_allow_html=True,
                            )
                    with ccols[1]:
                        start_disabled = in_progress
                        if st.button("▶️ Start", key=f"sim_start_athlete_{ring}_{cur_idx}",
                                     width="stretch", disabled=start_disabled,
                                     help="Click when this athlete starts performing." if not start_disabled else "Already in progress."):
                            rs["current_started_min"] = sim_now_min
                            st.rerun(scope="fragment")
                    with ccols[2]:
                        if st.button("✅ Complete", key=f"sim_done_{ring}_{cur_idx}", type="primary",
                                     width="stretch", disabled=not in_progress,
                                     help="Click when this athlete finishes." if in_progress else "Click ▶️ Start first."):
                            finished_at = sim_now_min
                            actual_start = started_at if started_at is not None else sim_now_min
                            duration = max(0, finished_at - actual_start)
                            rs["pending_score"] = {
                                "entry_id": int(current_row["entry_id"]),
                                "athlete": current_row["athlete"],
                                "division": current_row["division"],
                                "started_min": actual_start,
                                "finished_min": finished_at,
                                "duration_min": duration,
                                "auto_capped": False,
                            }
                            st.rerun(scope="fragment")
                    with ccols[3]:
                        if st.button("❌ Absent", key=f"sim_absent_{ring}_{cur_idx}",
                                     width="stretch", disabled=in_progress,
                                     help="Skip this athlete and move to the next." if not in_progress else "Already in progress."):
                            sched = st.session_state.schedule
                            sched.loc[sched["entry_id"] == int(current_row["entry_id"]), "score"] = "ABSENT"
                            save_schedule(sched)
                            rs["log"].append({
                                "entry_id": int(current_row["entry_id"]),
                                "athlete": current_row["athlete"],
                                "division": current_row["division"],
                                "status": "absent",
                                "started_min": None,
                                "finished_min": sim_now_min,
                                "duration_min": 0,
                                "score": "ABSENT",
                            })
                            rs["current_idx"] += 1
                            rs["current_started_min"] = None
                            st.rerun(scope="fragment")

                # ----- Forecast: next 10 athletes with projected times -----
                projected_current_end = (
                    started_at + ring_slot if in_progress
                    else max(sim_now_min, sched_start) + ring_slot
                )
                forecast_html = []
                for i in range(1, 11):
                    fc_idx = cur_idx + i
                    if fc_idx >= len(queue):
                        break
                    fc = queue.iloc[fc_idx]
                    proj_start = projected_current_end + (i - 1) * ring_slot
                    proj_end = proj_start + ring_slot
                    fc_sched_min = _time_to_min_safe(fc["start_time"])
                    if fc_sched_min is not None:
                        drift = proj_start - fc_sched_min
                        if drift > 0:
                            drift_str = f'<span style="color:#c0392b;">+{drift}m late</span>'
                        elif drift < 0:
                            drift_str = f'<span style="color:#27ae60;">{drift}m ahead</span>'
                        else:
                            drift_str = '<span style="color:#7f8c8d;">on time</span>'
                    else:
                        drift_str = ""
                    forecast_html.append(
                        f'<tr style="border-top:1px solid #eee;">'
                        f'<td style="padding:3px 8px;color:#888;width:34px;text-align:right;">#{i}</td>'
                        f'<td style="padding:3px 8px;font-family:monospace;width:110px;color:#222;">'
                        f'{_min_to_clock(proj_start)}–{_min_to_clock(proj_end)}</td>'
                        f'<td style="padding:3px 8px;color:#222;">{fc["athlete"]}</td>'
                        f'<td style="padding:3px 8px;color:#666;font-size:0.9em;">{fc["division"]}</td>'
                        f'<td style="padding:3px 8px;font-size:0.85em;text-align:right;width:90px;">{drift_str}</td>'
                        f'</tr>'
                    )

                if forecast_html:
                    st.markdown(
                        f'<div style="margin-top:6px;">'
                        f'<div style="font-size:0.82em;color:#666;font-weight:600;padding:0 4px 2px;">'
                        f'📋 NEXT {len(forecast_html)} (projected from current pace):</div>'
                        f'<table style="width:100%;border-collapse:collapse;background:#fafafa;'
                        f'border-radius:6px;border:1px solid #ddd;">'
                        + "".join(forecast_html) +
                        f'</table></div>',
                        unsafe_allow_html=True,
                    )

                st.write("")

            with st.expander("📜 Activity log (latest 30 actions across all rings)"):
                log_rows = []
                for ring, rs in st.session_state.sim_ring_state.items():
                    for entry in rs["log"]:
                        log_rows.append({
                            "Ring": ring,
                            "Status": entry["status"],
                            "Athlete": entry["athlete"],
                            "Division": entry["division"],
                            "Started": _min_to_clock(entry["started_min"]) if entry["started_min"] is not None else "—",
                            "Finished": _min_to_clock(entry["finished_min"]),
                            "Duration (min)": entry["duration_min"],
                            "Score": entry.get("score", ""),
                        })
                if log_rows:
                    log_df = pd.DataFrame(log_rows).sort_values("Finished", ascending=False).head(30)
                    st.dataframe(log_df, width="stretch", hide_index=True)
                else:
                    st.caption("No actions yet. Mark an athlete Complete or Absent to populate the log.")

    _sim_live_panel()


with tab_conflicts:
    st.subheader("Time Conflicts")
    st.caption("Athletes scheduled at overlapping times in different rings.")

    if conflicts_df.empty:
        st.success("✅ No conflicts detected.")
    else:
        st.warning(f"⚠️ {len(conflicts_df)} conflict(s) found.")
        st.dataframe(conflicts_df, width="stretch", hide_index=True)

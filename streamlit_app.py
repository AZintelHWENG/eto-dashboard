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


def _ring_capacity(slot_minutes=SLOT_MINUTES):
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


def build_schedule(df, slot_minutes=SLOT_MINUTES):
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
        ring_df = ring_df.sort_values(["event_category", "division", "athlete"]).reset_index(drop=True)

        day_idx = 0
        cursor = day_start_min
        for _, row in ring_df.iterrows():
            placed = False
            while day_idx < len(EVENT_DAYS):
                if cursor < lunch_end_min and cursor + slot_minutes > lunch_start_min:
                    cursor = lunch_end_min
                if cursor + slot_minutes > day_end_min:
                    day_idx += 1
                    cursor = day_start_min
                    continue
                rows.append({
                    **row.to_dict(),
                    "day": EVENT_DAYS[day_idx],
                    "start_time": _minutes_to_time(cursor),
                    "end_time": _minutes_to_time(cursor + slot_minutes),
                    "order_in_ring": len(rows),
                })
                cursor += slot_minutes
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


def renumber_ring(schedule, ring, slot_minutes=SLOT_MINUTES):
    schedule = schedule.copy()
    ring_mask = schedule["ring"] == ring
    ring_df = schedule[ring_mask].sort_values("order_in_ring").reset_index()

    day_start_min = _time_to_minutes(DAY_START)
    day_end_min = _time_to_minutes(DAY_END)
    lunch_start_min = _time_to_minutes(LUNCH_START)
    lunch_end_min = _time_to_minutes(LUNCH_END)

    day_idx = 0
    cursor = day_start_min
    for _, row in ring_df.iterrows():
        orig_idx = row["index"]
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


# =============================================================================
# Streamlit dashboard
# =============================================================================

APP_DIR = Path(__file__).parent
CSV_PATH = APP_DIR / "registrations.csv"
STATE_PATH = APP_DIR / "schedule_state.json"

st.set_page_config(
    page_title="6th Annual Phoenix Wushu Nationals - Schedule",
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

schedule = st.session_state.schedule

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

st.title("🥋 Run schedule")
st.caption("Interactive Schedule Dashboard | 8 Rings × 2 Days")

tab_view, tab_athletes, tab_divisions, tab_move, tab_conflicts = st.tabs(
    ["📋 Schedule View", "👤 Edit Athletes", "🏷️ Edit Divisions", "🔀 Move Division to Another Ring", "⚠️ Conflicts"]
)

with tab_view:
    st.subheader("Master Schedule")
    col1, col2 = st.columns([1, 3])
    with col1:
        day_filter = st.selectbox("Day", ["Both", "Saturday", "Sunday"], key="view_day")
    with col2:
        ring_filter = st.multiselect("Rings", ALL_RINGS, default=ALL_RINGS, key="view_rings")

    view_df = schedule.copy()
    if day_filter != "Both":
        view_df = view_df[view_df["day"] == day_filter]
    if ring_filter:
        view_df = view_df[view_df["ring"].isin(ring_filter)]
    view_df = view_df.sort_values(["day", "ring", "order_in_ring"]).reset_index(drop=True)

    display_cols = ["day", "ring", "start_time", "end_time", "athlete", "school", "event_category", "division"]
    display_df = view_df[display_cols].rename(columns={
        "day": "Day", "ring": "Ring", "start_time": "Start", "end_time": "End",
        "athlete": "Athlete", "school": "School", "event_category": "Event", "division": "Division",
    })
    st.dataframe(display_df, width="stretch", hide_index=True, height=600)

    st.markdown("### Ring Capacity")
    capacity = schedule.groupby(["ring", "day"]).size().unstack(fill_value=0).reindex(ALL_RINGS)
    st.dataframe(capacity, width="stretch")

with tab_athletes:
    st.subheader("Reorder Individual Athletes")
    st.caption("Drag athletes up or down to reorder. Click **Apply Reorder** to commit changes (times auto-recalculate).")

    ring_pick = st.selectbox("Ring", ALL_RINGS, key="ath_ring")
    ring_df = schedule[schedule["ring"] == ring_pick].sort_values("order_in_ring").reset_index(drop=True)

    if ring_df.empty:
        st.info(f"No athletes assigned to {ring_pick}.")
    else:
        st.markdown(f"**{len(ring_df)} athletes on {ring_pick}** (across both days)")

        items = []
        for _, row in ring_df.iterrows():
            eid = int(row["entry_id"])
            day_short = "Sat" if row["day"] == "Saturday" else ("Sun" if row["day"] == "Sunday" else "OVR")
            label = f"[{day_short} {row['start_time']}] {row['athlete']} — {row['division']}"
            items.append(f"{eid}|{label}")

        sortable_key = f"sortable_{ring_pick}"
        new_order = sort_items(items, direction="vertical", key=sortable_key)

        col_a, col_b = st.columns([1, 5])
        with col_a:
            if st.button("✅ Apply Reorder", key=f"apply_{ring_pick}", type="primary"):
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

    ring_pick_d = st.selectbox("Ring", ALL_RINGS, key="div_ring")
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

        div_labels = []
        label_to_div = {}
        for div in divisions_in_order:
            div_rows = ring_df[ring_df["division"] == div]
            n = len(div_rows)
            label = f"{div}  ({n} athletes)"
            div_labels.append(label)
            label_to_div[label] = div

        st.caption("Drag a division to a new position. Times for the moved block and every block below it update automatically.")

        commit_counter_key = f"div_commits_{ring_pick_d}"
        if commit_counter_key not in st.session_state:
            st.session_state[commit_counter_key] = 0
        sortable_key = f"sortable_div_{ring_pick_d}_{st.session_state[commit_counter_key]}"

        new_label_order = sort_items(div_labels, direction="vertical", key=sortable_key)

        if new_label_order and new_label_order != div_labels:
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
        for _, r in live_ring.iterrows():
            if r["division"] in seen_t:
                continue
            seen_t.add(r["division"])
            div_rows = live_ring[live_ring["division"] == r["division"]]
            time_rows.append({
                "Division": r["division"],
                "Athletes": len(div_rows),
                "Day": div_rows.iloc[0]["day"],
                "Starts": div_rows.iloc[0]["start_time"],
                "Ends": div_rows.iloc[-1]["end_time"],
            })
        st.dataframe(pd.DataFrame(time_rows), width="stretch", hide_index=True)

with tab_move:
    st.subheader("Move a Division to Another Ring")
    st.caption("Pick a division on a source ring, choose a destination ring, and decide where in the destination ring it should land. Both rings recompute their schedules automatically.")

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

with tab_conflicts:
    st.subheader("Time Conflicts")
    st.caption("Athletes scheduled at overlapping times in different rings.")

    if conflicts_df.empty:
        st.success("✅ No conflicts detected.")
    else:
        st.warning(f"⚠️ {len(conflicts_df)} conflict(s) found.")
        st.dataframe(conflicts_df, width="stretch", hide_index=True)

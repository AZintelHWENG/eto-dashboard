"""
6th Annual Phoenix Wushu Nationals - Schedule Dashboard
Interactive Streamlit app: 8-ring, 2-day schedule with admin reordering.
"""

import json
from pathlib import Path

import pandas as pd
import streamlit as st
from streamlit_sortables import sort_items

import schedule_builder as sb

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
        return sb.schedule_from_dict(records)
    df = sb.load_registrations(str(CSV_PATH))
    df = sb.auto_assign_rings(df)
    return sb.build_schedule(df)


def save_schedule(schedule):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(sb.schedule_to_dict(schedule), f, indent=2)


def rebuild_schedule():
    df = sb.load_registrations(str(CSV_PATH))
    df = sb.auto_assign_rings(df)
    schedule = sb.build_schedule(df)
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
    conflicts_df = sb.detect_conflicts(schedule)
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
        ring_filter = st.multiselect(
            "Rings",
            sb.ALL_RINGS,
            default=sb.ALL_RINGS,
            key="view_rings",
        )

    view_df = schedule.copy()
    if day_filter != "Both":
        view_df = view_df[view_df["day"] == day_filter]
    if ring_filter:
        view_df = view_df[view_df["ring"].isin(ring_filter)]

    view_df = view_df.sort_values(["day", "ring", "order_in_ring"]).reset_index(drop=True)

    display_cols = ["day", "ring", "start_time", "end_time", "athlete", "school", "event_category", "division"]
    display_df = view_df[display_cols].rename(columns={
        "day": "Day",
        "ring": "Ring",
        "start_time": "Start",
        "end_time": "End",
        "athlete": "Athlete",
        "school": "School",
        "event_category": "Event",
        "division": "Division",
    })

    st.dataframe(display_df, width="stretch", hide_index=True, height=600)

    st.markdown("### Ring Capacity")
    capacity = (
        schedule.groupby(["ring", "day"]).size().unstack(fill_value=0).reindex(sb.ALL_RINGS)
    )
    st.dataframe(capacity, width="stretch")

with tab_athletes:
    st.subheader("Reorder Individual Athletes")
    st.caption("Drag athletes up or down to reorder. Click **Apply Reorder** to commit changes (times auto-recalculate).")

    ring_pick = st.selectbox("Ring", sb.ALL_RINGS, key="ath_ring")

    ring_df = schedule[schedule["ring"] == ring_pick].sort_values("order_in_ring").reset_index(drop=True)

    if ring_df.empty:
        st.info(f"No athletes assigned to {ring_pick}.")
    else:
        st.markdown(f"**{len(ring_df)} athletes on {ring_pick}** (across both days)")

        # Encode each athlete as "entry_id|label" so we can parse the drag result back to entry_ids.
        items = []
        eid_to_label = {}
        for _, row in ring_df.iterrows():
            eid = int(row["entry_id"])
            day_short = "Sat" if row["day"] == "Saturday" else ("Sun" if row["day"] == "Sunday" else "OVR")
            label = f"[{day_short} {row['start_time']}] {row['athlete']} — {row['division']}"
            tagged = f"{eid}|{label}"
            items.append(tagged)
            eid_to_label[eid] = tagged

        sortable_key = f"sortable_{ring_pick}"
        new_order = sort_items(items, direction="vertical", key=sortable_key)

        col_a, col_b = st.columns([1, 5])
        with col_a:
            if st.button("✅ Apply Reorder", key=f"apply_{ring_pick}", type="primary"):
                new_eids = [int(s.split("|", 1)[0]) for s in new_order]
                st.session_state.schedule = sb.reorder_ring(schedule, ring_pick, new_eids)
                save_schedule(st.session_state.schedule)
                st.success(f"Reordered {len(new_eids)} athletes on {ring_pick}.")
                st.rerun()
        with col_b:
            st.caption("Drag above, then click Apply to lock in. Refresh resets the drag without applying.")

with tab_divisions:
    st.subheader("Reorder Entire Divisions")
    st.caption("Drag whole divisions (blocks of athletes) up or down. Click **Apply Reorder** to commit.")

    ring_pick_d = st.selectbox("Ring", sb.ALL_RINGS, key="div_ring")
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

        # Stable labels (no times embedded) so the sortable widget's cached
        # state always matches the current schedule. Times are shown in a
        # separate read-only table below.
        div_labels = []
        label_to_div = {}
        for div in divisions_in_order:
            div_rows = ring_df[ring_df["division"] == div]
            n = len(div_rows)
            label = f"{div}  ({n} athletes)"
            div_labels.append(label)
            label_to_div[label] = div

        st.caption("Drag a division block to a new position. Times for the moved block and every block below it update automatically.")

        # Bump the key after each commit so the widget reinitializes with the
        # new ordering. Stored counter lives in session_state.
        commit_counter_key = f"div_commits_{ring_pick_d}"
        if commit_counter_key not in st.session_state:
            st.session_state[commit_counter_key] = 0
        sortable_key = f"sortable_div_{ring_pick_d}_{st.session_state[commit_counter_key]}"

        new_label_order = sort_items(div_labels, direction="vertical", key=sortable_key)

        # Auto-apply when the order has actually changed.
        if new_label_order and new_label_order != div_labels:
            new_div_order = [label_to_div[lbl] for lbl in new_label_order]
            st.session_state.schedule = sb.reorder_divisions(schedule, ring_pick_d, new_div_order)
            save_schedule(st.session_state.schedule)
            st.session_state[commit_counter_key] += 1
            st.rerun()

        # Read-only times table that always reflects the current schedule.
        st.markdown("**Current times after most recent reorder:**")
        time_rows = []
        # Re-read schedule (it may have just been updated above before rerun, but we land here only when no change).
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
        source_ring = st.selectbox("Source ring", sb.ALL_RINGS, key="mv_src")
    with col2:
        dest_ring = st.selectbox(
            "Destination ring",
            [r for r in sb.ALL_RINGS if r != source_ring],
            key="mv_dest",
        )

    src_df = schedule[schedule["ring"] == source_ring].sort_values("order_in_ring")
    if src_df.empty:
        st.info(f"No divisions on {source_ring}.")
    else:
        # Divisions on the source ring, in current order.
        src_divs_in_order = []
        seen = set()
        for d in src_df["division"]:
            if d not in seen:
                src_divs_in_order.append(d)
                seen.add(d)
        div_size = src_df.groupby("division").size().to_dict()
        div_options = {f"{d}  ({div_size[d]} athletes)": d for d in src_divs_in_order}

        chosen_label = st.selectbox(
            f"Division to move from {source_ring}",
            list(div_options.keys()),
            key="mv_div",
        )
        chosen_div = div_options[chosen_label]

        # Destination position selector.
        dest_df = schedule[schedule["ring"] == dest_ring].sort_values("order_in_ring")
        dest_divs_in_order = []
        seen = set()
        for d in dest_df["division"]:
            if d not in seen:
                dest_divs_in_order.append(d)
                seen.add(d)

        position_choices = ["End of ring (after last division)", "Start of ring (before first division)"]
        for i, d in enumerate(dest_divs_in_order):
            position_choices.append(f"Before: {d}")

        position_label = st.selectbox(
            f"Where in {dest_ring} should it land?",
            position_choices,
            key="mv_pos",
        )

        if position_label.startswith("End"):
            position_arg = "end"
        elif position_label.startswith("Start"):
            position_arg = "start"
        else:
            target_div = position_label.replace("Before: ", "", 1)
            position_arg = dest_divs_in_order.index(target_div)

        # Capacity preview.
        src_size_after = len(src_df) - div_size[chosen_div]
        dest_size_after = len(dest_df) + div_size[chosen_div]
        cap = sb._ring_capacity()
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
            st.session_state.schedule = sb.move_division_to_ring(
                schedule, chosen_div, source_ring, dest_ring, position=position_arg,
            )
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

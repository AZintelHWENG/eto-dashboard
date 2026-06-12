"""
Read-only schedule viewer — single-file Streamlit app for deployment to
Streamlit Community Cloud.

Repo layout for deployment:
    streamlit_viewer.py     <- this file (set as the app entry point)
    registrations.csv       <- needed only if schedule_state.json is missing
    schedule_state.json     <- the schedule data the viewer renders
    requirements.txt        <- streamlit, pandas
    .streamlit/secrets.toml <- contains the shared viewer password (NOT committed)

Auth model:
    Single shared password defined in st.secrets["viewer"]["password"].
    Anyone with the URL + password can read; nobody can edit.

Live updates:
    The viewer reads schedule_state.json from the deployed repo. To refresh
    what's shown, push a new schedule_state.json to GitHub — Streamlit Cloud
    redeploys automatically. For TRUE live sync between a running simulator
    on your laptop and this hosted viewer, you'd need an external store
    (Firebase/Supabase/etc.) — that's a bigger refactor we can do later.
"""

import json
import secrets as _secrets
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st


APP_DIR = Path(__file__).parent
STATE_PATH = APP_DIR / "schedule_state.json"

# When deployed to Streamlit Cloud, sim_state.json doesn't ride along with the
# repo (the simulator runs locally). We still try to load it in case someone
# commits one for testing.
SIM_STATE_PATH = APP_DIR / "sim_state.json"

ALL_RINGS = [
    "Sanda Ring", "Open Mat", "Lion Dance Stage",
    "Ring 1", "Ring 2", "Ring 3", "Ring 4", "Ring 5",
]
EVENT_DAYS = ["Saturday", "Sunday"]
RING_SLOT_MINUTES = {"Lion Dance Stage": 15}
DEFAULT_SLOT = 5

st.set_page_config(
    page_title="Terry's Event Schedule — Live View",
    page_icon="📋",
    layout="wide",
)


# ---------- Auth (shared password from st.secrets) ----------
def _viewer_password():
    try:
        return st.secrets["viewer"]["password"]
    except (FileNotFoundError, KeyError, AttributeError):
        return None


def _render_login(expected: str):
    st.title("🔐 Sign in")
    st.caption("This dashboard is read-only and requires the shared viewer password.")
    with st.form("login_form"):
        p = st.text_input("Password", type="password")
        ok = st.form_submit_button("Sign in", type="primary")
    if ok:
        # Constant-time compare to avoid leaking the right password through timing.
        if _secrets.compare_digest(p, expected):
            st.session_state.auth_ok = True
            st.rerun()
        else:
            st.error("Wrong password.")


expected_pw = _viewer_password()
if expected_pw is None:
    st.error(
        "No viewer password configured. Add this to your `.streamlit/secrets.toml` "
        "(or the secrets pane on Streamlit Community Cloud):\n\n"
        "```\n[viewer]\npassword = \"your-shared-password-here\"\n```"
    )
    st.stop()
if not st.session_state.get("auth_ok"):
    _render_login(expected_pw)
    st.stop()


# ---------- Helpers ----------
def slot_for_ring(ring):
    return RING_SLOT_MINUTES.get(ring, DEFAULT_SLOT)


def division_colors(divisions_in_order):
    """Light pastel hex per division so adjacent divisions look distinct."""
    palette = [
        "#FFE5B4", "#B4E5FF", "#FFB4B4", "#FFFFB4", "#E5B4FF",
        "#FFC8B4", "#B4FFE5", "#FFB4E5", "#C8FFB4", "#B4C8FF",
    ]
    out, n = {}, len(palette)
    for i, d in enumerate(divisions_in_order):
        out[d] = palette[(i * 3) % n]
    return out


def _hhmm_to_min(t):
    try:
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _min_to_hhmm(m):
    return f"{int(m // 60) % 24:02d}:{int(m % 60):02d}"


def _file_mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


# IMPORTANT: cache-key arg name must NOT start with underscore (Streamlit treats
# leading-underscore params as unhashable so the cache would never invalidate).
@st.cache_data(show_spinner=False)
def _load_schedule(mtime):
    if not STATE_PATH.exists():
        return pd.DataFrame()
    with open(STATE_PATH, "r", encoding="utf-8") as f:
        records = json.load(f)
    df = pd.DataFrame(records)
    if "score" not in df.columns:
        df["score"] = ""
    else:
        df["score"] = df["score"].fillna("")
    if "age_group" not in df.columns:
        df["age_group"] = ""
    return df


@st.cache_data(show_spinner=False)
def _load_sim_state(mtime):
    if not SIM_STATE_PATH.exists():
        return None
    with open(SIM_STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Live time projection (mirrors main app's apply_sim_time_overrides) ----------
def projected_overrides(schedule, sim_state):
    if not sim_state or sim_state.get("sim_wall_start") is None:
        return {}

    sim_running = sim_state.get("sim_running", False)
    sim_speed = sim_state.get("sim_speed", 1)
    paused_offset = sim_state.get("sim_paused_offset", 0)
    sim_wall_start = sim_state.get("sim_wall_start")
    if sim_running and sim_wall_start is not None:
        elapsed_real = time.time() - float(sim_wall_start)
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

    sim_ring_state = sim_state.get("sim_ring_state", {})
    overrides = {}

    for ring in ALL_RINGS:
        if ring not in sim_ring_state:
            continue
        rs = sim_ring_state[ring]
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


def apply_overrides(view_df, overrides):
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


# ---------- Render ----------
header_l, header_r = st.columns([6, 1])
with header_l:
    st.title("📋 Terry's Event Schedule — Live View")
with header_r:
    if st.button("Sign out", key="logout"):
        st.session_state.auth_ok = False
        st.rerun()

st.caption(
    "Read-only mirror of the master schedule. "
    "Times update live when a sim_state.json is present; otherwise scheduled times are shown."
)

schedule_mtime = _file_mtime(STATE_PATH)
sim_mtime = _file_mtime(SIM_STATE_PATH)

if schedule_mtime == 0:
    st.error(
        f"No schedule file found at `{STATE_PATH}`. "
        "The host needs to commit `schedule_state.json` to the deployed repo."
    )
    st.stop()


@st.fragment(run_every="2s")
def _live_view():
    sched_mt = _file_mtime(STATE_PATH)
    sim_mt = _file_mtime(SIM_STATE_PATH)
    sched = _load_schedule(sched_mt)
    sim = _load_sim_state(sim_mt)

    overrides = projected_overrides(sched, sim)
    live_df = apply_overrides(sched, overrides)

    # Status banner
    sb_cols = st.columns([2, 2, 4])
    with sb_cols[0]:
        if sim and sim.get("sim_running"):
            st.markdown("🟢 **Simulator: RUNNING**")
        elif sim and sim.get("sim_wall_start") is not None:
            st.markdown("🟡 **Simulator: PAUSED**")
        else:
            st.markdown("⚪ **Simulator: IDLE**")
    with sb_cols[1]:
        sched_age = datetime.fromtimestamp(sched_mt).strftime("%H:%M:%S") if sched_mt else "—"
        sim_age = datetime.fromtimestamp(sim_mt).strftime("%H:%M:%S") if sim_mt else "—"
        st.caption(f"Schedule: {sched_age} · Sim: {sim_age}")
    with sb_cols[2]:
        st.caption("⏱️ Auto-refreshing every 2s")

    # Filters
    fcol1, fcol2 = st.columns([1, 3])
    with fcol1:
        day_filter = st.selectbox("Day", ["Both", "Saturday", "Sunday"], key="vw_day")
    with fcol2:
        ring_filter = st.multiselect(
            "Rings", ALL_RINGS, default=ALL_RINGS, key="vw_rings"
        )

    view_df = live_df
    if day_filter != "Both":
        view_df = view_df[view_df["day"] == day_filter]
    if ring_filter:
        view_df = view_df[view_df["ring"].isin(ring_filter)]
    view_df = view_df.sort_values(["day", "ring", "order_in_ring"]).reset_index(drop=True)

    color_map = {}
    for ring in ALL_RINGS:
        ring_view = sched[sched["ring"] == ring].sort_values("order_in_ring")
        seen = []
        for d in ring_view["division"]:
            if d not in seen:
                seen.append(d)
        ring_colors = division_colors(seen)
        for div, color in ring_colors.items():
            color_map[(ring, div)] = color

    display_cols = [
        "day", "ring", "start_time", "end_time", "athlete",
        "age_group", "school", "event_category", "division", "score",
    ]
    for c in display_cols:
        if c not in view_df.columns:
            view_df = view_df.assign(**{c: ""})
    display_df = view_df[display_cols].rename(columns={
        "day": "Day", "ring": "Ring", "start_time": "Start", "end_time": "End",
        "athlete": "Athlete", "age_group": "Age Group", "school": "School",
        "event_category": "Event", "division": "Division", "score": "Score",
    })

    def _row_style(row):
        color = color_map.get((row["Ring"], row["Division"]), "#FFFFFF")
        return [f"background-color: {color}; color: #000000"] * len(row)

    styled = display_df.style.apply(_row_style, axis=1)
    st.caption(
        "✅ done · 🟢 in progress · 📝 awaiting score · ⏳ ready · ❌ absent · "
        "no marker = future projection"
    )
    st.dataframe(styled, width="stretch", hide_index=True, height=720)


_live_view()

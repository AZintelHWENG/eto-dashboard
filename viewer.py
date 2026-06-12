"""
Read-only schedule viewer.

Run with:
    streamlit run c:/Claude/PWN_2026_schedule/viewer.py --server.port 8510

Polls schedule_state.json (the master schedule) and sim_state.json (live
simulator state) every ~2 seconds and renders a read-only Schedule View
with live time projections — same per-row markers as the main dashboard
(✅ done · 🟢 in progress · 📝 awaiting score · ⏳ ready · ❌ absent).

This dashboard is intentionally read-only. Edits happen on the main app.
"""

import hashlib
import json
import os
import re
import secrets
import smtplib
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import pandas as pd
import streamlit as st

import schedule_builder as sb


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CODE_TTL_SECONDS = 15 * 60  # confirmation codes expire after 15 minutes


def _send_confirmation_email(to_email: str, code: str) -> tuple[bool, str]:
    """
    Try to send the confirmation code via Gmail SMTP using credentials stored
    in .streamlit/secrets.toml under the [smtp] table:
        [smtp]
        host = "smtp.gmail.com"
        port = 465
        user = "you@gmail.com"
        password = "<16-char Gmail App Password>"
        from_name = "Terry's Event Schedule"  # optional

    Returns (sent_via_email: bool, message: str). If SMTP isn't configured we
    return (False, ...) so the caller can show the code on-screen instead —
    that keeps the viewer usable on first launch before secrets are wired up.
    """
    try:
        smtp_cfg = st.secrets.get("smtp")
    except (FileNotFoundError, AttributeError):
        smtp_cfg = None
    if not smtp_cfg or not smtp_cfg.get("user") or not smtp_cfg.get("password"):
        return (False, "SMTP not configured — code will be shown on screen.")

    host = smtp_cfg.get("host", "smtp.gmail.com")
    port = int(smtp_cfg.get("port", 465))
    user = smtp_cfg["user"]
    password = smtp_cfg["password"]
    from_name = smtp_cfg.get("from_name", "Terry's Event Schedule")

    msg = EmailMessage()
    msg["Subject"] = "Your viewer dashboard confirmation code"
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = to_email
    msg.set_content(
        f"Your confirmation code is: {code}\n\n"
        f"Enter this code in the dashboard to finish creating your account.\n"
        f"It expires in 15 minutes.\n"
    )

    try:
        ctx = ssl.create_default_context()
        if port == 465:
            # Implicit SSL (SMTPS). Often blocked by corporate firewalls.
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=20) as srv:
                srv.login(user, password)
                srv.send_message(msg)
        else:
            # STARTTLS (port 587 by convention). Plays nicer with proxies.
            with smtplib.SMTP(host, port, timeout=20) as srv:
                srv.ehlo()
                srv.starttls(context=ctx)
                srv.ehlo()
                srv.login(user, password)
                srv.send_message(msg)
        return (True, f"Confirmation code sent to {to_email}.")
    except Exception as e:
        return (False, f"Email send failed ({type(e).__name__}: {e}). Code will be shown on screen instead.")


APP_DIR = Path(__file__).parent
STATE_PATH = APP_DIR / "schedule_state.json"
SIM_STATE_PATH = APP_DIR / "sim_state.json"
AUTH_PATH = APP_DIR / "auth.json"  # local-only credentials store

POLL_SECONDS = 2  # how often the live panels refresh

# PBKDF2-HMAC-SHA256 settings. ~310k iterations matches modern OWASP guidance
# and runs imperceptibly on a single login.
PBKDF2_ITERATIONS = 310_000
PBKDF2_HASH = "sha256"

st.set_page_config(
    page_title="Terry's Event Schedule — Live View",
    page_icon="📋",
    layout="wide",
)


# ---------- Auth helpers ----------
def _hash_password(password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_HASH, password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return digest.hex()


def _load_auth():
    """Return the auth record dict, or None if no account is set up yet."""
    if not AUTH_PATH.exists():
        return None
    try:
        with open(AUTH_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_auth(username: str, password: str, email: str = ""):
    salt = secrets.token_bytes(16)
    record = {
        "username": username,
        "email": email,
        "email_verified_at": datetime.utcnow().isoformat() + "Z" if email else "",
        "salt_hex": salt.hex(),
        "password_hash": _hash_password(password, salt),
        "iterations": PBKDF2_ITERATIONS,
        "hash": PBKDF2_HASH,
    }
    with open(AUTH_PATH, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    try:
        os.chmod(AUTH_PATH, 0o600)
    except OSError:
        pass


def _verify(username: str, password: str, record: dict) -> bool:
    if not record:
        return False
    if record.get("username", "").strip().lower() != username.strip().lower():
        return False
    salt = bytes.fromhex(record["salt_hex"])
    candidate = _hash_password(password, salt)
    # Constant-time comparison to avoid timing leaks on the hash.
    return secrets.compare_digest(candidate, record["password_hash"])


def _render_setup():
    st.title("🔐 First-time setup")
    st.caption(
        "No account exists yet. Create a username, email, and password. "
        "We'll send a 6-digit code to your email to confirm it."
    )

    # Stage 1: collect details and trigger a confirmation email.
    pending = st.session_state.get("setup_pending")

    if not pending:
        show_pw = st.checkbox("Show password", key="setup_show_pw")
        pw_type = "default" if show_pw else "password"
        with st.form("setup_form", clear_on_submit=False):
            u = st.text_input("Username", placeholder="e.g. terry")
            e = st.text_input("Email", placeholder="you@example.com")
            p1 = st.text_input("Password", type=pw_type)
            p2 = st.text_input("Confirm password", type=pw_type)
            submitted = st.form_submit_button("Send confirmation code", type="primary")
        if submitted:
            if not u.strip():
                st.error("Username is required.")
            elif not EMAIL_RE.match(e.strip()):
                st.error("Please enter a valid email address.")
            elif len(p1) < 6:
                st.error("Password must be at least 6 characters.")
            elif p1 != p2:
                st.error("Passwords do not match.")
            else:
                code = f"{secrets.randbelow(1_000_000):06d}"
                sent, msg = _send_confirmation_email(e.strip(), code)
                st.session_state.setup_pending = {
                    "username": u.strip(),
                    "email": e.strip(),
                    "password": p1,
                    "code": code,
                    "expires_at": time.time() + CODE_TTL_SECONDS,
                    "delivery": "email" if sent else "onscreen",
                    "delivery_msg": msg,
                }
                st.rerun()
        return

    # Stage 2: enter the confirmation code.
    if time.time() > pending["expires_at"]:
        st.error("That confirmation code expired. Please start over.")
        if st.button("Start over"):
            st.session_state.pop("setup_pending", None)
            st.rerun()
        return

    if pending["delivery"] == "email":
        st.success(pending["delivery_msg"])
    else:
        st.warning(pending["delivery_msg"])
        st.info(f"Confirmation code: **{pending['code']}** (valid for 15 minutes)")

    with st.form("setup_confirm_form"):
        st.write(f"We sent a 6-digit code to **{pending['email']}**.")
        code_in = st.text_input("Enter the code", placeholder="123456", max_chars=6)
        c1, c2 = st.columns([1, 1])
        with c1:
            confirm_btn = st.form_submit_button("Confirm and create account", type="primary")
        with c2:
            cancel_btn = st.form_submit_button("Cancel and start over")

    if cancel_btn:
        st.session_state.pop("setup_pending", None)
        st.rerun()

    if confirm_btn:
        if not secrets.compare_digest(code_in.strip(), pending["code"]):
            st.error("That code doesn't match. Try again or cancel and start over.")
        else:
            _save_auth(pending["username"], pending["password"], pending["email"])
            st.session_state.pop("setup_pending", None)
            st.session_state.pop("auth_user", None)
            st.success("Account created and email confirmed. Please sign in.")
            st.rerun()


def _render_login(record: dict):
    st.title("🔐 Sign in")
    st.caption("This dashboard is read-only and requires authentication.")
    show_pw = st.checkbox("Show password", key="login_show_pw")
    pw_type = "default" if show_pw else "password"
    with st.form("login_form", clear_on_submit=False):
        u = st.text_input("Username")
        p = st.text_input("Password", type=pw_type)
        submitted = st.form_submit_button("Sign in", type="primary")
    if submitted:
        if _verify(u, p, record):
            st.session_state.auth_user = record["username"]
            st.rerun()
        else:
            st.error("Invalid username or password.")

    # Recovery path: lets the user wipe auth.json and start over without
    # needing to touch the filesystem. Two-step click+confirm so it can't
    # be triggered accidentally.
    with st.expander("Forgot your password? Reset account"):
        st.warning(
            "This deletes the saved credentials and returns the dashboard to "
            "the first-time setup screen. There's no way to recover the old account."
        )
        confirm = st.text_input(
            "Type RESET to confirm",
            key="auth_reset_confirm",
            placeholder="RESET",
        )
        if st.button("🗑️ Reset account", disabled=(confirm.strip().upper() != "RESET")):
            try:
                AUTH_PATH.unlink()
            except OSError as e:
                st.error(f"Couldn't remove auth.json: {e}")
            else:
                st.session_state.pop("auth_user", None)
                st.session_state.pop("auth_reset_confirm", None)
                st.rerun()


# ---------- Auth gate ----------
auth_record = _load_auth()
if auth_record is None:
    _render_setup()
    st.stop()
if "auth_user" not in st.session_state:
    _render_login(auth_record)
    st.stop()


# ---------- Loaders (cached so we only re-parse on file mtime change) ----------
# IMPORTANT: do NOT name the cache-key arg with a leading underscore — Streamlit
# treats `_foo` parameters as "do not hash", so the cache would never invalidate.
@st.cache_data(show_spinner=False)
def _load_schedule(mtime):
    """Cache key is the file's mtime — cache invalidates when the main app saves."""
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
        df["age_group"] = df.get("dob", pd.Series([""] * len(df))).apply(sb.compute_age_group)
    return df


@st.cache_data(show_spinner=False)
def _load_sim_state(mtime):
    if not SIM_STATE_PATH.exists():
        return None
    with open(SIM_STATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _file_mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0


# ---------- Time helpers ----------
def _hhmm_to_min(t):
    try:
        h, m = str(t).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _min_to_hhmm(m):
    return f"{int(m // 60) % 24:02d}:{int(m % 60):02d}"


# ---------- Projection (mirrors app.py's apply_sim_time_overrides) ----------
def projected_overrides(schedule, sim_state):
    """Walk each ring once and project actual + projected times for the live day.
    sim_state is the dict loaded from sim_state.json (or None when idle).
    Returns {entry_id: (proj_start_min, proj_end_min, marker)}.
    """
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

    for ring in sb.ALL_RINGS:
        if ring not in sim_ring_state:
            continue
        rs = sim_ring_state[ring]
        ring_slot = sb.slot_for_ring(ring)
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

            # Only bridge real structural gaps (lunch ≥ 30 min). Smaller gaps
            # are accumulated drift from absences/early finishes and must
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
    st.caption(f"Signed in as **{st.session_state.auth_user}**")
    if st.button("Sign out", key="logout"):
        st.session_state.pop("auth_user", None)
        st.rerun()

st.caption(
    "Read-only mirror of the main dashboard. Times update live as the simulator runs. "
    "To make edits, switch to the main dashboard."
)

# Load both files using their mtimes as cache keys.
schedule_mtime = _file_mtime(STATE_PATH)
sim_mtime = _file_mtime(SIM_STATE_PATH)

if schedule_mtime == 0:
    st.error(
        f"No schedule file found at `{STATE_PATH}`. "
        "Open the main dashboard first so it can build and save the schedule."
    )
    st.stop()

schedule = _load_schedule(schedule_mtime)
sim_state = _load_sim_state(sim_mtime)


# ---------- Status banner ----------
status_cols = st.columns([2, 2, 4])
with status_cols[0]:
    if sim_state and sim_state.get("sim_running"):
        st.markdown("🟢 **Simulator: RUNNING**")
    elif sim_state and sim_state.get("sim_wall_start") is not None:
        st.markdown("🟡 **Simulator: PAUSED**")
    else:
        st.markdown("⚪ **Simulator: IDLE**")
with status_cols[1]:
    sched_age = datetime.fromtimestamp(schedule_mtime).strftime("%H:%M:%S")
    sim_age = datetime.fromtimestamp(sim_mtime).strftime("%H:%M:%S") if sim_mtime else "—"
    st.caption(f"Schedule saved {sched_age} · Sim saved {sim_age}")
with status_cols[2]:
    st.caption(f"⏱️ Auto-refreshing every {POLL_SECONDS}s")


# ---------- Live grid wrapped in a fragment ----------
@st.fragment(run_every=f"{POLL_SECONDS}s")
def _live_view():
    sched_mt = _file_mtime(STATE_PATH)
    sim_mt = _file_mtime(SIM_STATE_PATH)
    sched = _load_schedule(sched_mt)
    sim = _load_sim_state(sim_mt)

    overrides = projected_overrides(sched, sim)
    live_df = apply_overrides(sched, overrides)

    # Filters (these live INSIDE the fragment so the dropdown values persist
    # across polls but the grid still re-renders).
    fcol1, fcol2 = st.columns([1, 3])
    with fcol1:
        day_filter = st.selectbox("Day", ["Both", "Saturday", "Sunday"], key="vw_day")
    with fcol2:
        ring_filter = st.multiselect(
            "Rings",
            sb.ALL_RINGS,
            default=sb.ALL_RINGS,
            key="vw_rings",
        )

    view_df = live_df
    if day_filter != "Both":
        view_df = view_df[view_df["day"] == day_filter]
    if ring_filter:
        view_df = view_df[view_df["ring"].isin(ring_filter)]
    view_df = view_df.sort_values(["day", "ring", "order_in_ring"]).reset_index(drop=True)

    # Per-(ring, division) color map — same logic as app.py.
    color_map = {}
    for ring in sb.ALL_RINGS:
        ring_view = sched[sched["ring"] == ring].sort_values("order_in_ring")
        seen = []
        for d in ring_view["division"]:
            if d not in seen:
                seen.append(d)
        ring_colors = sb.division_colors(seen)
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

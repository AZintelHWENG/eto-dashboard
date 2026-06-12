# Deploying the read-only viewer to Streamlit Community Cloud

## What gets deployed
- `streamlit_viewer.py` — single-file viewer (the entry point)
- `schedule_state.json` — the schedule data the viewer renders
- `requirements.txt` — Python dependencies
- `registrations.csv` — only needed if `schedule_state.json` is ever missing

The viewer is **read-only** and **password-gated**. Anyone with the URL plus
the shared password can watch.

## Important caveat: "live" updates

The viewer reads the `schedule_state.json` that's committed in your GitHub
repo at deploy time. To update what people see, you have to push a new
`schedule_state.json`. There is no live link from the simulator running on
your laptop to the public viewer — the cloud server cannot read your
laptop's filesystem.

If you want true live sync (laptop simulator → public viewer), we have to
add an external store (Firebase/Supabase/etc.). That's a follow-up project.

## Steps

1. **Make sure the repo has these files at the root:**
   - `streamlit_viewer.py`
   - `schedule_state.json` (commit a snapshot — re-commit when it changes)
   - `requirements.txt` (must include at least `streamlit>=1.37` and `pandas>=2.0`)

2. **Push the repo to GitHub** (public or private — Streamlit Cloud supports both).

3. **Go to https://share.streamlit.io** and click "New app".
   - Repository: your repo
   - Branch: `main`
   - Main file path: `streamlit_viewer.py`
   - App URL: pick something memorable (e.g. `pwn-schedule-view`)

4. **Add the shared password to the app's Secrets pane** (Settings → Secrets):
   ```toml
   [viewer]
   password = "pick-a-good-password-here"
   ```
   Save. Streamlit Cloud injects this into `st.secrets` at runtime — it does
   NOT live in the GitHub repo.

5. **Deploy.** First boot takes ~1-2 minutes. Visit the public URL, enter the
   password, see the schedule.

## Updating the schedule after deploy

Two paths:

- **Manual:** when you want viewers to see updated times, commit & push a
  fresh `schedule_state.json`. Streamlit Cloud auto-redeploys on push (~1 min).
- **Local network:** keep using `viewer.py` on `localhost:8510` for
  on-the-day live viewing on your venue WiFi. Use the public viewer for
  pre-event preview / post-event summary.

## Security notes

- `auth.json` from the local viewer is NOT used here. The cloud version
  uses a single shared password from `st.secrets`. Anyone with that password
  can read everything.
- To rotate, change the password in the Secrets pane — takes effect on next
  app reboot.
- The viewer renders athlete names, schools, DOBs (if present), and scores.
  Make sure that's appropriate before sharing the URL publicly.

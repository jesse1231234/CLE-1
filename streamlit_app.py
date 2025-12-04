import time
import json
import requests
import streamlit as st

st.set_page_config(page_title="Course Load Estimator", page_icon="📚", layout="wide")

st.title("📚 Course Load Estimator (Streamlit Client)")
st.caption("Front-end for the FastAPI/Celery backend described in SPEC-1 — run scans, enter video durations, and monitor progress.")

# --- Sidebar configuration
with st.sidebar:
    st.header("Configuration")
    api_base = st.text_input("API Base URL", value=st.session_state.get("api_base", "http://localhost:8080"))
    st.session_state["api_base"] = api_base

    st.subheader("Learner Level")
    level = st.radio("Level affects reading-speed defaults", ["Undergraduate", "Graduate"], index=0, horizontal=True)
    st.session_state["level"] = level

    st.divider()
    st.markdown("**Health Check**")
    if st.button("Ping /health"):
        try:
            r = requests.get(f"{api_base}/health", timeout=5)
            st.success(f"Health: {r.json()}")
        except Exception as e:
            st.error(f"Health check failed: {e}")

# --- Start a run
st.header("1) Start a Course Scan")
col1, col2, col3 = st.columns([2,1,1])
with col1:
    course_id = st.number_input("Canvas Course ID", min_value=1, step=1, value=12345)
with col2:
    start_run = st.button("🚀 Start Run")
with col3:
    auto_poll = st.toggle("Auto-poll progress", value=True)

if start_run:
    try:
        payload = {"course_id": int(course_id)}
        r = requests.post(f"{api_base}/runs/start", json=payload, timeout=15)
        r.raise_for_status()
        run_id = r.json().get("run_id")
        st.session_state["run_id"] = run_id
        st.success(f"Run started: {run_id}")
    except Exception as e:
        st.error(f"Failed to start run: {e}")

# --- Progress
st.header("2) Run Progress")
run_id = st.session_state.get("run_id", "")
if run_id:
    colp1, colp2 = st.columns([2,1])
    with colp1:
        if st.button("🔄 Refresh Status") or auto_poll:
            try:
                r = requests.get(f"{api_base}/runs/{run_id}", timeout=10)
                data = r.json()
                st.session_state["run_status"] = data
            except Exception as e:
                st.error(f"Failed to get status: {e}")
                st.stop()

        data = st.session_state.get("run_status", {})
        state = data.get("state", "PENDING")
        progress = float(data.get("progress", 0.0))
        message = data.get("message", "")
        st.progress(min(max(progress, 0.0), 1.0), text=f"{state} — {message}")
        st.code(json.dumps(data, indent=2), language="json")
    with colp2:
        st.metric("State", state)
        st.metric("Progress", f"{progress*100:.0f}%")
        st.metric("Message", message or "-")
else:
    st.info("Start a run to see progress.")

# --- Manual Video Durations Inbox
st.header("3) Video Duration Inbox")
st.caption("Enter durations for detected videos (hh:mm:ss). The backend stores and reuses these values on future scans.")
if st.button("🔍 Check Pending Videos"):
    try:
        r = requests.get(f"{api_base}/videos/pending", timeout=15)
        r.raise_for_status()
        st.session_state["pending_videos"] = r.json()
    except Exception as e:
        st.error(f"Failed to load pending videos: {e}")

pending = st.session_state.get("pending_videos", [])
if pending:
    for video in pending:
        with st.expander(f"{video.get('title','(untitled)')} — id {video.get('item_id')}"):
            vcols = st.columns([2,1,1])
            with vcols[0]:
                st.write(video)
            with vcols[1]:
                hhmmss = st.text_input("Duration (hh:mm:ss)", key=f"dur_{video['item_id']}", value="00:10:00")
            with vcols[2]:
                if st.button("💾 Save Duration", key=f"save_{video['item_id']}"):
                    try:
                        r = requests.post(f"{api_base}/videos/{video['item_id']}/duration",
                                          json={"hhmmss": hhmmss}, timeout=15)
                        r.raise_for_status()
                        st.success(f"Saved duration for item {video['item_id']}")
                    except Exception as e:
                        st.error(f"Failed to save duration: {e}")
else:
    st.info("No pending videos (or you haven't pressed 'Check Pending Videos' yet).")

# --- Exports
st.header("4) Exports")
colx1, colx2 = st.columns(2)
with colx1:
    st.caption("Download CSV export for a course")
    export_course_id = st.number_input("Course ID for export (CSV/JSON)", min_value=1, step=1, value=int(course_id))
    if st.button("⬇️ Download CSV"):
        try:
            url = f"{api_base}/exports/courses/{export_course_id}.csv"
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                st.download_button("Save CSV", r.content, file_name=f"course_{export_course_id}.csv", mime="text/csv")
            else:
                st.warning(f"Export not implemented or not found (HTTP {r.status_code}).")
        except Exception as e:
            st.error(f"CSV export failed: {e}")
with colx2:
    if st.button("⬇️ Download JSON"):
        try:
            url = f"{api_base}/exports/courses/{export_course_id}.json"
            r = requests.get(url, timeout=30)
            if r.status_code == 200:
                st.download_button("Save JSON", r.content, file_name=f"course_{export_course_id}.json", mime="application/json")
            else:
                st.warning(f"Export not implemented or not found (HTTP {r.status_code}).")
        except Exception as e:
            st.error(f"JSON export failed: {e}")

st.divider()
st.caption("Tip: Adjust the UG/Grad level in the sidebar; future versions will send this to the backend assumptions API.")
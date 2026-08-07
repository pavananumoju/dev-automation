#!/usr/bin/env python3
"""
Dashboard
=========

A local, browser-based status/control panel over state_store.py and each
tracked project's own logs/AUDIT.md -- so you can check status, read
recent session output, and kick off or resume a run without a terminal.

This is the one piece of this whole tool suite that isn't Python-standard-
-library-only: it needs Streamlit installed (`pip install streamlit`, one
time). Everything else -- audit_generator.py, engine_registry.py,
audit_compiler.py, state_store.py, pipeline.py -- stays dependency-free on
purpose; only this optional, purely-visual layer trades that off for a
real UI. If you never install Streamlit, every other tool in this suite
still works exactly the same from the terminal.

Run with:
    streamlit run app.py

This dashboard only ever launches pipeline.py as a background subprocess
(never calls into audit_generator.py/pipeline.py's Python functions
directly) -- so a long-running audit doesn't block the browser tab, and a
page refresh never loses progress. It reads state_store.py and log files;
it doesn't hold any state of its own.
"""

import subprocess
import sys
import time
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "pipeline-src"))
sys.path.insert(0, str(REPO_ROOT / "audit-generator-src"))
sys.path.insert(0, str(REPO_ROOT / "audit-fix-runner-src"))

import state_store as ss  # noqa: E402
import audit_fix_runner as afr  # noqa: E402  (reused only for its table parser -- see view_audit_table())

PIPELINE_PATH = REPO_ROOT / "pipeline-src" / "pipeline.py"

STATUS_COLORS = {
    ss.STATUS_RUNNING: "blue",
    ss.STATUS_PAUSED_QUOTA: "orange",
    ss.STATUS_PAUSED_REVIEW: "orange",
    ss.STATUS_COMPLETED: "green",
    ss.STATUS_FAILED: "red",
    ss.STATUS_IDLE: "gray",
}


# ======================================================================
# HELPERS
# ======================================================================

def most_recent_log_tail(project_path, max_lines=60):
    logs_folder = Path(project_path) / "logs"
    if not logs_folder.is_dir():
        return None, []
    log_files = sorted(logs_folder.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not log_files:
        return None, []
    newest = log_files[0]
    try:
        lines = newest.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return newest, []
    return newest, lines[-max_lines:]


def view_audit_table(project_path):
    """Reuse audit_fix_runner.py's own table parser -- same source of truth the fixer itself uses, no re-implementation."""
    audit_path = Path(project_path) / "AUDIT.md"
    if not audit_path.is_file():
        return None
    try:
        rows = afr.parse_master_tracking_table(audit_path)
    except SystemExit:
        return None
    return rows


def launch_pipeline_background(args_list):
    """Start pipeline.py as a detached background subprocess. Never blocks the dashboard -- progress shows up in state_store on the next refresh."""
    command = [sys.executable, str(PIPELINE_PATH)] + args_list
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


# ======================================================================
# PAGE
# ======================================================================

st.set_page_config(page_title="Dev Automation Pipeline", layout="wide")
st.title("Dev Automation Pipeline")

with st.sidebar:
    st.header("Controls")
    if st.button("Run --check-resume now", help="Resume every due, auto-resume-enabled paused project right now, instead of waiting for the next scheduled ping."):
        launch_pipeline_background(["--check-resume"])
        st.success("Started in the background -- refresh in a few seconds to see updated status.")

    st.divider()
    st.subheader("Start a new project")
    with st.form("new_project_form"):
        new_project_path = st.text_input("Project path", placeholder="/Users/you/code/my-app")
        new_test_cmd = st.text_input("Test command (optional)", placeholder="npm test", help="Leave blank to stop after AUDIT.md is generated, without running the fixer/verifier.")
        new_review_required = st.checkbox("--review-required", value=True)
        new_auto_resume = st.checkbox("--auto-resume", value=True)
        new_auto_push = st.checkbox("--auto-push", value=False)
        submitted = st.form_submit_button("Launch")
        if submitted:
            if not new_project_path.strip():
                st.error("Project path is required.")
            elif not Path(new_project_path).expanduser().is_dir():
                st.error(f"Not a folder: {new_project_path}")
            else:
                launch_args = ["--project", new_project_path.strip()]
                if new_test_cmd.strip():
                    launch_args += ["--test-cmd", new_test_cmd.strip()]
                if new_review_required:
                    launch_args.append("--review-required")
                if new_auto_resume:
                    launch_args.append("--auto-resume")
                if new_auto_push:
                    launch_args.append("--auto-push")
                launch_pipeline_background(launch_args)
                st.success("Launched in the background -- it'll appear below once it starts writing state.")

    st.divider()
    auto_refresh = st.checkbox("Auto-refresh every 15s", value=False)

states = ss.list_states()

if not states:
    st.info("No projects tracked yet. Launch one from the sidebar, or run pipeline.py / audit_generator.py from the terminal against a project.")
else:
    st.subheader(f"{len(states)} tracked project(s)")
    overview_rows = [
        {
            "Project": state["project_path"],
            "Status": state["status"],
            "Stage": state["current_stage"] or "",
            "Last updated (UTC)": state["last_updated"],
        }
        for state in states
    ]
    st.dataframe(overview_rows, use_container_width=True, hide_index=True)

    for state in states:
        color = STATUS_COLORS.get(state["status"], "gray")
        with st.expander(f":{color}[{state['status']}]  —  {state['project_path']}"):
            col1, col2 = st.columns(2)
            with col1:
                st.write("**Stage:**", state["current_stage"] or "(none)")
                st.write("**Last updated (UTC):**", state["last_updated"])
                if state["status"] in (ss.STATUS_PAUSED_QUOTA, ss.STATUS_PAUSED_REVIEW):
                    st.write("**Paused reason:**", state["paused_reason"])
                    if state["resume_at"]:
                        st.write("**Resume at (UTC):**", state["resume_at"])
                    st.write("**Auto-resume:**", "on" if state["auto_resume"] else "off (manual re-run needed)")
                if state["status"] == ss.STATUS_FAILED and state["last_error"]:
                    st.error(state["last_error"])
            with col2:
                rows = view_audit_table(state["project_path"])
                if rows is not None:
                    fixed = sum(1 for r in rows if afr.status_is_fixed(r["fix_status"]))
                    blocked = sum(1 for r in rows if afr.status_matches(r["fix_status"], afr.STATUS_BLOCKED))
                    not_started = sum(1 for r in rows if afr.status_matches(r["fix_status"], afr.STATUS_NOT_STARTED))
                    st.write(f"**AUDIT.md:** {len(rows)} finding(s) — {fixed} fixed, {blocked} blocked, {not_started} not started")
                else:
                    st.write("**AUDIT.md:** not generated yet")

            log_path, log_lines = most_recent_log_tail(state["project_path"])
            if log_path:
                st.caption(f"Last {len(log_lines)} line(s) of {log_path.name}")
                st.code("\n".join(log_lines) or "(empty)", language=None)
            else:
                st.caption("No logs/ folder found yet.")

if auto_refresh:
    time.sleep(15)
    st.rerun()

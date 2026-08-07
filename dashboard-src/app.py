#!/usr/bin/env python3
"""
Dashboard
=========

A local, browser-based control panel over pipeline.py's three independent
actions (generate/fix/verify) and state_store.py -- full parameter forms
for each, so starting or resuming a run never means typing a command
line. Every launch is a detached background subprocess (`pipeline.py
<action> ...`), so a long-running audit never blocks the browser tab and
a page refresh never loses progress.

This is the one piece of this whole tool suite that isn't Python-standard-
-library-only: it needs Streamlit installed (`pip install streamlit`, one
time -- see README.md for why a dedicated virtualenv is used). Every other
tool -- audit_generator.py, engine_registry.py, audit_compiler.py,
state_store.py, pipeline.py -- stays dependency-free on purpose.

Run with:
    .venv/bin/streamlit run app.py
"""

import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "pipeline-src"))
sys.path.insert(0, str(REPO_ROOT / "audit-generator-src"))
sys.path.insert(0, str(REPO_ROOT / "audit-fix-runner-src"))

import state_store as ss  # noqa: E402
import audit_generator as ag  # noqa: E402  (reused only for DEFAULT_MAX_TURNS/print_dry_run_plan)
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
        return afr.parse_master_tracking_table(audit_path)
    except SystemExit:
        return None


def launch_pipeline_background(args_list):
    """Start pipeline.py as a detached background subprocess. Never blocks the dashboard -- progress shows up in state_store on the next refresh."""
    command = [sys.executable, str(PIPELINE_PATH)] + args_list
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def run_pipeline_foreground(args_list, timeout=30):
    """Run pipeline.py synchronously and return its combined output -- only for fast, read-only calls like --dry-run."""
    command = [sys.executable, str(PIPELINE_PATH)] + args_list
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return "(timed out -- this should have been a fast, read-only call; something's wrong)"


def build_generate_args(project_path, branch, max_turns, review_required, usage_warn_threshold, hard_pause_threshold, auto_resume, auto_push, dry_run=False):
    args = ["generate", "--project", project_path]
    if branch.strip():
        args += ["--branch", branch.strip()]
    args += ["--max-turns", str(max_turns), "--usage-warn-threshold", str(usage_warn_threshold), "--hard-pause-threshold", str(hard_pause_threshold)]
    if review_required:
        args.append("--review-required")
    if auto_resume:
        args.append("--auto-resume")
    if auto_push:
        args.append("--auto-push")
    if dry_run:
        args.append("--dry-run")
    return args


def build_fixver_args(action, project_path, branch, test_cmd, max_rows, severities, emulator_avd, hard_pause_threshold, auto_resume, auto_push):
    args = [action, "--project", project_path]
    if branch.strip():
        args += ["--branch", branch.strip()]
    args += ["--test-cmd", test_cmd, "--mode", "auto"]  # mode is always "auto" for dashboard launches -- "manual" needs a real terminal to answer its prompts, which a detached background process doesn't have.
    if max_rows:
        args += ["--max-rows", str(max_rows)]
    if severities.strip():
        args += ["--severities", severities.strip()]
    if emulator_avd.strip():
        args += ["--emulator-avd", emulator_avd.strip()]
    args += ["--hard-pause-threshold", str(hard_pause_threshold)]
    if auto_resume:
        args.append("--auto-resume")
    if auto_push:
        args.append("--auto-push")
    return args


# ======================================================================
# PAGE
# ======================================================================

st.set_page_config(page_title="Dev Automation Pipeline", layout="wide")
st.title("Dev Automation Pipeline")

with st.sidebar:
    st.header("Controls")
    if st.button("Run --check-resume now", help="Resume every due, auto-resume-enabled paused project right now, instead of waiting for the next scheduled ping."):
        launch_pipeline_background(["check-resume"])
        st.success("Started in the background -- refresh in a few seconds to see updated status.")
    st.divider()
    auto_refresh = st.checkbox("Auto-refresh every 15s", value=False)

st.subheader("Generate a new audit")
st.caption("Runs Stage 0-3 only -- scans the project, researches online, builds a phased audit prompt, executes it, and compiles AUDIT.md. Stops there; fixing and verifying are separate, run whenever you're ready (below).")
with st.form("generate_form"):
    col1, col2 = st.columns(2)
    with col1:
        gen_project_path = st.text_input("Project path", placeholder="/Users/you/code/my-app")
        gen_branch = st.text_input("Branch", placeholder="(default: audit-gen-<today's date>)")
        gen_max_turns = st.number_input("Max turns per stage/phase session", min_value=1, value=ag.DEFAULT_MAX_TURNS)
    with col2:
        gen_usage_warn = st.number_input("Usage warn threshold (%)", min_value=0, max_value=100, value=90)
        gen_hard_pause = st.number_input("Hard pause threshold (%)", min_value=0, max_value=100, value=97)
    gen_review_required = st.checkbox("--review-required (pause for you to review the audit prompt, and again before compiling)", value=True)
    gen_auto_resume = st.checkbox("--auto-resume (auto-resume on usage-limit pauses once due)", value=True)
    gen_auto_push = st.checkbox("--auto-push", value=False)

    preview_col, launch_col = st.columns(2)
    preview_clicked = preview_col.form_submit_button("Preview (dry-run)")
    launch_clicked = launch_col.form_submit_button("Launch", type="primary")

    if preview_clicked or launch_clicked:
        if not gen_project_path.strip():
            st.error("Project path is required.")
        elif not Path(gen_project_path).expanduser().is_dir():
            st.error(f"Not a folder: {gen_project_path}")
        else:
            args = build_generate_args(
                gen_project_path.strip(), gen_branch, gen_max_turns, gen_review_required,
                gen_usage_warn, gen_hard_pause, gen_auto_resume, gen_auto_push,
                dry_run=preview_clicked,
            )
            if preview_clicked:
                st.code(run_pipeline_foreground(args), language=None)
            else:
                launch_pipeline_background(args)
                st.success("Launched in the background -- it'll appear below once it starts writing state.")

st.divider()

states = ss.list_states()

if not states:
    st.info("No projects tracked yet. Generate one above, or run pipeline.py from the terminal against a project.")
else:
    st.subheader(f"{len(states)} tracked project(s)")
    overview_rows = [
        {
            "Project": state["project_path"],
            "Status": state["status"],
            "Action": state.get("action") or "",
            "Stage": state["current_stage"] or "",
            "Last updated (UTC)": state["last_updated"],
        }
        for state in states
    ]
    st.dataframe(overview_rows, width="stretch", hide_index=True)

    for state in states:
        color = STATUS_COLORS.get(state["status"], "gray")
        project_path = state["project_path"]
        with st.expander(f":{color}[{state['status']}]  —  {project_path}"):
            col1, col2 = st.columns(2)
            with col1:
                st.write("**Action:**", state.get("action") or "(none)")
                st.write("**Stage:**", state["current_stage"] or "(none)")
                st.write("**Last updated (UTC):**", state["last_updated"])
                if state["status"] in (ss.STATUS_PAUSED_QUOTA, ss.STATUS_PAUSED_REVIEW):
                    st.write("**Paused reason:**", state["paused_reason"])
                    if state["resume_at"]:
                        st.write("**Resume at (UTC):**", state["resume_at"])
                    st.write("**Auto-resume:**", "on" if state["auto_resume"] else "off")
                    if st.button("Resume now", key=f"resume_{project_path}", type="primary"):
                        launch_pipeline_background(["resume-project", "--project", project_path])
                        st.success("Resume launched in the background -- refresh in a few seconds.")
                if state["status"] == ss.STATUS_FAILED and state["last_error"]:
                    st.error(state["last_error"])
            with col2:
                rows = view_audit_table(project_path)
                if rows is not None:
                    fixed = sum(1 for r in rows if afr.status_is_fixed(r["fix_status"]))
                    blocked = sum(1 for r in rows if afr.status_matches(r["fix_status"], afr.STATUS_BLOCKED))
                    not_started = sum(1 for r in rows if afr.status_matches(r["fix_status"], afr.STATUS_NOT_STARTED))
                    st.write(f"**AUDIT.md:** {len(rows)} finding(s) — {fixed} fixed, {blocked} blocked, {not_started} not started")
                else:
                    st.write("**AUDIT.md:** not generated yet")

            log_path, log_lines = most_recent_log_tail(project_path)
            if log_path:
                st.caption(f"Last {len(log_lines)} line(s) of {log_path.name}")
                st.code("\n".join(log_lines) or "(empty)", language=None)
            else:
                st.caption("No logs/ folder found yet.")

            audit_exists = (Path(project_path) / "AUDIT.md").is_file()
            if audit_exists:
                last_branch = (state.get("run_args") or {}).get("branch", "")
                fixver_col1, fixver_col2 = st.columns(2)

                with fixver_col1:
                    st.markdown("**Run Fixer**")
                    with st.form(f"fix_form_{project_path}"):
                        fix_branch = st.text_input("Branch", value=last_branch, key=f"fix_branch_{project_path}")
                        fix_test_cmd = st.text_input("Test command", placeholder="npm test", key=f"fix_test_cmd_{project_path}")
                        fix_max_rows = st.number_input("Max rows this run (0 = no limit)", min_value=0, value=0, key=f"fix_max_rows_{project_path}")
                        fix_severities = st.text_input("Severities filter", placeholder="P0,P1", key=f"fix_sev_{project_path}")
                        fix_emulator = st.text_input("Emulator AVD (Android only)", key=f"fix_avd_{project_path}")
                        fix_hard_pause = st.number_input("Hard pause threshold (%)", min_value=0, max_value=100, value=97, key=f"fix_hp_{project_path}")
                        fix_auto_resume = st.checkbox("--auto-resume", value=True, key=f"fix_ar_{project_path}")
                        fix_auto_push = st.checkbox("--auto-push", value=False, key=f"fix_ap_{project_path}")
                        st.caption("Always runs in --mode auto -- manual mode needs a real terminal to answer its prompts.")
                        if st.form_submit_button("Launch Fixer", type="primary"):
                            if not fix_test_cmd.strip():
                                st.error("Test command is required.")
                            else:
                                args = build_fixver_args(
                                    "fix", project_path, fix_branch, fix_test_cmd.strip(),
                                    fix_max_rows or None, fix_severities, fix_emulator,
                                    fix_hard_pause, fix_auto_resume, fix_auto_push,
                                )
                                launch_pipeline_background(args)
                                st.success("Fixer launched in the background.")

                with fixver_col2:
                    st.markdown("**Run Verifier**")
                    with st.form(f"verify_form_{project_path}"):
                        ver_branch = st.text_input("Branch", value=last_branch, key=f"ver_branch_{project_path}")
                        ver_test_cmd = st.text_input("Test command", placeholder="npm test", key=f"ver_test_cmd_{project_path}")
                        ver_max_rows = st.number_input("Max rows this run (0 = no limit)", min_value=0, value=0, key=f"ver_max_rows_{project_path}")
                        ver_severities = st.text_input("Severities filter", placeholder="P0,P1", key=f"ver_sev_{project_path}")
                        ver_emulator = st.text_input("Emulator AVD (Android only)", key=f"ver_avd_{project_path}")
                        ver_hard_pause = st.number_input("Hard pause threshold (%)", min_value=0, max_value=100, value=97, key=f"ver_hp_{project_path}")
                        ver_auto_resume = st.checkbox("--auto-resume", value=True, key=f"ver_ar_{project_path}")
                        ver_auto_push = st.checkbox("--auto-push", value=False, key=f"ver_ap_{project_path}")
                        st.caption("Always runs in --mode auto -- manual mode needs a real terminal to answer its prompts.")
                        if st.form_submit_button("Launch Verifier", type="primary"):
                            if not ver_test_cmd.strip():
                                st.error("Test command is required.")
                            else:
                                args = build_fixver_args(
                                    "verify", project_path, ver_branch, ver_test_cmd.strip(),
                                    ver_max_rows or None, ver_severities, ver_emulator,
                                    ver_hard_pause, ver_auto_resume, ver_auto_push,
                                )
                                launch_pipeline_background(args)
                                st.success("Verifier launched in the background.")
            else:
                st.caption("AUDIT.md not generated yet on this branch -- fixer/verifier forms appear once it exists.")

if auto_refresh:
    time.sleep(15)
    st.rerun()

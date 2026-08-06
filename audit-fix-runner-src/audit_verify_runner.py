#!/usr/bin/env python3
"""
Audit Verify Runner
====================

This tool automates VERIFYING a project's already-fixed AUDIT.md rows --
the companion to audit_fix_runner.py, which implements fixes in the first
place. It never implements a fix from scratch; it confirms one already
implemented actually holds up.

AUDIT.md is expected to contain a "Master Tracking Table": a markdown table
that lists findings/features, one per row, with a "Fix Status" column. This
script finds every row whose status contains "unverified" (e.g.
"Fixed (unverified)", "Partially fixed (unverified)") and works through
them one at a time. For each row, it starts a brand-new, memory-free
headless Claude Code session (`claude -p ...`). That session reads
AUDIT.md itself, confirms the described fix is actually present and
correct in the current source, runs (or writes, if missing) a test for it
-- including an on-device instrumented test on an emulator when the
finding has any on-screen or interactive effect -- and updates its own row
to "Fixed (verified)" if it holds up, or "Blocked" with a clear reason if
it doesn't.

This script is only an orchestrator. It never edits source code and never
edits AUDIT.md itself -- it only reads AUDIT.md to decide what is left to
verify and to report progress. All the real work happens inside each
`claude -p` session.

Because every row gets a completely fresh Claude session, context never
dilutes across a long run of many findings.

Never run this at the same time as audit_fix_runner.py against the same
project -- both touch the same working tree, branch, and commit history.
This script checks for a live audit_fix_runner.py before starting and
refuses to run if one is found; run one at a time.

Example:
    python3 audit_verify_runner.py \\
        --project ~/code/my-app \\
        --test-cmd "npm test" \\
        --mode auto

Run with --help to see every available option.
"""

import argparse
import atexit
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


# ======================================================================
# CONSTANTS
# ======================================================================

# Order in which severities are processed. Anything not in this list is
# processed last, in alphabetical order.
SEVERITY_ORDER = ["P0", "P1", "P2", "P3"]

# Width of the section/row banner boxes drawn by print_banner(), and of the
# full-width dashed separator lines around the PROJECT.md/README.md preview
# -- kept equal so those two visual elements line up with each other.
BANNER_WIDTH = 100

# Matches ANSI terminal escape codes (color codes, cursor movement, etc.)
# so log files stay plain, readable text.
ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

STATUS_NOT_STARTED = "Not started"
STATUS_FIXED = "Fixed"
STATUS_FIXED_VERIFIED = "Fixed (verified)"
STATUS_BLOCKED = "Blocked"


class ClaudeNotFoundError(Exception):
    """
    Raised when the 'claude' command can't be found mid-loop. main() catches
    this specifically (instead of letting a raw sys.exit() skip straight past
    POST-RUN) so any progress already made this run still gets summarized.
    """

# The prompt sent to each row's fresh `claude -p` session. Filled in with
# .format(row_id=..., branch_name=..., test_cmd=...) before use.
PROMPT_TEMPLATE = """Read AUDIT.md at the repo root. Find row {row_id} in the Master Tracking
Table. Its Fix Status is expected to contain "unverified". Read that
finding's full entry in its phase section, and read its current Notes
column carefully -- it describes what a previous session believed it
changed. Do not trust that description blindly: confirm it against the
actual current source, which may have moved or changed since. Do not rely
on any other session's context -- read everything fresh.

You are on git branch {branch_name}. Confirm this with `git branch --show-
current` before doing anything else; if you are not on that branch, stop
and report the mismatch instead of proceeding.

Your job is to VERIFY {row_id}, not to redesign or rewrite its fix.

1. Read every source file the row's Notes/phase entry cites. Confirm the
   described change is actually present and looks correct for the
   finding described. If it is missing, or looks wrong or incomplete, a
   small, obviously-correct completion of the existing fix is fine -- but
   if it needs a real redesign, stop there and report that instead of
   guessing or rewriting it from scratch.

2. Check whether a dedicated test for this fix already exists (the Notes
   column usually names it). If one exists, run it. If it's missing, or
   doesn't actually exercise the behavior the finding describes, write
   one -- a test that would fail against the pre-fix behavior and passes
   now.

3. Run the full test suite with: {test_cmd}
   Every test must pass, not just this row's.

4. If a design-system or 'Approved Values' section exists in AUDIT.md and
   this finding relates to it (colors, spacing, motion, typography,
   etc.), use only those values. Do not invent new ones.

5. Decide whether {row_id}'s finding has any on-screen or interactive
   effect (a color, a layout, an animation, a tap/gesture outcome, text a
   user would see) as opposed to a purely internal change (calculation,
   storage, threading, timing with no visible surface).

   If it's purely internal: on-device verification doesn't apply here --
   say so in one clause in the Notes and rely on the check above.

   If it does have an observable effect, verify it for real on-device:

   a. Run `adb devices -l`. If it lists anything other than exactly one
      emulator (e.g. a real device is attached), skip on-device
      verification, note in AUDIT.md that it was skipped because a real
      device was present, and fall back to the unit-test-only result.
      Never run instrumented tests when a real device is connected.

   b. Write or extend a Compose UI test under app/src/androidTest/...,
      matching the pattern in
      app/src/androidTest/java/com/capitalrecall/app/ui/components/CompletionOverlayTest.kt:
      - Host the affected composable directly via composeRule.setContent(...)
        where possible, rather than the full app/ViewModel/navigation
        graph -- only go through the real screen if the fix can't be
        observed any other way.
      - For a color/visual fix: capture the node with
        captureToImage().asAndroidBitmap().getPixel(x, y) and assert
        against the expected value with a small tolerance. Sample a point
        well inside any padding() on that node, not near its edge -- a
        node's captured bounds include its own padding, not just its
        visible painted area; dump a debug screenshot to a file and pull
        it via adb if you're not sure where the paint boundary actually
        is before picking coordinates.
      - For an interaction fix: drive the real click/gesture and assert
        the resulting state or callback fired, not just that nothing
        crashed.

   c. Run only that test class:
      ./gradlew :app:connectedDebugAndroidTest -Pandroid.testInstrumentationRunnerArguments.class=<fully.qualified.TestClassName>

   d. A failure here is treated exactly like a unit test failure: fix the
      underlying issue (never the test) or mark the row Blocked with why.
      Do not leave a failing on-device test uncommitted-but-unmentioned.

Never weaken or delete an existing test's assertion just to make it pass.
If a test looks wrong, say so in your notes instead of editing it away.

Decide the outcome:

- Everything checks out (the fix is real, tests pass, on-device
  verification passed or was correctly not applicable): update AUDIT.md's
  row for {row_id} -- set Fix Status to 'Fixed (verified)', and rewrite
  the Notes to state plainly what you verified and how (name the test
  class(es), what they check, and whether on-device verification ran or
  was skipped and why). Commit only if you actually changed something (a
  new/extended test, or a small completion of the existing fix) with a
  message referencing {row_id}. If you made no code changes at all
  (the existing fix and its test were already complete and correct),
  commit nothing except the AUDIT.md update itself.

- The fix is missing, wrong, or incomplete in a way you should not
  unilaterally redesign: set Fix Status to 'Blocked', and in Notes
  explain precisely what's wrong with the existing fix and why it needs a
  real second pass. Do not commit any half-fix.

Do not push.

End your response with a single clear line stating the row ID and whether
it is now 'Fixed (verified)' or 'Blocked', so this can be confirmed by
re-reading the file."""


# ======================================================================
# PRE-FLIGHT: command-line arguments
# ======================================================================

def parse_arguments():
    """Define and parse every command-line argument this tool accepts."""
    parser = argparse.ArgumentParser(
        prog="audit_verify_runner.py",
        description=(
            "Work through a project's AUDIT.md Master Tracking Table one row "
            "at a time, verifying rows whose Fix Status contains "
            "'unverified'. Each row is handled by its own fresh, "
            "memory-free headless Claude Code session, which confirms the "
            "existing fix actually holds up (writing/running a test, incl. "
            "on-device where applicable) rather than reimplementing it."
        ),
    )

    parser.add_argument(
        "--project",
        required=True,
        help="Path to the project root. Must contain AUDIT.md at its root.",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help=(
            "Git branch to work on. Default: audit-<today's date, YYYY-MM-DD>. "
            "If the branch already exists it is reused -- this is how resuming "
            "a previous run works."
        ),
    )
    parser.add_argument(
        "--mode",
        choices=["manual", "auto"],
        default="manual",
        help=(
            "manual (default): pause and wait for you to press Enter after "
            "every row before continuing. auto: keep going automatically "
            "after each row, only stopping at the end or on Ctrl+C."
        ),
    )
    parser.add_argument(
        "--severities",
        default=None,
        help=(
            'Comma-separated list like "P0,P1" to only process rows of those '
            "severities this run. Default: all severities, P0 first."
        ),
    )
    parser.add_argument(
        "--max-rows",
        dest="max_rows",
        type=int,
        default=None,
        help=(
            "Stop after processing N rows this run, regardless of mode. "
            "Useful as a safety valve. Default: no limit."
        ),
    )
    parser.add_argument(
        "--pause-every",
        dest="pause_every",
        type=int,
        default=None,
        help=(
            "In --mode manual, pause for confirmation every N rows instead "
            "of every row. If not set, this is chosen automatically: no "
            "pausing if --max-rows is 2 or less, otherwise every 2 rows."
        ),
    )
    parser.add_argument(
        "--max-turns",
        dest="max_turns",
        type=int,
        default=60,
        help=(
            "Cap each row's Claude session at N agent turns, so a session "
            "that gets stuck (e.g. polling in a loop waiting on a slow "
            "command) fails fast and cleanly instead of silently running "
            "out of budget. Default: 60."
        ),
    )
    parser.add_argument(
        "--test-cmd",
        dest="test_cmd",
        default=None,
        help=(
            "The shell command that runs this project's full test suite, e.g. "
            '"./gradlew testDebugUnitTest" or "npm test". Passed to each '
            "row's Claude session as the required verification step. If "
            "omitted, you will be asked to confirm before continuing."
        ),
    )
    parser.add_argument(
        "--emulator-avd",
        dest="emulator_avd",
        default=None,
        help=(
            "Name of an Android emulator AVD (e.g. "
            '"Pixel_3a_API_34_extension_level_7_arm64-v8a") to make '
            "available for on-device verification steps. If no device or "
            "emulator is already attached when the run starts, this AVD "
            "is booted once, before the first row, and left running for "
            "every row in the run (not rebooted per row) -- if this "
            "script is the one that started it, it's shut down again "
            "after the last row. If a device or emulator is already "
            "attached, it's reused as-is and left running afterward, "
            "since this script didn't start it. Omit for non-Android "
            "projects, or to manage the emulator yourself."
        ),
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help=(
            "Print the full plan (branch, row order, count) and exit without "
            "running anything."
        ),
    )
    parser.add_argument(
        "--auto-push",
        dest="auto_push",
        action="store_true",
        help=(
            "After each row's session finishes and that row is confirmed "
            "'Fixed (verified)' in AUDIT.md, automatically run `git push "
            "origin <branch>` before moving to the next row. Off by "
            "default -- rows that end up Blocked or still unverified are "
            "never pushed, regardless of this flag. A failed push is "
            "reported but does not stop the run, since the local commit "
            "is safe either way."
        ),
    )
    parser.add_argument(
        "--usage-warn-threshold",
        dest="usage_warn_threshold",
        type=int,
        default=90,
        help=(
            "Warn if Claude Code session or weekly usage is at or above N "
            "percent before starting this run. Checked once, at the start "
            "of the run (not per row). Default: 90."
        ),
    )

    return parser.parse_args()


# ======================================================================
# PRE-FLIGHT: project folder checks
# ======================================================================

def validate_project_path(project_path):
    """
    Confirm --project points at a real folder that contains AUDIT.md.
    Exits the whole script with a clear error if not. Returns the path
    to AUDIT.md on success.
    """
    if not project_path.is_dir():
        print(f"ERROR: --project path does not exist or is not a folder: {project_path}")
        sys.exit(1)

    audit_path = project_path / "AUDIT.md"
    if not audit_path.is_file():
        print(f"ERROR: no AUDIT.md found at the root of the project: {audit_path}")
        sys.exit(1)

    print(f"Project folder: {project_path}")
    print(f"Found AUDIT.md: {audit_path}\n")
    return audit_path


def print_project_context(project_path):
    """
    Print the first ~15 lines of PROJECT.md (preferred) or README.md, if
    either exists, as an informational sanity check that this is the right
    project. This is not used programmatically anywhere else.

    The whole section (including the no-preview-found fallback) is wrapped
    in full-width dashed separator lines so it reads as one clearly bounded
    block instead of blending into the rest of the pre-flight output.
    """
    dashed_line = "-" * BANNER_WIDTH
    print(dashed_line)
    print("Reading project context...")
    for filename in ["PROJECT.md", "README.md"]:
        candidate_path = project_path / filename
        if candidate_path.is_file():
            print(f"--- first 15 lines of {filename} ---")
            file_text = candidate_path.read_text(errors="replace")
            for line in file_text.splitlines()[:15]:
                print(line)
            print("--- end of preview ---")
            print(dashed_line + "\n")
            return
    print("(No PROJECT.md or README.md found at the project root -- skipping preview.)")
    print(dashed_line + "\n")


# ======================================================================
# PRE-FLIGHT: git safety checks
# ======================================================================

def run_git_command(project_path, git_args):
    """
    Run `git <git_args>` inside project_path. Returns a tuple of
    (succeeded, stdout_text, stderr_text). Exits the script with a plain
    error message if git itself is not installed.
    """
    try:
        result = subprocess.run(
            ["git"] + git_args,
            cwd=project_path,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        print("ERROR: git was not found on your PATH. Please install git and try again.")
        sys.exit(1)

    return result.returncode == 0, result.stdout.rstrip("\n"), result.stderr.strip()


def confirm_git_repo(project_path):
    """Stop the script if --project is not inside a git repository."""
    succeeded, stdout_text, _stderr_text = run_git_command(
        project_path, ["rev-parse", "--is-inside-work-tree"]
    )
    if not succeeded or stdout_text != "true":
        print(f"ERROR: {project_path} does not look like a git repository.")
        sys.exit(1)
    print("Git repository: confirmed.")


def is_logs_folder_status_line(status_line):
    """
    True if a `git status --porcelain` line refers only to this tool's own
    logs/ folder (run logs and run-summary files). Those are intentionally
    left untracked, so they must never make the working tree look "dirty" --
    otherwise the very first row of every real run would falsely abort.
    """
    path_part = status_line[3:] if len(status_line) > 3 else ""
    return path_part.strip().startswith("logs/")


def confirm_clean_working_tree(project_path, current_row_id=None):
    """
    Stop the script if the git working tree has uncommitted changes,
    ignoring this tool's own untracked logs/ folder. If current_row_id is
    given, the error message names that row, since a dirty tree at that
    point usually means a previous row was interrupted mid-fix.

    Exception: if AUDIT.md is the ONLY dirty file, auto-commit it and
    continue instead of aborting -- that's expected churn from this tool
    itself editing AUDIT.md between rows, not a sign of an interrupted fix.
    """
    _succeeded, stdout_text, _stderr_text = run_git_command(
        project_path, ["status", "--porcelain"]
    )
    dirty_lines = [
        line for line in stdout_text.splitlines() if not is_logs_folder_status_line(line)
    ]
    if dirty_lines:
        only_audit_md_dirty = all(
            len(line) > 3 and line[3:].strip() == "AUDIT.md" for line in dirty_lines
        )
        if only_audit_md_dirty:
            print(
                "AUDIT.md is the only dirty file in the working tree -- "
                "auto-committing it before resuming."
            )
            add_succeeded, _stdout_text, add_stderr_text = run_git_command(
                project_path, ["add", "AUDIT.md"]
            )
            commit_succeeded, _stdout_text, commit_stderr_text = run_git_command(
                project_path,
                ["commit", "-m", "AUDIT.md: auto-commit before resuming audit-fix-runner"],
            )
            if add_succeeded and commit_succeeded:
                return
            print("ERROR: failed to auto-commit AUDIT.md.")
            print(add_stderr_text)
            print(commit_stderr_text)
            sys.exit(1)

        print("ERROR: Working tree is dirty.")
        if current_row_id:
            print(
                f"This is possibly left over from an interrupted attempt at row "
                f"{current_row_id}."
            )
        print("Inspect `git status` and `git diff` before re-running.")
        print(
            "Commit, stash, or discard the changes yourself (and, if needed, reset "
            "that row's Fix Status back to an 'unverified' state in AUDIT.md), then "
            "run this tool again."
        )
        sys.exit(1)


def confirm_remote_origin(project_path):
    """Warn (but do not block) if there is no git remote named 'origin'."""
    _succeeded, stdout_text, _stderr_text = run_git_command(project_path, ["remote", "-v"])
    if "origin" not in stdout_text:
        print(
            "WARNING: no git remote named 'origin' was found. That's fine for "
            "working locally, but you won't be able to push this branch until "
            "one is added.\n"
        )
    else:
        print("Git remote 'origin': found.")


def confirm_no_sibling_runner_active(project_path, this_script_name, sibling_script_names):
    """
    Refuse to start if a DIFFERENT runner script (the fixer or the
    verifier) is already running against this same project path. This is
    a one-time check made by the orchestrator process itself, before the
    row loop starts -- never delegated into a per-row `claude -p`
    session's own prompt, which can't reliably tell "my own parent
    process" apart from "a genuinely separate process" (that exact
    confusion previously derailed a run: a per-row session read a memory
    note telling it to check for a live audit_fix_runner.py, found
    itself, and aborted mid-fix). Checked once, here, with full context,
    there is no such ambiguity.

    Exits the script if a sibling is found. Best-effort: if `ps` itself
    fails for some reason, this does not block the run.
    """
    try:
        result = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=10)
    except Exception:
        return

    project_str = str(project_path)
    for line in result.stdout.splitlines():
        if project_str not in line:
            continue
        for sibling_name in sibling_script_names:
            if sibling_name in line and sibling_name != this_script_name:
                print(f"ERROR: {sibling_name} appears to already be running against this project:")
                print(f"  {line.strip()}")
                print(
                    f"Refusing to start {this_script_name} at the same time -- both scripts "
                    "touch the same working tree, branch, and commit history. Let the other "
                    "one finish first."
                )
                sys.exit(1)


def push_branch_to_origin(project_path, branch_name):
    """
    Run `git push origin <branch_name>`. Prints a clear success or error
    line either way. Never raises -- a failed push (network issue,
    rejected, etc.) is reported but must not crash the run, since the
    local commit is safe regardless.
    """
    succeeded, _stdout_text, stderr_text = run_git_command(
        project_path, ["push", "origin", branch_name]
    )
    if succeeded:
        print(f"{TermColors.GREEN}Pushed to origin/{branch_name}.{TermColors.RESET}")
    else:
        print(
            f"{TermColors.RED}ERROR: git push to origin/{branch_name} failed -- "
            f"continuing anyway, the local commit is still safe.{TermColors.RESET}"
        )
        if stderr_text:
            print(stderr_text)


def get_current_branch(project_path):
    """Return the name of the branch currently checked out."""
    succeeded, stdout_text, _stderr_text = run_git_command(
        project_path, ["branch", "--show-current"]
    )
    if not succeeded or stdout_text == "":
        print("ERROR: could not determine the current git branch.")
        sys.exit(1)
    return stdout_text


def branch_exists_locally(project_path, branch_name):
    """Return True if branch_name already exists as a local branch."""
    _succeeded, stdout_text, _stderr_text = run_git_command(
        project_path, ["branch", "--list", branch_name]
    )
    return stdout_text != ""


def checkout_or_create_branch(project_path, branch_name):
    """
    Check out branch_name if it already exists locally (this is how a
    previous, interrupted run is resumed). Otherwise create it fresh from
    whatever branch is currently checked out.
    """
    if branch_exists_locally(project_path, branch_name):
        print(f"Branch '{branch_name}' already exists -- checking it out to resume.")
        succeeded, _stdout_text, stderr_text = run_git_command(
            project_path, ["checkout", branch_name]
        )
        if not succeeded:
            print(f"ERROR: could not check out branch '{branch_name}':\n{stderr_text}")
            sys.exit(1)
    else:
        base_branch = get_current_branch(project_path)
        print(f"Creating new branch '{branch_name}' from '{base_branch}'.")
        succeeded, _stdout_text, stderr_text = run_git_command(
            project_path, ["checkout", "-b", branch_name]
        )
        if not succeeded:
            print(f"ERROR: could not create branch '{branch_name}':\n{stderr_text}")
            sys.exit(1)

    print(f"On branch: {branch_name}\n")


def restore_branch_after_dry_run(project_path, original_branch, branch_name, branch_already_existed):
    """
    Undoes checkout_or_create_branch's side effects after a --dry-run:
    switches back to whatever branch was checked out before this run
    started, and removes branch_name if this run is the one that created
    it (a plain `git branch -d`, safe since a dry run never commits, so
    the branch can only be identical to its base -- trivially "merged").
    A dry run should leave git state exactly as it found it.
    """
    if original_branch != branch_name:
        succeeded, _stdout_text, stderr_text = run_git_command(
            project_path, ["checkout", original_branch]
        )
        if not succeeded:
            print(
                f"WARNING: could not switch back to '{original_branch}' after the dry "
                f"run:\n{stderr_text}\nYou may need to `git checkout {original_branch}` "
                "yourself."
            )
            return

    if not branch_already_existed:
        succeeded, _stdout_text, stderr_text = run_git_command(
            project_path, ["branch", "-d", branch_name]
        )
        if not succeeded:
            print(
                f"WARNING: could not remove the '{branch_name}' branch created for this "
                f"dry run:\n{stderr_text}\nYou may want to remove it yourself with "
                f"`git branch -d {branch_name}`."
            )


# ======================================================================
# PRE-FLIGHT: reading AUDIT.md's Master Tracking Table (read-only)
# ======================================================================
#
# The script never writes to AUDIT.md. It only reads the Master Tracking
# Table to decide what work is left and to report progress. All edits to
# AUDIT.md are made by the per-row Claude session.

def split_table_row(line):
    """
    Split one markdown table line into a list of stripped cell strings,
    ignoring an optional leading/trailing pipe. Example:
        "| P0-1 | P0 | Not started |"  ->  ["P0-1", "P0", "Not started"]
    """
    stripped_line = line.strip()
    if stripped_line.startswith("|"):
        stripped_line = stripped_line[1:]
    if stripped_line.endswith("|"):
        stripped_line = stripped_line[:-1]
    raw_cells = stripped_line.split("|")
    return [cell.strip() for cell in raw_cells]


def is_table_row(line):
    """A markdown table line contains at least one pipe character."""
    return "|" in line.strip()


def is_separator_row(line):
    """
    A markdown table separator row looks like: | --- | :--- | ---: |
    (only dashes, colons, and pipes).
    """
    if not is_table_row(line):
        return False
    cells = split_table_row(line)
    if not cells:
        return False
    for cell in cells:
        if not re.match(r"^:?-+:?$", cell.strip()):
            return False
    return True


def find_heading_line_index(lines, heading_keyword):
    """
    Find the first markdown heading line (a line starting with '#') whose
    text contains heading_keyword, case-insensitively. Returns the line
    index, or None if no such heading exists.
    """
    for line_index, line in enumerate(lines):
        stripped_line = line.strip()
        if stripped_line.startswith("#"):
            heading_text = stripped_line.lstrip("#").strip().lower()
            if heading_keyword.lower() in heading_text:
                return line_index
    return None


def find_fix_status_table_header(lines, start_index):
    """
    Search lines[start_index:] for a markdown table header row that
    contains a "Fix Status" column, followed immediately by a separator
    row. Returns the line index of the header row, or None if not found.
    """
    for line_index in range(start_index, len(lines)):
        line = lines[line_index]
        if not is_table_row(line):
            continue
        cells = split_table_row(line)
        normalized_cells = [cell.lower() for cell in cells]
        if "fix status" in normalized_cells:
            next_line_index = line_index + 1
            if next_line_index < len(lines) and is_separator_row(lines[next_line_index]):
                return line_index
    return None


def find_master_tracking_table_header(lines):
    """
    Find the header row of the Master Tracking Table.

    Some AUDIT.md files have more than one table with a "Fix Status"
    column -- for example, an earlier summary or legend table. To avoid
    picking the wrong one, this first looks for a markdown heading whose
    text contains "Master Tracking Table" and, if found, only searches
    for the Fix Status table at or after that heading. If no such heading
    exists (or no matching table is found after it), it falls back to
    searching the whole file for the first Fix Status table, so files
    without that exact heading still work.
    """
    heading_line_index = find_heading_line_index(lines, "master tracking table")

    if heading_line_index is not None:
        header_line_index = find_fix_status_table_header(lines, heading_line_index)
        if header_line_index is not None:
            return header_line_index

    return find_fix_status_table_header(lines, 0)


def map_column_positions(header_cells):
    """
    Work out which column index holds the id, severity, fix status, and
    notes values, matching header names case-insensitively. Falls back to
    the first column for "id" if no column is literally named "ID".
    """
    positions = {"id": None, "severity": None, "fix_status": None, "notes": None}

    for column_index, header_text in enumerate(header_cells):
        normalized_header = header_text.strip().lower()
        if normalized_header == "id" and positions["id"] is None:
            positions["id"] = column_index
        elif normalized_header == "severity" and positions["severity"] is None:
            positions["severity"] = column_index
        elif normalized_header == "fix status" and positions["fix_status"] is None:
            positions["fix_status"] = column_index
        elif normalized_header == "notes" and positions["notes"] is None:
            positions["notes"] = column_index

    if positions["id"] is None:
        positions["id"] = 0

    return positions


def build_row_dict(cells, column_positions, raw_line):
    """Turn one data row's cells into a normalized dict, or None if blank."""

    def cell_at(column_index):
        if column_index is None or column_index >= len(cells):
            return ""
        return cells[column_index].strip()

    row_id = cell_at(column_positions["id"])
    if row_id == "":
        return None

    return {
        "id": row_id,
        "severity": cell_at(column_positions["severity"]).upper(),
        "fix_status": cell_at(column_positions["fix_status"]),
        "notes": cell_at(column_positions["notes"]),
        "raw_row": raw_line,
    }


def parse_master_tracking_table(audit_path):
    """
    Read AUDIT.md fresh from disk and parse its Master Tracking Table into
    a list of row dicts: {id, severity, fix_status, notes, raw_row}.
    Read-only: never writes anything back to AUDIT.md. Exits the script
    with a clear error if no such table can be found.
    """
    file_text = audit_path.read_text(errors="replace")
    lines = file_text.splitlines()

    header_line_index = find_master_tracking_table_header(lines)
    if header_line_index is None:
        print("ERROR: could not find a Master Tracking Table in AUDIT.md.")
        print("(Looked for a markdown table with a 'Fix Status' column.)")
        sys.exit(1)

    header_cells = split_table_row(lines[header_line_index])
    column_positions = map_column_positions(header_cells)

    rows = []
    row_line_index = header_line_index + 2  # skip the header row and the separator row
    while row_line_index < len(lines) and is_table_row(lines[row_line_index]):
        cells = split_table_row(lines[row_line_index])
        row_dict = build_row_dict(cells, column_positions, lines[row_line_index])
        if row_dict is not None:
            rows.append(row_dict)
        row_line_index += 1

    return rows


def status_matches(status_text, target_status):
    """Compare two status strings case-insensitively, ignoring surrounding whitespace."""
    return status_text.strip().lower() == target_status.strip().lower()


def status_is_fixed(status_text):
    """
    True for any status starting with "Fixed" -- bare "Fixed" as well as
    annotated variants like "Fixed (verified)" and "Fixed (unverified)".
    Case-insensitive, ignoring surrounding whitespace. Does NOT match
    "Partially fixed ..." (that's a different status, not a "Fixed"
    prefix) -- those stay counted under "Other status".
    """
    return status_text.strip().lower().startswith(STATUS_FIXED.strip().lower())


def status_is_unverified(status_text):
    """
    True for any status containing "unverified", regardless of what it
    starts with -- catches "Fixed (unverified)", "Partially fixed
    (unverified)", and any other future variant, since the word itself
    (not a fixed prefix list) is what actually signals "needs a
    verification pass". Case-insensitive, ignoring surrounding
    whitespace.
    """
    return "unverified" in status_text.strip().lower()


def find_row_by_id(rows, row_id):
    """Find a row dict by its id, matching case-insensitively."""
    for row in rows:
        if row["id"].strip().lower() == row_id.strip().lower():
            return row
    return None


def severity_sort_key(severity):
    """Sort key: P0 first, then P1, P2, P3, then anything else alphabetically."""
    normalized_severity = severity.strip().upper()
    if normalized_severity in SEVERITY_ORDER:
        return (0, SEVERITY_ORDER.index(normalized_severity))
    return (1, normalized_severity)


def render_ascii_table(headers, rows, align=None):
    """
    Render a simple bordered ASCII table (box-drawing characters) given a
    list of column header strings and a list of row tuples (cells are
    stringified). Column widths are sized to the longest cell in each
    column. align is an optional list of "left"/"right" per column,
    defaulting to left for every column. Returns a list of lines to print.
    """
    if align is None:
        align = ["left"] * len(headers)

    all_rows = [headers] + [[str(cell) for cell in row] for row in rows]
    column_widths = [max(len(row[i]) for row in all_rows) for i in range(len(headers))]

    def build_separator(left, mid, right):
        return left + mid.join("─" * (w + 2) for w in column_widths) + right

    def build_row(cells):
        padded_cells = [
            (str(cell).rjust(w) if a == "right" else str(cell).ljust(w))
            for cell, w, a in zip(cells, column_widths, align)
        ]
        return "│ " + " │ ".join(padded_cells) + " │"

    lines = [build_separator("┌", "┬", "┐"), build_row(headers), build_separator("├", "┼", "┤")]
    for row in rows:
        lines.append(build_row(row))
    lines.append(build_separator("└", "┴", "┘"))
    return lines


def summarize_rows(rows):
    """Print a console summary of the Master Tracking Table's current state."""
    total_count = len(rows)
    not_started_count = sum(1 for row in rows if status_matches(row["fix_status"], STATUS_NOT_STARTED))
    fixed_count = sum(1 for row in rows if status_is_fixed(row["fix_status"]))
    blocked_count = sum(1 for row in rows if status_matches(row["fix_status"], STATUS_BLOCKED))
    other_count = total_count - not_started_count - fixed_count - blocked_count
    unverified_count = sum(1 for row in rows if status_is_unverified(row["fix_status"]))

    print(f"Master Tracking Table: {total_count} row(s) total\n")

    status_rows = [
        ("Not started", not_started_count),
        ("Fixed", fixed_count),
        ("Blocked", blocked_count),
    ]
    if other_count:
        status_rows.append(("Other status", other_count))
    for line in render_ascii_table(["Status", "Count"], status_rows, align=["left", "right"]):
        print(line)
    print()
    print(f"Rows needing verification (status contains 'unverified'): {unverified_count}\n")

    severities_seen = sorted(set(row["severity"] for row in rows), key=severity_sort_key)
    severity_rows = []
    for severity in severities_seen:
        count = sum(1 for row in rows if row["severity"] == severity)
        label = severity if severity else "(blank)"
        severity_rows.append((label, count))
    for line in render_ascii_table(["Severity", "Count"], severity_rows, align=["left", "right"]):
        print(line)
    print()


def select_rows_to_process(rows, severities_filter, max_rows):
    """
    Build the ordered list of rows this run will process: only rows whose
    status contains "unverified", optionally restricted to specific
    severities, sorted P0 first (ties keep the table's original order),
    optionally capped at max_rows.
    """
    unverified_rows = [row for row in rows if status_is_unverified(row["fix_status"])]

    if severities_filter:
        allowed_severities = set(severity.strip().upper() for severity in severities_filter)
        unverified_rows = [row for row in unverified_rows if row["severity"] in allowed_severities]

    # Python's sort() is stable, so rows that share a severity keep their
    # original order from the table.
    ordered_rows = sorted(unverified_rows, key=lambda row: severity_sort_key(row["severity"]))

    if max_rows is not None:
        ordered_rows = ordered_rows[:max_rows]

    return ordered_rows


def compute_pause_interval(pause_every, max_rows):
    """
    Work out how often --mode manual should pause for confirmation, in
    rows. Returns an int interval (pause after every Nth row completed
    this run), or None if pausing should be disabled entirely (run
    straight through, same as --mode auto, within this run's row limit).

    If pause_every was explicitly given on the command line, it's used
    as-is. Otherwise it's auto-derived: --max-rows of 2 or less disables
    pausing; --max-rows of None or more than 2 pauses every 2 rows.
    """
    if pause_every is not None:
        return pause_every

    if max_rows is not None and max_rows <= 2:
        return None
    return 2


def print_dry_run_plan(branch_name, ordered_rows):
    """Print what a real run would do, without doing any of it."""
    print("=" * 60)
    print("DRY RUN -- no Claude sessions will be started")
    print("=" * 60)
    print(f"Branch: {branch_name}")
    print(f"Rows that would be processed: {len(ordered_rows)}\n")
    for position, row in enumerate(ordered_rows, start=1):
        print(f"  {position}. {row['id']} ({row['severity']})")
    if not ordered_rows:
        print("  (none -- nothing matches your filters)")
    print("\nRe-run without --dry-run to actually start working through these rows.")


def confirm_missing_test_command():
    """
    If --test-cmd was not given, warn the user clearly and require them to
    type "yes" before continuing. Exits the script if they don't.
    """
    print("WARNING: no --test-cmd was given.")
    print(
        "Each row's Claude session will not have a project test command to "
        "verify its fix against, which is risky -- it may commit changes "
        "without confirming the test suite still passes."
    )
    answer = input("Type 'yes' to continue anyway, or anything else to stop: ")
    if answer.strip().lower() != "yes":
        print("Stopping. Re-run with --test-cmd \"<your test command>\".")
        sys.exit(1)
    print()


# ======================================================================
# LOOP: usage checking
# ======================================================================

def parse_claude_usage(output_text):
    """
    Extract 'Current session' and 'Current week' usage percentages from
    the text output of `claude /usage`. Returns a dict with
    session_percent, week_percent (ints or None), and session_reset,
    week_reset (strings or None).
    """
    result = {
        "session_percent": None,
        "week_percent": None,
        "session_reset": None,
        "week_reset": None,
    }
    session_match = re.search(
        r"Current session[:\s].*?(\d+)%\s*used"
        r"(?:.*?resets\s+([^\n]+?)(?:\n|$))?",
        output_text,
        re.IGNORECASE,
    )
    if session_match:
        result["session_percent"] = int(session_match.group(1))
        if session_match.group(2):
            result["session_reset"] = session_match.group(2).strip()
    week_match = re.search(
        r"Current week[^\n:]*[:\s].*?(\d+)%\s*used"
        r"(?:.*?resets\s+([^\n]+?)(?:\n|$))?",
        output_text,
        re.IGNORECASE,
    )
    if week_match:
        result["week_percent"] = int(week_match.group(1))
        if week_match.group(2):
            result["week_reset"] = week_match.group(2).strip()
    return result


def get_current_usage():
    """
    Runs `claude /usage` with output redirected (not a real terminal, so
    it behaves non-interactively and exits cleanly) and parses the result.
    Returns the same dict as parse_claude_usage(), or all-None values if
    the command fails for any reason (never crash the main script over this).

    Called exactly twice per script invocation -- once at command start
    (before the LOOP begins) and once at command end (in POST-RUN) -- not
    once per row. See display_usage_check(), run_start_usage_check(), and
    run_end_usage_check() below.
    """
    try:
        completed = subprocess.run(
            ["claude", "/usage"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return parse_claude_usage(completed.stdout)
    except Exception:
        return parse_claude_usage("")


def display_usage_check(label, before_usage=None):
    """
    Fetch current usage and build (not print) the full usage-check block --
    the USAGE-CHECK-START/END grep markers, the bordered banner, and both
    colored bars -- for one command-level snapshot identified by label
    (e.g. "BEFORE THIS RUN" or "AFTER THIS RUN").

    Always builds the full block regardless of how high usage is -- there
    is no threshold gate here. (The old per-row version only ever built
    this block when a threshold was crossed, which in practice meant it
    almost never appeared in a real run's console output; command-level
    calls now always show it, once at the start and once at the end, so
    the user gets a real "before" and "after" snapshot every run.)

    If before_usage is given (the dict returned by an earlier
    run_start_usage_check() call), a "(+N% this run)" delta line is added
    under each bar comparing it against this snapshot.

    Returns a tuple of (usage_dict, lines) -- usage_dict is the same shape
    parse_claude_usage() returns, so callers can act on the percentages
    without re-parsing the display lines.
    """
    usage = get_current_usage()
    lines = ["--- USAGE-CHECK-START ---"]
    lines.extend(usage_check_banner_lines(label))
    if usage["session_percent"] is None and usage["week_percent"] is None:
        lines.append("Could not retrieve Claude usage.")
    else:
        lines.append("Current session")
        lines.append(
            f"  {usage_bar_color(usage['session_percent'])}"
            f"{render_usage_bar(usage['session_percent'])}{TermColors.RESET}"
        )
        if before_usage:
            delta_line = format_usage_delta_line(before_usage.get("session_percent"), usage["session_percent"])
            if delta_line:
                lines.append(delta_line)
        if usage["session_reset"]:
            lines.append(f"  Resets {usage['session_reset']}")
        lines.append("Current week (all models)")
        lines.append(
            f"  {usage_bar_color(usage['week_percent'])}"
            f"{render_usage_bar(usage['week_percent'])}{TermColors.RESET}"
        )
        if before_usage:
            delta_line = format_usage_delta_line(before_usage.get("week_percent"), usage["week_percent"])
            if delta_line:
                lines.append(delta_line)
        if usage["week_reset"]:
            lines.append(f"  Resets {usage['week_reset']}")
    lines.append("--- USAGE-CHECK-END ---")
    return usage, lines


def run_start_usage_check(usage_warn_threshold, run_summary_path):
    """
    Display the "before this run" usage snapshot. Called exactly once per
    script invocation, right after pre-flight finishes and before the LOOP
    section begins -- always shown, not gated on the threshold.

    If usage is already at or above usage_warn_threshold (session or
    week), warns clearly and asks for y/N confirmation before the loop is
    allowed to start at all. Returns a tuple of (should_run, usage) --
    should_run is True if the run should proceed, False if the user
    declined; usage is the dict from display_usage_check(), so the caller
    can hand it to run_end_usage_check() later for a before/after delta.
    """
    usage, lines = display_usage_check("BEFORE THIS RUN")
    for line in lines:
        print(line)
    append_lines_to_log(run_summary_path, lines)

    session_percent = usage["session_percent"] or 0
    week_percent = usage["week_percent"] or 0
    if session_percent >= usage_warn_threshold or week_percent >= usage_warn_threshold:
        usage_choice = input(
            f"\nUsage is already at or above {usage_warn_threshold}% -- "
            "start this run anyway? [y/N]: "
        )
        if usage_choice.strip().lower() != "y":
            return False, usage
    print()
    return True, usage


def run_end_usage_check(run_summary_path, before_usage=None):
    """
    Display the "after this run" usage snapshot. Called exactly once per
    script invocation, in POST-RUN after the loop has finished for any
    reason (normal completion, --max-rows reached, manual stop, Ctrl+C, or
    'claude' going missing). Purely informational -- never blocks or
    prompts -- so the user can see how much usage this run actually
    consumed.

    before_usage, if given (the usage dict returned by run_start_usage_check()),
    is used to print a "(+N% this run)" delta line under each bar.
    """
    _usage, lines = display_usage_check("AFTER THIS RUN", before_usage=before_usage)
    for line in lines:
        print(line)
    append_lines_to_log(run_summary_path, lines)
    print()


# ======================================================================
# LOOP: building and running one row's Claude session
# ======================================================================

def build_prompt(row_id, branch_name, test_cmd):
    """Fill in the prompt template for one row."""
    return PROMPT_TEMPLATE.format(row_id=row_id, branch_name=branch_name, test_cmd=test_cmd)


def strip_ansi_codes(text):
    """Remove terminal color/control escape codes so log files stay plain text."""
    return ANSI_ESCAPE_PATTERN.sub("", text)


def ensure_logs_folder(project_path):
    """Create <project>/logs if it doesn't already exist, and return its path."""
    logs_folder = project_path / "logs"
    logs_folder.mkdir(exist_ok=True)
    return logs_folder


def build_row_log_path(logs_folder, row_id):
    """Build a unique log file path for one row's session, e.g. logs/20260803_142200_P0-3.log"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_row_id = row_id.replace("/", "-").replace(" ", "_")
    return logs_folder / f"{timestamp}_{safe_row_id}.log"


def append_lines_to_log(log_path, lines):
    """
    Append plain text lines to a log file, ANSI-stripped, creating the file
    if it doesn't exist yet. Used both for a row's own log file (for output
    that happens before run_claude_session() opens log_path itself) and for
    the run summary file (for the command-level usage-check snapshots).
    """
    with open(log_path, "a", encoding="utf-8") as log_file:
        for line in lines:
            log_file.write(strip_ansi_codes(line) + "\n")


# Tool names, as they appear in a stream-json tool_use block's "name"
# field, that count as "editing a file" for console progress purposes.
FILE_EDIT_TOOL_NAMES = {"Edit", "Write", "MultiEdit"}

# How much of a Bash command to show on the console before truncating.
BASH_COMMAND_CONSOLE_PREVIEW_LENGTH = 80


# ======================================================================
# TERMINAL UI & FORMATTING HELPERS
# ======================================================================

class TermColors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"

    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"

def print_banner(title, color=TermColors.CYAN):
    """Prints a styled box banner around sections."""
    width = BANNER_WIDTH
    print(f"\n{color}{TermColors.BOLD}╔{'═' * (width - 2)}╗")
    print(f"║ {title.center(width - 4)} ║")
    print(f"╚{'═' * (width - 2)}╝{TermColors.RESET}\n")


def usage_check_banner_lines(label):
    """
    Build (not print) the bordered banner lines for the usage-check block.
    Magenta with a 📊 marker, so it's visually distinct at a glance from
    both the blue section banners and the cyan per-row banners already in
    use, and easy to spot in a long, noisy console/log.

    label identifies which of the two command-level checks this is, e.g.
    "BEFORE THIS RUN" or "AFTER THIS RUN" -- this is no longer per-row.
    """
    width = 64
    title = f"📊 USAGE CHECK — {label}"
    color = TermColors.MAGENTA
    return [
        f"{color}{TermColors.BOLD}╔{'═' * (width - 2)}╗",
        f"║ {title.center(width - 4)}║",
        f"╚{'═' * (width - 2)}╝{TermColors.RESET}",
    ]

def short_path(file_path, project_path):
    """Truncates absolute paths relative to project root for cleaner output."""
    if not file_path:
        return "file"
    try:
        rel = Path(file_path).relative_to(project_path)
        return f"./{rel}"
    except ValueError:
        return str(file_path)


def render_usage_bar(percent, width=40):
    """
    Renders a filled progress bar like: '███████████████▌    31% used'
    percent: 0-100 (int). width: total character width of the bar itself.
    """
    if percent is None:
        return "(unknown)"
    filled = int((percent / 100) * width)
    half_block = "▌" if (percent / 100 * width) - filled >= 0.5 else ""
    bar = ("█" * filled) + half_block
    bar = bar.ljust(width)
    return f"{bar} {percent}% used"


def usage_bar_color(percent):
    """Pick a TermColors color for a usage percentage: green/yellow/red."""
    if percent is None:
        return TermColors.RESET
    if percent >= 90:
        return TermColors.RED
    if percent >= 70:
        return TermColors.YELLOW
    return TermColors.GREEN


def format_usage_delta_line(before_percent, after_percent):
    """
    Build a "(+N% this run)" line comparing a before/after usage percentage,
    or None if either side is unavailable. Flagged YELLOW/RED when the run
    burned through more than ~10%/~20% in one go, so an expensive row stands
    out at a glance; default color otherwise.
    """
    if before_percent is None or after_percent is None:
        return None
    delta = after_percent - before_percent
    sign = "+" if delta >= 0 else ""
    if abs(delta) >= 20:
        color = TermColors.RED
    elif abs(delta) > 10:
        color = TermColors.YELLOW
    else:
        color = TermColors.RESET
    return f"  {color}({sign}{delta}% this run){TermColors.RESET}"


def console_line_for_tool_use(tool_name, tool_input, project_path):
    """
    Returns a styled, human-readable console progress line for a tool call.
    Returns None for tool types that shouldn't print anything to console.
    This function is for CONSOLE DISPLAY ONLY -- the caller is responsible
    for writing the full raw event to the log file separately, with ANSI
    codes stripped, exactly as it already does today.
    """
    G = TermColors.GRAY
    C = TermColors.CYAN
    R = TermColors.RESET

    if tool_name in FILE_EDIT_TOOL_NAMES:
        file_path = tool_input.get("file_path", "") if tool_input else ""
        clean_path = short_path(file_path, project_path)
        return f"  {TermColors.YELLOW}✎ {R}{G}Editing {TermColors.BOLD}{clean_path}{R}"

    if tool_name == "Bash":
        command = tool_input.get("command", "") if tool_input else ""
        command = command.replace(str(project_path), ".")
        if len(command) > BASH_COMMAND_CONSOLE_PREVIEW_LENGTH:
            command = command[:BASH_COMMAND_CONSOLE_PREVIEW_LENGTH] + "..."
        return f"  {C}⚡{R} {G}Running:{R} {TermColors.ITALIC}{command}{R}"

    if tool_name == "Read":
        file_path = tool_input.get("file_path", "") if tool_input else ""
        clean_path = short_path(file_path, project_path)
        return f"  {G}\U0001F4D6 Reading {clean_path}...{R}"

    return None


def console_lines_for_stream_json_event(event, project_path):
    """
    Given one successfully parsed stream-json event (a dict with a "type"
    field), return a list of short, human-readable console progress lines
    to print for it. Returns an empty list for event types this tool has
    nothing worth printing for (partial text deltas, system/init events,
    etc.) -- those are still written to the log file in full by the
    caller, just not echoed to the console.

    Tool calls arrive nested inside "assistant" events, as one or more
    tool_use blocks in message["content"] -- there is no separate top-
    level "tool_use" event type.
    """
    event_type = event.get("type")

    if event_type == "result":
        return ["  Session finished."]

    if event_type == "assistant":
        content_blocks = (event.get("message") or {}).get("content") or []
        console_lines = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                console_line = console_line_for_tool_use(
                    block.get("name", ""), block.get("input") or {}, project_path
                )
                if console_line:
                    console_lines.append(console_line)
        return console_lines

    return []


def detect_turn_limit_hit(result_event):
    """
    Given a parsed stream-json "result" event, return True if the session
    was cut off because it hit --max-turns, False if it ended normally, or
    None if this can't be determined from this event's shape.

    Based on real result events collected 2026-08-03:

    - A session that got stuck polling in a loop (waiting on a slow Gradle
      run) but quit on its own before any turn cap looked like a totally
      normal completion -- {"subtype": "success", "is_error": false,
      "terminal_reason": "completed", "num_turns": 55, ...}. Nothing about
      that event distinguishes it from a genuinely finished session; this
      function correctly returns False for it. This is why the turn-limit
      message below is only a heuristic, not a guarantee it catches every
      "actually stuck" session -- it only catches ones that ran the cap
      all the way out instead of quitting first.
    - A session actually cut off by `--max-turns` (reproduced by running
      `claude -p ... --max-turns 1` against a multi-tool-call prompt)
      looked like -- {"subtype": "error_max_turns", "is_error": true,
      "terminal_reason": "max_turns", "num_turns": 2,
      "errors": ["Reached maximum number of turns (1)"], ...}.

    If a future CLI version renames these fields, this falls back to None
    (unknown) rather than raising or guessing.
    """
    try:
        subtype = result_event.get("subtype")
        terminal_reason = result_event.get("terminal_reason")
    except AttributeError:
        return None

    if subtype == "error_max_turns" or terminal_reason == "max_turns":
        return True
    if subtype == "success" or terminal_reason == "completed":
        return False
    return None


_caffeinate_warning_printed = False


def start_caffeinate_for_row():
    """
    Start a `caffeinate -dim` background process so the Mac can't sleep
    during one row's Claude session (-d display, -i idle, -m disk -- this
    covers a long test/build run without the user needing to run
    caffeinate manually). Returns the Popen object, or None if caffeinate
    isn't available on this machine (e.g. not macOS) -- in that case a
    one-line warning is printed once, the first time it happens, not on
    every row.
    """
    global _caffeinate_warning_printed
    try:
        return subprocess.Popen(["caffeinate", "-dim"])
    except FileNotFoundError:
        if not _caffeinate_warning_printed:
            print("caffeinate not available -- sleep prevention disabled")
            _caffeinate_warning_printed = True
        return None


def stop_caffeinate_for_row(caffeinate_process):
    """
    Stop a caffeinate process started for one row, if any. Tries a clean
    terminate() first; if it doesn't exit within 5 seconds, kill() it so
    it never lingers between rows or after the script exits.
    """
    if caffeinate_process is None:
        return
    caffeinate_process.terminate()
    try:
        caffeinate_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        caffeinate_process.kill()
        caffeinate_process.wait()


# ======================================================================
# ANDROID EMULATOR LIFECYCLE (for --emulator-avd)
# ======================================================================
#
# Booted once for the whole run (not once per row -- a cold boot is ~60-90s)
# only if nothing is already attached, and only shut down again afterward if
# this script is the one that started it. An emulator/device that was
# already running before the run started is left exactly as it was found.

def find_android_sdk_tool(tool_relative_path):
    """
    Locate an Android SDK command-line tool (e.g. "platform-tools/adb" or
    "emulator/emulator"). Checks $ANDROID_HOME / $ANDROID_SDK_ROOT first,
    then the common macOS default install location, then falls back to the
    bare tool name and lets the shell's PATH resolve it.
    """
    for env_var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        sdk_root = os.environ.get(env_var)
        if sdk_root:
            candidate = Path(sdk_root).expanduser() / tool_relative_path
            if candidate.exists():
                return str(candidate)

    default_candidate = Path.home() / "Library" / "Android" / "sdk" / tool_relative_path
    if default_candidate.exists():
        return str(default_candidate)

    return Path(tool_relative_path).name


def list_attached_android_devices(adb_binary):
    """
    Returns the serials of every device/emulator currently visible to adb
    and ready to use (adb reports them with state "device"; "offline" and
    "unauthorized" entries are excluded since they can't run tests yet).
    """
    try:
        result = subprocess.run(
            [adb_binary, "devices"], capture_output=True, text=True, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    serials = []
    for line in result.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def wait_for_emulator_boot(adb_binary, serial, timeout_seconds=180):
    """
    Polls `adb -s <serial> shell getprop sys.boot_completed` every 3
    seconds until it returns "1" or timeout_seconds elapses. Returns True
    if boot completed in time, False otherwise.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [adb_binary, "-s", serial, "shell", "getprop", "sys.boot_completed"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.stdout.strip() == "1":
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        time.sleep(3)
    return False


def ensure_emulator_running(avd_name, adb_binary, emulator_binary):
    """
    Makes sure exactly one Android device/emulator is available for
    on-device verification steps, starting one only if nothing is already
    attached.

    Returns {"started_by_us": bool, "serial": str or None}. If a
    device/emulator was already attached, it's reused untouched and
    started_by_us is False -- this run must never close something it
    didn't open. Otherwise avd_name is booted, the boot wait is
    caffeinated (a ~60-90s cold boot is long enough for the Mac to try to
    sleep), and started_by_us is True.
    """
    existing = list_attached_android_devices(adb_binary)
    if existing:
        print(
            f"Android device/emulator already attached ({', '.join(existing)}) -- "
            "reusing it, and leaving it running when this run ends."
        )
        return {"started_by_us": False, "serial": existing[0]}

    print(f"No Android device/emulator attached. Booting '{avd_name}' for on-device verification...")
    caffeinate_process = start_caffeinate_for_row()
    try:
        subprocess.Popen(
            [emulator_binary, "-avd", avd_name, "-no-snapshot"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # The serial usually shows up in `adb devices` well before
        # sys.boot_completed=1, so wait for that first.
        serial = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and serial is None:
            attached = list_attached_android_devices(adb_binary)
            if attached:
                serial = attached[0]
            else:
                time.sleep(3)

        if serial is None:
            print(
                "WARNING: the emulator did not appear in `adb devices` within 60s. "
                "On-device verification steps this run may not work."
            )
            return {"started_by_us": False, "serial": None}

        if wait_for_emulator_boot(adb_binary, serial):
            print(f"Emulator '{avd_name}' booted ({serial}).")
        else:
            print(
                f"WARNING: emulator '{avd_name}' ({serial}) did not finish booting "
                "within the timeout. Leaving it running, but on-device verification "
                "steps this run may not work."
            )
        return {"started_by_us": True, "serial": serial}
    finally:
        stop_caffeinate_for_row(caffeinate_process)


def shutdown_emulator(adb_binary, serial):
    """
    Cleanly shuts down the emulator this run started, via
    `adb -s <serial> emu kill`. Only ever called with a serial this script
    booted itself -- never for a device/emulator it found already running.
    """
    if serial is None:
        return
    print(f"Shutting down emulator ({serial}) started for this run...")
    try:
        subprocess.run([adb_binary, "-s", serial, "emu", "kill"], timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("Could not confirm the emulator shut down cleanly -- you may need to close it manually.")


def run_claude_session(prompt, project_path, log_path, max_turns):
    """
    Run `claude -p <prompt> --permission-mode acceptEdits --verbose
    --output-format stream-json --max-turns <max_turns>` as a subprocess
    with its working directory set to project_path.

    stream-json makes the session emit one JSON object per line (NDJSON)
    as it works, instead of one final text blob. Each line is parsed as it
    arrives and turned into a short, human-readable progress line printed
    live to the console (file edits, Bash commands, file reads, and the
    final result). Lines that fail to parse as JSON are tolerated, not
    treated as errors.

    The full original line -- ANSI-stripped, whether or not it parsed --
    is always written to log_path in full, so the log file remains the
    complete, unabridged record even though the console only shows short
    summaries. stderr is captured too.

    log_path is opened in append mode, not write/truncate: by the time
    this function runs, the caller may have already written the row's
    usage-check block to the same file (see append_lines_to_log()), and
    that content must be preserved, not wiped out.

    A `caffeinate -dim` process is started for the duration of this row's
    session only (so the Mac can't sleep mid-fix or mid-test-run) and is
    always stopped before this function returns or raises -- normal
    completion, a non-zero exit code, an exception, or KeyboardInterrupt.

    Returns a tuple of (returncode, hit_turn_limit, saw_result_event).

    hit_turn_limit is True only if the session's final "result" event
    clearly indicates it was cut off by --max-turns; False otherwise
    (including when this can't be determined -- see detect_turn_limit_hit()).

    saw_result_event is True if a stream-json "result" event was seen at
    any point. A session that finishes on its own (success, error, or even
    hitting --max-turns) always emits exactly one. A session killed from
    outside -- SIGTERM from the OS, an OOM kill, a supervisor timeout --
    aborts mid-turn and never emits one. Combined with returncode, this is
    what distinguishes "Claude Code decided to stop" from "something killed
    the process out from under it": a negative/143 returncode with
    saw_result_event False means it was killed externally, not that it hit
    --max-turns or finished normally.
    """
    command = [
        "claude",
        "-p",
        prompt,
        "--permission-mode",
        "acceptEdits",
        "--verbose",
        "--output-format",
        "stream-json",
        "--max-turns",
        str(max_turns),
    ]

    print("Preventing sleep for this row...")
    caffeinate_process = start_caffeinate_for_row()

    try:
        try:
            process = subprocess.Popen(
                command,
                cwd=project_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError:
            print("ERROR: the 'claude' command was not found on your PATH.")
            print("Install Claude Code and make sure 'claude' is runnable from your shell.")
            raise ClaudeNotFoundError("'claude' was not found on PATH")

        hit_turn_limit = False
        saw_result_event = False

        with open(log_path, "a", encoding="utf-8") as log_file:
            try:
                for line in process.stdout:
                    clean_line = strip_ansi_codes(line)
                    log_file.write(clean_line)
                    log_file.flush()

                    json_text = clean_line.strip()
                    if not json_text:
                        continue

                    try:
                        event = json.loads(json_text)
                    except ValueError:
                        # Streaming output can be noisy (partial/malformed
                        # lines). The raw text is already in the log above --
                        # just skip console progress for this one line.
                        continue

                    if event.get("type") == "result":
                        saw_result_event = True
                        if detect_turn_limit_hit(event):
                            hit_turn_limit = True

                    for console_line in console_lines_for_stream_json_event(event, project_path):
                        print(console_line)
            except KeyboardInterrupt:
                process.terminate()
                process.wait()
                raise

        process.wait()
        return process.returncode, hit_turn_limit, saw_result_event
    finally:
        if caffeinate_process is not None:
            stop_caffeinate_for_row(caffeinate_process)
            print("Sleep prevention released.")


# ======================================================================
# LOOP: run summary log file
# ======================================================================

def initialize_run_summary_file(run_summary_path, project_path, branch_name, args):
    """Create the run summary markdown file and write its header section."""
    header_lines = [
        "# Audit Verify Runner -- Run Summary",
        "",
        f"- Project: {project_path}",
        f"- Branch: {branch_name}",
        f"- Mode: {args.mode}",
        f"- Test command: {args.test_cmd or '(none provided)'}",
        f"- Severities filter: {args.severities or '(all)'}",
        f"- Max rows this run: {args.max_rows if args.max_rows is not None else '(no limit)'}",
        f"- Auto-push: {'on' if args.auto_push else 'off'}",
        f"- Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Rows processed",
        "",
    ]
    run_summary_path.write_text("\n".join(header_lines) + "\n", encoding="utf-8")


def format_run_summary_line(row_id, severity, status, note):
    """Format one line describing a single row's result, for the run summary file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    note_suffix = f" -- {note}" if note else ""
    return f"- [{timestamp}] {row_id} ({severity}): {status}{note_suffix}"


def append_to_run_summary_file(run_summary_path, line_text):
    """Append one line to the run summary file, so progress is never lost."""
    with open(run_summary_path, "a", encoding="utf-8") as summary_file:
        summary_file.write(line_text + "\n")


# ======================================================================
# POST-RUN
# ======================================================================

def print_final_summary(final_rows, rows_processed_this_run, branch_name, run_summary_path, auto_push=False):
    """Print the end-of-run summary to the console."""
    total_verified = sum(1 for row in final_rows if status_matches(row["fix_status"], STATUS_FIXED_VERIFIED))
    blocked_rows = [row for row in final_rows if status_matches(row["fix_status"], STATUS_BLOCKED)]
    total_still_unverified = sum(1 for row in final_rows if status_is_unverified(row["fix_status"]))

    print(f"Rows processed this run:        {len(rows_processed_this_run)}")
    print(f"Total Fixed (verified, all time): {total_verified}")
    print(f"Total Blocked:                   {len(blocked_rows)}")
    for row in blocked_rows:
        note_suffix = f" -- {row['notes']}" if row["notes"] else ""
        print(f"    {row['id']}{note_suffix}")
    print(f"Total still unverified:          {total_still_unverified}")
    print()
    print(f"Branch: {branch_name}")
    if auto_push:
        print(
            "--auto-push was on: each row that finished 'Fixed (verified)' was "
            "pushed to origin as it completed. Blocked/still-unverified rows "
            "were never pushed. Review with:"
        )
    else:
        print("Nothing was pushed. Review before pushing yourself, e.g.:")
    print("  git log")
    print(f"  git diff main..{branch_name}")
    print()
    print(f"Full run log: {run_summary_path}")


def append_final_summary_to_file(run_summary_path, final_rows, rows_processed_this_run, branch_name, auto_push=False):
    """Append the end-of-run summary to the run summary file (never overwrite it)."""
    total_verified = sum(1 for row in final_rows if status_matches(row["fix_status"], STATUS_FIXED_VERIFIED))
    blocked_rows = [row for row in final_rows if status_matches(row["fix_status"], STATUS_BLOCKED)]
    total_still_unverified = sum(1 for row in final_rows if status_is_unverified(row["fix_status"]))

    summary_lines = [
        "",
        "## Final summary",
        "",
        f"- Finished: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- Rows processed this run: {len(rows_processed_this_run)}",
        f"- Total Fixed (verified, all time): {total_verified}",
        f"- Total Blocked: {len(blocked_rows)}",
    ]
    for row in blocked_rows:
        note_suffix = f" -- {row['notes']}" if row["notes"] else ""
        summary_lines.append(f"    - {row['id']}{note_suffix}")
    summary_lines.append(f"- Total still unverified: {total_still_unverified}")
    branch_suffix = "auto-pushed 'Fixed (verified)' rows as they completed" if auto_push else "not pushed"
    summary_lines.append(f"- Branch: {branch_name} ({branch_suffix})")

    with open(run_summary_path, "a", encoding="utf-8") as summary_file:
        summary_file.write("\n".join(summary_lines) + "\n")


def build_notification_text(rows_processed_this_run, final_rows):
    """Build a short one-line summary for the macOS notification."""
    total_verified = sum(1 for row in final_rows if status_matches(row["fix_status"], STATUS_FIXED_VERIFIED))
    total_blocked = sum(1 for row in final_rows if status_matches(row["fix_status"], STATUS_BLOCKED))
    return (
        f"Processed {len(rows_processed_this_run)} row(s). "
        f"Verified: {total_verified}, Blocked: {total_blocked}."
    )


def send_mac_notification(message_text):
    """
    Send a macOS notification via osascript. Never lets a notification
    failure crash the script or hide the console summary.
    """
    try:
        safe_text = message_text.replace('"', "'")
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe_text}" with title "Audit Verify Runner"'],
            capture_output=True,
        )
    except Exception:
        pass


# ======================================================================
# MAIN
# ======================================================================

def main():
    args = parse_arguments()
    pause_interval = compute_pause_interval(args.pause_every, args.max_rows)

    print_banner("AUDIT VERIFY RUNNER -- PRE-FLIGHT", color=TermColors.BLUE)

    project_path = Path(args.project).expanduser().resolve()
    audit_path = validate_project_path(project_path)

    print_project_context(project_path)

    confirm_git_repo(project_path)
    confirm_no_sibling_runner_active(project_path, "audit_verify_runner.py", ["audit_fix_runner.py"])
    confirm_clean_working_tree(project_path)
    confirm_remote_origin(project_path)
    print()

    branch_name = args.branch or f"audit-{datetime.now().strftime('%Y-%m-%d')}"
    original_branch = get_current_branch(project_path)
    branch_already_existed = branch_exists_locally(project_path, branch_name)
    checkout_or_create_branch(project_path, branch_name)

    rows = parse_master_tracking_table(audit_path)
    summarize_rows(rows)

    if not args.test_cmd:
        confirm_missing_test_command()

    severities_filter = None
    if args.severities:
        severities_filter = [severity for severity in args.severities.split(",") if severity.strip()]

    ordered_rows = select_rows_to_process(rows, severities_filter, args.max_rows)

    if args.dry_run:
        print_dry_run_plan(branch_name, ordered_rows)
        restore_branch_after_dry_run(project_path, original_branch, branch_name, branch_already_existed)
        sys.exit(0)

    if not ordered_rows:
        print("No unverified rows match your filters. Nothing to do.")
        sys.exit(0)

    logs_folder = ensure_logs_folder(project_path)
    start_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_summary_path = logs_folder / f"run_summary_{start_timestamp}.md"
    initialize_run_summary_file(run_summary_path, project_path, branch_name, args)

    # Boot the emulator once, before the first row, so on-device
    # verification steps never each pay their own ~60-90s cold-boot cost --
    # and so nothing this run needs to babysit whether it's already open.
    adb_binary = None
    emulator_state = {"started_by_us": False, "serial": None}
    if args.emulator_avd:
        adb_binary = find_android_sdk_tool("platform-tools/adb")
        emulator_binary = find_android_sdk_tool("emulator/emulator")
        emulator_state = ensure_emulator_running(args.emulator_avd, adb_binary, emulator_binary)
        if emulator_state["started_by_us"]:
            # atexit (not a try/finally around the row loop below) so this
            # still fires on KeyboardInterrupt, ClaudeNotFoundError, an
            # early sys.exit(), or any other uncaught exception -- an
            # emulator this run started must never be left orphaned no
            # matter how the run ends.
            atexit.register(shutdown_emulator, adb_binary, emulator_state["serial"])

    should_run_loop, before_usage = run_start_usage_check(args.usage_warn_threshold, run_summary_path)

    print_banner("AUDIT VERIFY RUNNER -- LOOP", color=TermColors.BLUE)

    test_cmd_for_prompt = args.test_cmd or "(no test command was provided -- find and run the project's existing test suite yourself)"

    rows_processed_this_run = []
    rows_since_last_pause = 0
    exit_code = 0

    if not should_run_loop:
        print("Stopping at your request -- usage was already at or above the warning threshold.\n")

    try:
        for position, row in enumerate(ordered_rows if should_run_loop else [], start=1):
            # Track which row is being worked on *before* touching git or
            # starting the subprocess, so any error can name this row.
            current_row_id = row["id"]

            confirm_clean_working_tree(project_path, current_row_id=current_row_id)

            print_banner(f"Row {position}/{len(ordered_rows)}: {current_row_id} ({row['severity']})")
            print(f"Rows completed so far this run: {len(rows_processed_this_run)}")

            log_path = build_row_log_path(logs_folder, current_row_id)

            prompt = build_prompt(current_row_id, branch_name, test_cmd_for_prompt)

            print(f"Starting Claude session. Logging to: {log_path}\n")

            returncode, hit_turn_limit, saw_result_event = run_claude_session(
                prompt, project_path, log_path, args.max_turns
            )
            # returncode -15 (SIGTERM) or 143 (128 + SIGTERM) with no "result"
            # event ever seen means the process was killed from outside --
            # by the OS (OOM), a supervisor, or some other external timeout --
            # rather than Claude Code itself deciding to stop (which always
            # emits a final "result" event, even for --max-turns or an error).
            likely_killed_externally = returncode in (-15, 143) and not saw_result_event

            fresh_rows = parse_master_tracking_table(audit_path)
            updated_row = find_row_by_id(fresh_rows, current_row_id)

            if updated_row is None:
                result_status = "UNKNOWN (row not found after run)"
                result_note = ""
            else:
                result_status = updated_row["fix_status"] or "(blank)"
                result_note = updated_row["notes"]

            if status_matches(result_status, STATUS_FIXED_VERIFIED):
                result_color = TermColors.GREEN
            elif status_matches(result_status, STATUS_BLOCKED):
                result_color = TermColors.YELLOW
            else:
                result_color = TermColors.RED
            print(f"\nResult: {current_row_id} -> {result_color}{result_status}{TermColors.RESET}")
            if result_note:
                print(f"Note: {result_note}")
            if likely_killed_externally:
                print(
                    f"{TermColors.RED}This row's Claude session was killed from outside "
                    f"(exit code {returncode}, no final 'result' event ever seen in the "
                    f"log) -- not by --max-turns and not by Claude Code choosing to stop. "
                    "Likely causes: the OS killed it (e.g. out of memory), the machine "
                    "slept despite caffeinate, or an external supervisor/timeout. This is "
                    f"NOT a sign the verification was hard.{TermColors.RESET}"
                )

            if args.auto_push and status_matches(result_status, STATUS_FIXED_VERIFIED):
                push_branch_to_origin(project_path, branch_name)

            if status_is_unverified(result_status):
                if hit_turn_limit:
                    print(
                        f"This row hit the --max-turns limit ({args.max_turns}) before "
                        "finishing. It likely got stuck (e.g. waiting on a slow command) "
                        "rather than being genuinely hard to verify. Check the log file, "
                        "and consider re-running this row by hand with more turns, or "
                        "investigating why it stalled."
                    )
                elif not likely_killed_externally:
                    print("This row is still unverified -- the session may not have finished correctly.")

            summary_note = result_note
            if likely_killed_externally:
                summary_note = (
                    f"{result_note} -- " if result_note else ""
                ) + f"KILLED EXTERNALLY (exit code {returncode}, no result event)"

            rows_processed_this_run.append(
                {
                    "id": current_row_id,
                    "severity": row["severity"],
                    "status": result_status,
                    "note": summary_note,
                }
            )
            append_to_run_summary_file(
                run_summary_path,
                format_run_summary_line(current_row_id, row["severity"], result_status, summary_note),
            )

            if args.max_rows is not None and len(rows_processed_this_run) >= args.max_rows:
                print(f"\nReached --max-rows limit of {args.max_rows}. Stopping (not necessarily finished).")
                break

            rows_since_last_pause += 1

            if args.mode == "manual" and pause_interval is not None and rows_since_last_pause % pause_interval == 0:
                user_choice = input(
                    f"\n{rows_since_last_pause} row(s) completed since last pause. Press Enter to "
                    "continue to the next batch, or type 'q' then Enter to stop here: "
                )
                rows_since_last_pause = 0
                if user_choice.strip().lower().startswith("q"):
                    print("Stopping at your request.")
                    break

    except KeyboardInterrupt:
        print("\n\nInterrupted (Ctrl+C).")
        print("Nothing is lost: AUDIT.md and git reflect whatever was last completed.")
        print("Run this exact same command again to resume where you left off")
        print(f"(branch '{branch_name}' will be reused).")

    except ClaudeNotFoundError:
        print("\n\nStopping early: 'claude' is not available right now.")
        print("Nothing is lost: AUDIT.md and git reflect whatever was last completed.")
        print("Once 'claude' is back on your PATH, run this exact same command again")
        print(f"to resume (branch '{branch_name}' will be reused).")
        exit_code = 1

    print_banner("AUDIT VERIFY RUNNER -- POST-RUN", color=TermColors.BLUE)

    run_end_usage_check(run_summary_path, before_usage=before_usage)

    final_rows = parse_master_tracking_table(audit_path)
    print_final_summary(final_rows, rows_processed_this_run, branch_name, run_summary_path, auto_push=args.auto_push)
    append_final_summary_to_file(run_summary_path, final_rows, rows_processed_this_run, branch_name, auto_push=args.auto_push)
    send_mac_notification(build_notification_text(rows_processed_this_run, final_rows))

    sys.exit(exit_code)


if __name__ == "__main__":
    main()

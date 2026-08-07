#!/usr/bin/env python3
"""
Audit Generator
===============

Turns a bare project path into a fully-populated AUDIT.md, without you
copy-pasting phase prompts between Claude Code sessions by hand.

Runs four stages, each a fresh, memory-free headless Claude Code session
(via engine_registry.py) -- so context never dilutes across a long audit
the way it would in one giant session:

    Stage 0  Project Scan          -> audit-gen/PROJECT_PROFILE.md,
                                       audit-gen/CONTEXT_MANIFEST.md
    Stage 1  Competitive Research  -> audit-gen/RESEARCH_NOTES.md
    Stage 2  Audit Prompt Build    -> audit-gen/AUDIT_PROMPT.md
    Stage 3  Phased Execution      -> audit-gen/staging/phase-*.json,
                                       then compiled (audit_compiler.py,
                                       plain Python, no AI) into AUDIT.md

AUDIT.md is exactly the file audit_fix_runner.py and audit_verify_runner.py
(unchanged, existing tools) already expect -- run one of those next.

Every stage checks whether its own output already exists before running,
so this script is safe to re-run after an interruption, a usage-limit
pause, or a deliberate stop for --review-required: already-done stages
(and, within Stage 3, already-done phases) are skipped, not redone.

See SCHEMA.md in this same folder for the exact file formats this script
produces and depends on.

Example:
    python3 audit_generator.py --project ~/code/my-app

Run with --help to see every available option.
"""

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import audit_compiler
import engine_registry as er

# state_store.py lives in the sibling pipeline-src/ folder, not this one --
# it's deliberately dependency-free of audit_generator.py/engine_registry.py
# so it can be tested and reasoned about on its own (see its own docstring).
# This is the one place that direction is crossed, since Stage 3's per-phase
# boundaries are the natural, safe granularity for a usage-limit pause, and
# only this script's stage loop actually knows where those boundaries are.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "pipeline-src"))
import state_store as ss


# ======================================================================
# CONSTANTS
# ======================================================================

BANNER_WIDTH = 100
DEFAULT_MAX_TURNS = 50


class TermColors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"


def print_banner(title, color=TermColors.CYAN):
    width = BANNER_WIDTH
    print(f"\n{color}{TermColors.BOLD}=={title.center(width - 4)}=={TermColors.RESET}\n")


class GeneratorError(Exception):
    """Raised for any condition that should stop the run with a clear message."""


class PausedForQuotaError(Exception):
    """
    Raised (and caught in main(), handled distinctly from GeneratorError --
    this is an expected, graceful stop, not a failure) when usage crosses
    --hard-pause-threshold at a stage/phase boundary. The run's state is
    already recorded in state_store by the time this is raised.
    """


FALLBACK_RESUME_DELAY_HOURS = 6


def check_hard_pause(engine, project_path, stage_label, run_args, auto_resume, hard_pause_threshold, action=None, db_path=None):
    """
    Check usage against hard_pause_threshold at a stage/phase boundary
    (never mid-session -- see the module docstring's note on why this is
    the granularity actually checked). If either session or week usage is
    at or above the threshold, record PAUSED_QUOTA in state_store (with
    auto_resume as given -- False still records the pause for visibility,
    it just means state_store.due_paused_projects() will never pick it up
    automatically) and raise PausedForQuotaError.

    resume_at is parsed from whichever of session_reset/week_reset
    actually crossed the threshold (the later of the two, if both did,
    since both must clear). If neither reset string parses, falls back to
    now + FALLBACK_RESUME_DELAY_HOURS rather than pausing forever with no
    resume time at all -- logged clearly so it's not mistaken for a real
    reset time.
    """
    usage = engine.check_usage()
    session_over = (usage.session_percent or 0) >= hard_pause_threshold
    week_over = (usage.week_percent or 0) >= hard_pause_threshold
    if not session_over and not week_over:
        return

    candidates = []
    if session_over:
        candidates.append(ss.parse_reset_timestamp(usage.session_reset))
    if week_over:
        candidates.append(ss.parse_reset_timestamp(usage.week_reset))
    candidates = [c for c in candidates if c is not None]

    if candidates:
        resume_at_dt = max(candidates)
    else:
        from datetime import timedelta, timezone

        resume_at_dt = datetime.now(timezone.utc) + timedelta(hours=FALLBACK_RESUME_DELAY_HOURS)
        print(
            f"(Could not parse a reset time from usage output -- falling back to a "
            f"{FALLBACK_RESUME_DELAY_HOURS}-hour retry delay.)"
        )

    reason = (
        f"session {usage.session_percent}% used"
        if session_over
        else f"week {usage.week_percent}% used"
    )
    ss.mark_paused_quota(
        project_path, stage_label, reason, resume_at_dt, action=action, run_args=run_args, auto_resume=auto_resume, db_path=db_path
    )
    print(f"\n{TermColors.YELLOW}Usage at or above --hard-pause-threshold ({hard_pause_threshold}%): {reason}.{TermColors.RESET}")
    print(f"Pausing before {stage_label}. Recorded in state_store, resume time: {resume_at_dt}.")
    if auto_resume:
        print("--auto-resume is on: this will resume automatically once a scheduled check finds it's due.")
    else:
        print("--auto-resume is off: re-run this same command yourself once usage has reset.")
    raise PausedForQuotaError(reason)


# ======================================================================
# PRE-FLIGHT: CLI args
# ======================================================================

def parse_arguments():
    parser = argparse.ArgumentParser(
        prog="audit_generator.py",
        description=(
            "Generate a project's AUDIT.md from scratch: scan the project, research "
            "comparable projects online, build a phased audit prompt, then execute it "
            "phase by phase (each phase a fresh Claude Code session) and compile the "
            "results into AUDIT.md."
        ),
    )
    parser.add_argument("--project", required=True, help="Path to the project root.")
    parser.add_argument(
        "--branch",
        default=None,
        help="Git branch to work on. Default: audit-gen-<today's date>. Reused if it already exists (resume).",
    )
    parser.add_argument(
        "--max-turns",
        dest="max_turns",
        type=int,
        default=DEFAULT_MAX_TURNS,
        help=f"Cap each stage/phase session at N agent turns. Default: {DEFAULT_MAX_TURNS}. "
        "Research-heavy phases may need more headroom than this if they time out.",
    )
    parser.add_argument(
        "--review-required",
        dest="review_required",
        action="store_true",
        help=(
            "Pause for your confirmation twice: right after AUDIT_PROMPT.md is generated "
            "(Stage 2), so you can review or hand-edit it before it's executed, and again "
            "after every phase has been staged (end of Stage 3), before findings are "
            "compiled into AUDIT.md. Off by default."
        ),
    )
    parser.add_argument(
        "--auto-push",
        dest="auto_push",
        action="store_true",
        help="Push this branch to origin after each stage/phase commits. Off by default -- nothing is ever pushed automatically unless this is set.",
    )
    parser.add_argument(
        "--usage-warn-threshold",
        dest="usage_warn_threshold",
        type=int,
        default=90,
        help="Warn if Claude usage is at or above N percent before starting. Default: 90.",
    )
    parser.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="Print which stages/phases are already done vs. still pending, and exit without running anything.",
    )
    parser.add_argument(
        "--hard-pause-threshold",
        dest="hard_pause_threshold",
        type=int,
        default=97,
        help=(
            "If session or week usage is at or above N percent at a stage/phase boundary, "
            "pause automatically (recorded in pipeline-src/pipeline_state.db) instead of "
            "starting that stage/phase. Default: 97. Distinct from --usage-warn-threshold, "
            "which only asks for confirmation once, before the run starts."
        ),
    )
    parser.add_argument(
        "--auto-resume",
        dest="auto_resume",
        action="store_true",
        help=(
            "If a --hard-pause-threshold pause happens, mark it for automatic resume once "
            "usage resets -- pipeline.py --check-resume (meant to be pinged periodically by "
            "cron/launchd) will then pick it back up on its own. Off by default: a pause is "
            "still recorded either way, but without this, only re-running this same command "
            "yourself resumes it."
        ),
    )
    return parser.parse_args()


# ======================================================================
# PRE-FLIGHT: project & git checks
# ======================================================================

def run_git_command(project_path, git_args):
    try:
        result = subprocess.run(["git"] + git_args, cwd=project_path, capture_output=True, text=True)
    except FileNotFoundError:
        raise GeneratorError("git was not found on your PATH.")
    return result.returncode == 0, result.stdout.rstrip("\n"), result.stderr.strip()


def confirm_git_repo(project_path):
    succeeded, stdout_text, _ = run_git_command(project_path, ["rev-parse", "--is-inside-work-tree"])
    if not succeeded or stdout_text != "true":
        raise GeneratorError(f"{project_path} does not look like a git repository.")


def get_current_branch(project_path):
    succeeded, stdout_text, _ = run_git_command(project_path, ["branch", "--show-current"])
    if not succeeded or stdout_text == "":
        raise GeneratorError("Could not determine the current git branch.")
    return stdout_text


def branch_exists_locally(project_path, branch_name):
    _, stdout_text, _ = run_git_command(project_path, ["branch", "--list", branch_name])
    return stdout_text != ""


def checkout_or_create_branch(project_path, branch_name):
    if branch_exists_locally(project_path, branch_name):
        print(f"Branch '{branch_name}' already exists -- checking it out to resume.")
        succeeded, _, stderr_text = run_git_command(project_path, ["checkout", branch_name])
    else:
        base_branch = get_current_branch(project_path)
        print(f"Creating new branch '{branch_name}' from '{base_branch}'.")
        succeeded, _, stderr_text = run_git_command(project_path, ["checkout", "-b", branch_name])
    if not succeeded:
        raise GeneratorError(f"Could not check out branch '{branch_name}':\n{stderr_text}")
    print(f"On branch: {branch_name}\n")


def commit_paths(project_path, relative_paths, message):
    """
    Stage exactly the given relative paths (never a broad `git add -A`) and
    commit. Silently does nothing if none of those paths actually changed
    (e.g. re-running after a stage was already committed) -- checked via
    `git status --porcelain` on just those paths, not the whole tree,
    since this tool must never assume the rest of the working tree is
    clean (unlike audit_fix_runner.py, this tool never touches existing
    source files, so a dirty tree elsewhere is none of its business).
    """
    _, status_text, _ = run_git_command(project_path, ["status", "--porcelain", "--"] + relative_paths)
    if not status_text.strip():
        return False
    add_succeeded, _, add_stderr = run_git_command(project_path, ["add", "--"] + relative_paths)
    if not add_succeeded:
        raise GeneratorError(f"git add failed for {relative_paths}:\n{add_stderr}")
    commit_succeeded, _, commit_stderr = run_git_command(project_path, ["commit", "-m", message])
    if not commit_succeeded:
        raise GeneratorError(f"git commit failed for {relative_paths}:\n{commit_stderr}")
    return True


def push_branch_to_origin(project_path, branch_name):
    succeeded, _, stderr_text = run_git_command(project_path, ["push", "origin", branch_name])
    if succeeded:
        print(f"{TermColors.GREEN}Pushed to origin/{branch_name}.{TermColors.RESET}")
    else:
        print(f"{TermColors.RED}git push failed -- continuing, the local commit is still safe.{TermColors.RESET}")
        if stderr_text:
            print(stderr_text)


# ======================================================================
# USAGE CHECK (lighter-weight version of the sibling tools' usage bars)
# ======================================================================

def check_and_report_usage(engine, label, warn_threshold=None):
    usage = engine.check_usage()
    if usage.session_percent is None and usage.week_percent is None:
        print(f"[{label}] Could not retrieve usage.")
        return usage
    print(
        f"[{label}] session {usage.session_percent}% used"
        + (f" (resets {usage.session_reset})" if usage.session_reset else "")
    )
    print(
        f"[{label}] week    {usage.week_percent}% used"
        + (f" (resets {usage.week_reset})" if usage.week_reset else "")
    )
    if warn_threshold is not None:
        session_percent = usage.session_percent or 0
        week_percent = usage.week_percent or 0
        if session_percent >= warn_threshold or week_percent >= warn_threshold:
            answer = input(f"\nUsage is at or above {warn_threshold}% -- start anyway? [y/N]: ")
            if answer.strip().lower() != "y":
                raise GeneratorError("Stopped at your request -- usage was already high.")
    return usage


# ======================================================================
# STAGE 0/1/2 PROMPTS (fixed, code-authored)
# ======================================================================

STAGE0_PROMPT = """You are auditing a software project you have never seen before. Explore the
project at the root of your current working directory (read config/manifest
files, package/dependency definitions, build scripts, folder structure, and
enough source to understand it -- do not just read file names, actually open
representative files).

Determine:
- What the project is (its purpose, in plain language).
- Its full tech stack: languages, frameworks, key libraries, build tools.
- Its output surface(s): website, PWA, native Android app, native iOS app,
  local/self-hosted server, CLI tool, library, or some mix -- be specific,
  and say if it's more than one.
- Its overall architecture at a high level (major modules/layers, how they
  connect).
- Anything you can find about an existing design system: color palette,
  typography, spacing scale, component library -- only if genuinely present
  in the code, never invented.

Write two files:

1. audit-gen/PROJECT_PROFILE.md -- a thorough write-up of everything above,
   with enough detail that someone auditing this project's UI, performance,
   security, and architecture would have what they need, without having to
   re-explore the codebase themselves.

2. audit-gen/CONTEXT_MANIFEST.md -- a SHORT (aim for well under one page)
   distillation of only the facts every later audit phase will need at a
   glance: the tech stack in a few lines, the output surface(s), and (if
   found) the design tokens. This file will be read by many separate future
   sessions that each have a limited turn budget -- keep it lean on purpose,
   it is not a place for detail (that's what PROJECT_PROFILE.md is for).

Do not run any git commands. Do not modify any existing project files."""


STAGE1_PROMPT_TEMPLATE = """You are researching comparable projects and current best practices to
inform a software audit. Read audit-gen/CONTEXT_MANIFEST.md first for the
project's tech stack and output surface.

Using web search, research:
- A small number of comparable/competing products or projects (similar
  purpose and/or platform) and what they do well or poorly.
- Current best practices and standards relevant to this project's specific
  output surface(s) -- e.g. platform design guidelines (Material Design /
  Apple HIG / web), accessibility standards (WCAG), performance benchmarks
  typical for this kind of app, offline/sync patterns, relevant
  legal/privacy baseline expectations (e.g. GDPR-style data handling) if
  applicable to what this project does.
- Anything else a thorough, up-to-date audit of this specific kind of
  project should be checking for in 2026 that a purely offline read of the
  code wouldn't surface on its own.

Write audit-gen/RESEARCH_NOTES.md summarizing what you found, organized so
each point states a concrete, actionable implication for what to look for
during the audit (not just "competitor X has feature Y" on its own, but
what that suggests should be checked in this project).

Do not run any git commands. Do not modify any existing project files."""


STAGE2_PROMPT_TEMPLATE = """You are designing the execution plan for a multi-phase software audit.
Read, in full: audit-gen/PROJECT_PROFILE.md, audit-gen/CONTEXT_MANIFEST.md,
and audit-gen/RESEARCH_NOTES.md.

Decide how to split a full, 360-degree audit of this specific project into
somewhere between 4 and 8 phases, each with a focused category (only
include phases that are actually relevant to this project's real tech
stack and output surface -- e.g. do not include an "Android on-device"
phase for a project that isn't Android). Typical categories to draw from,
adapt freely: UI/UX & Visual Design, Performance, Accessibility, Security,
Backend/API & Data Integrity, Database & Offline Capability,
Legal/Compliance & Privacy, DevEx/Testing/Documentation.

Write audit-gen/AUDIT_PROMPT.md with EXACTLY this structure, one block per
phase, nothing else at the top level:

## Phase <N>: <Title>

**Category focus:** <category>
**Engine:** claude

**Objective:** <one paragraph: what this phase is trying to find and why it
matters for this specific project>

**Prompt:**
```
<the full instructions for that phase's investigation -- what to read,
what to look for, how deep to go, and (for phases where it's relevant)
that it should use web search to compare against what you found in
audit-gen/RESEARCH_NOTES.md. Do NOT include instructions here about how or
where to write output files -- that part is handled automatically by the
script that runs this phase. Focus entirely on what to investigate.>
```

Number phases sequentially starting at 1. Every phase's Prompt block must
be self-contained: assume the session executing it has no memory of this
planning session and only your written instructions to go on.

Do not run any git commands. Do not modify any existing project files."""


PHASE_HEADING_PATTERN = re.compile(r"^##\s*Phase\s+(\d+)\s*:\s*(.+?)\s*$", re.MULTILINE)
ENGINE_LINE_PATTERN = re.compile(r"\*\*Engine:\*\*\s*(.+)")
PROMPT_FENCE_PATTERN = re.compile(r"\*\*Prompt:\*\*\s*```(?:[a-zA-Z]*\n)?(.*?)```", re.DOTALL)


def parse_audit_prompt_phases(audit_prompt_text):
    """
    Parse AUDIT_PROMPT.md (SCHEMA.md section 1) into an ordered list of
    {phase_number, title, engine, prompt_text} dicts. Raises GeneratorError
    naming exactly what's wrong -- an unparseable phase is never silently
    skipped, since that would mean a chunk of the audit silently never
    happens.
    """
    headings = list(PHASE_HEADING_PATTERN.finditer(audit_prompt_text))
    if not headings:
        raise GeneratorError(
            "No '## Phase N: Title' headings found in AUDIT_PROMPT.md. Check its formatting "
            "against SCHEMA.md section 1."
        )

    phases = []
    for index, heading_match in enumerate(headings):
        phase_number = int(heading_match.group(1))
        title = heading_match.group(2).strip()
        block_start = heading_match.end()
        block_end = headings[index + 1].start() if index + 1 < len(headings) else len(audit_prompt_text)
        block_text = audit_prompt_text[block_start:block_end]

        engine_match = ENGINE_LINE_PATTERN.search(block_text)
        engine_name = engine_match.group(1).strip().lower() if engine_match else "claude"

        prompt_match = PROMPT_FENCE_PATTERN.search(block_text)
        if not prompt_match:
            raise GeneratorError(
                f"Phase {phase_number} ('{title}') has no fenced ```Prompt:``` block. "
                "Check AUDIT_PROMPT.md against SCHEMA.md section 1."
            )
        prompt_text = prompt_match.group(1).strip()
        if not prompt_text:
            raise GeneratorError(f"Phase {phase_number} ('{title}')'s Prompt block is empty.")

        phases.append(
            {"phase_number": phase_number, "title": title, "engine": engine_name, "prompt_text": prompt_text}
        )

    return phases


def slugify(text, max_length=30):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].strip("-") or "phase"


def staging_path_for_phase(project_path, phase):
    slug = slugify(phase["title"])
    filename = f"phase-{phase['phase_number']:02d}-{slug}.json"
    return project_path / "audit-gen" / "staging" / filename


def build_phase_harness_suffix(phase, staging_relative_path):
    """
    The fixed, code-generated instruction appended to every phase's
    prompt (see SCHEMA.md section 2-3, and the module docstring above for
    why this is never left to Stage 2's own writing). Kept deliberately
    verbatim-example-driven, not just descriptive, since an AI session
    following an exact worked example is more reliable than one inferring
    a schema from prose.
    """
    return f"""

---
When you finish investigating, report your findings by writing a SINGLE file
at exactly this path (relative to your current working directory):

    {staging_relative_path}

Its content must be exactly this JSON shape (write valid JSON, no trailing
commas, no comments):

{{
  "phase_number": {phase['phase_number']},
  "phase_title": "{phase['title']}",
  "findings": [
    {{
      "local_id": "phase{phase['phase_number']:02d}-001",
      "category": "<short category label, e.g. UI/UX, Performance, Security>",
      "severity": "P0",
      "finding": "<one or two sentences: what's wrong and where>",
      "notes": "<optional: how you found it, or extra context>"
    }}
  ]
}}

- severity must be one of: P0 (highest), P1, P2, P3.
- local_id only needs to be unique within this one file -- number your
  findings phase{phase['phase_number']:02d}-001, phase{phase['phase_number']:02d}-002, etc.
- If you genuinely find nothing in this category, still write the file,
  with "findings": [] -- an empty list, not a missing file. A missing file
  is treated as this phase having failed to run, not as "nothing found".
- Do not write to AUDIT.md, or to any other file under audit-gen/, or run
  any git commands. This file is the only output expected from you."""


# ======================================================================
# STAGE RUNNERS
# ======================================================================

def run_stage_session(engine_name, prompt, project_path, log_path, max_turns, label):
    engine = er.get_engine(engine_name)
    print(f"Starting {label} ({engine_name} engine). Logging to: {log_path}\n")
    result = engine.run_session(prompt=prompt, cwd=project_path, log_path=log_path, max_turns=max_turns)

    if result.status == er.STATUS_KILLED_EXTERNALLY:
        raise GeneratorError(
            f"{label}'s session was killed from outside (exit code {result.returncode}, no "
            f"final result event) -- not a normal finish. Check {log_path}, then re-run this "
            "same command to retry (already-completed stages/phases are skipped automatically)."
        )
    if result.status == er.STATUS_TURN_LIMIT:
        raise GeneratorError(
            f"{label} hit the --max-turns limit ({max_turns}) before finishing. It may have "
            f"gotten stuck rather than genuinely needing more turns. Check {log_path}, then "
            "re-run with a higher --max-turns to retry just this stage/phase."
        )
    if result.status == er.STATUS_ERROR:
        raise GeneratorError(f"{label}'s session exited with an error (code {result.returncode}). Check {log_path}.")

    print(f"{label}: session finished normally.")
    return result


def ensure_logs_folder(project_path):
    logs_folder = project_path / "logs"
    logs_folder.mkdir(exist_ok=True)
    return logs_folder


def build_stage_log_path(logs_folder, stage_label):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", stage_label)
    return logs_folder / f"{timestamp}_{safe_label}.log"


def run_stage0(project_path, logs_folder, max_turns, auto_push, branch_name, pause_ctx):
    profile_path = project_path / "audit-gen" / "PROJECT_PROFILE.md"
    manifest_path = project_path / "audit-gen" / "CONTEXT_MANIFEST.md"
    if profile_path.exists() and manifest_path.exists():
        print("Stage 0 (Project Scan): already done -- skipping.")
        return

    check_hard_pause(pause_ctx["engine"], project_path, "Stage 0 (Project Scan)", pause_ctx["run_args"], pause_ctx["auto_resume"], pause_ctx["hard_pause_threshold"], action=pause_ctx["action"], db_path=pause_ctx["db_path"])

    print_banner("STAGE 0: PROJECT SCAN")
    (project_path / "audit-gen").mkdir(exist_ok=True)
    log_path = build_stage_log_path(logs_folder, "stage0_scan")
    run_stage_session("claude", STAGE0_PROMPT, project_path, log_path, max_turns, "Stage 0 (Project Scan)")

    for required_path, description in [(profile_path, "PROJECT_PROFILE.md"), (manifest_path, "CONTEXT_MANIFEST.md")]:
        if not required_path.exists() or required_path.stat().st_size < 20:
            raise GeneratorError(
                f"Stage 0's session finished but {description} is missing or suspiciously "
                f"small ({required_path}). Check the log and re-run to retry."
            )

    if commit_paths(project_path, ["audit-gen/PROJECT_PROFILE.md", "audit-gen/CONTEXT_MANIFEST.md"], "audit-gen: Stage 0 project scan"):
        print("Committed Stage 0 output.")
        if auto_push:
            push_branch_to_origin(project_path, branch_name)


def run_stage1(project_path, logs_folder, max_turns, auto_push, branch_name, pause_ctx):
    notes_path = project_path / "audit-gen" / "RESEARCH_NOTES.md"
    if notes_path.exists():
        print("Stage 1 (Competitive Research): already done -- skipping.")
        return

    check_hard_pause(pause_ctx["engine"], project_path, "Stage 1 (Research)", pause_ctx["run_args"], pause_ctx["auto_resume"], pause_ctx["hard_pause_threshold"], action=pause_ctx["action"], db_path=pause_ctx["db_path"])

    print_banner("STAGE 1: COMPETITIVE & UX RESEARCH")
    log_path = build_stage_log_path(logs_folder, "stage1_research")
    run_stage_session("claude", STAGE1_PROMPT_TEMPLATE, project_path, log_path, max_turns, "Stage 1 (Research)")

    if not notes_path.exists() or notes_path.stat().st_size < 20:
        raise GeneratorError(
            f"Stage 1's session finished but RESEARCH_NOTES.md is missing or suspiciously "
            f"small ({notes_path}). Check the log and re-run to retry."
        )

    if commit_paths(project_path, ["audit-gen/RESEARCH_NOTES.md"], "audit-gen: Stage 1 competitive research"):
        print("Committed Stage 1 output.")
        if auto_push:
            push_branch_to_origin(project_path, branch_name)


def run_stage2(project_path, logs_folder, max_turns, auto_push, branch_name, review_required, pause_ctx):
    prompt_path = project_path / "audit-gen" / "AUDIT_PROMPT.md"
    already_existed = prompt_path.exists()
    if already_existed:
        print("Stage 2 (Audit Prompt Build): already done -- skipping.")
    else:
        check_hard_pause(pause_ctx["engine"], project_path, "Stage 2 (Audit Prompt Build)", pause_ctx["run_args"], pause_ctx["auto_resume"], pause_ctx["hard_pause_threshold"], action=pause_ctx["action"], db_path=pause_ctx["db_path"])

        print_banner("STAGE 2: AUDIT PROMPT BUILD")
        log_path = build_stage_log_path(logs_folder, "stage2_prompt_build")
        run_stage_session("claude", STAGE2_PROMPT_TEMPLATE, project_path, log_path, max_turns, "Stage 2 (Audit Prompt Build)")

        if not prompt_path.exists() or prompt_path.stat().st_size < 20:
            raise GeneratorError(
                f"Stage 2's session finished but AUDIT_PROMPT.md is missing or suspiciously "
                f"small ({prompt_path}). Check the log and re-run to retry."
            )

        # Fail loudly now, not partway through Stage 3, if the phases it
        # wrote don't even parse.
        parse_audit_prompt_phases(prompt_path.read_text(encoding="utf-8"))

        if commit_paths(project_path, ["audit-gen/AUDIT_PROMPT.md"], "audit-gen: Stage 2 audit prompt"):
            print("Committed Stage 2 output.")
            if auto_push:
                push_branch_to_origin(project_path, branch_name)

    if review_required and not already_existed:
        print(f"\n--review-required: review {prompt_path} now (hand-edit it if you want).")
        answer = input("Continue to Stage 3 and execute this audit prompt? [y/N]: ")
        if answer.strip().lower() != "y":
            raise GeneratorError(
                "Stopped at your request after Stage 2. Edit audit-gen/AUDIT_PROMPT.md if "
                "needed, then re-run this same command to continue from Stage 3."
            )


def run_stage3(project_path, logs_folder, max_turns, auto_push, branch_name, review_required, pause_ctx):
    print_banner("STAGE 3: PHASED AUDIT EXECUTION")
    prompt_path = project_path / "audit-gen" / "AUDIT_PROMPT.md"
    phases = parse_audit_prompt_phases(prompt_path.read_text(encoding="utf-8"))
    (project_path / "audit-gen" / "staging").mkdir(exist_ok=True)

    for phase in phases:
        staging_path = staging_path_for_phase(project_path, phase)
        phase_label = f"Phase {phase['phase_number']} ({phase['title']})"

        if staging_path.exists():
            print(f"{phase_label}: already staged -- skipping.")
            continue

        # Checked once per phase, not mid-phase -- see check_hard_pause()'s
        # docstring for why a phase boundary is the granularity this can
        # actually guarantee.
        check_hard_pause(pause_ctx["engine"], project_path, phase_label, pause_ctx["run_args"], pause_ctx["auto_resume"], pause_ctx["hard_pause_threshold"], action=pause_ctx["action"], db_path=pause_ctx["db_path"])

        print_banner(f"STAGE 3 -- {phase_label}")
        staging_relative_path = staging_path.relative_to(project_path).as_posix()
        full_prompt = phase["prompt_text"] + build_phase_harness_suffix(phase, staging_relative_path)

        log_path = build_stage_log_path(logs_folder, f"phase{phase['phase_number']:02d}")
        run_stage_session(phase["engine"], full_prompt, project_path, log_path, max_turns, phase_label)

        if not staging_path.exists():
            raise GeneratorError(
                f"{phase_label}'s session finished but never wrote {staging_path}. Check the "
                "log and re-run to retry just this phase."
            )
        try:
            audit_compiler.validate_phase_shape(
                __import__("json").loads(staging_path.read_text(encoding="utf-8")), staging_path.name
            )
        except audit_compiler.StagingValidationError as exc:
            raise GeneratorError(
                f"{phase_label} wrote {staging_path}, but it doesn't match the required schema: "
                f"{exc}. Check the log; you may need to delete this file and re-run to retry "
                "the phase, or fix it by hand."
            )
        except ValueError as exc:
            raise GeneratorError(f"{phase_label} wrote {staging_path}, but it isn't valid JSON: {exc}.")

        if commit_paths(project_path, [staging_relative_path], f"audit-gen: {phase_label} findings"):
            print(f"Committed {phase_label} findings.")
            if auto_push:
                push_branch_to_origin(project_path, branch_name)

    if review_required:
        print(f"\n--review-required: review staged findings under {project_path / 'audit-gen' / 'staging'} now.")
        answer = input("Continue and compile them into AUDIT.md? [y/N]: ")
        if answer.strip().lower() != "y":
            raise GeneratorError("Stopped at your request before compiling. Re-run this same command when ready.")

    print_banner("COMPILING AUDIT.md")
    audit_path = audit_compiler.compile_audit_md(project_path)
    print(f"Wrote {audit_path}")
    if commit_paths(project_path, ["AUDIT.md"], "audit-gen: compile AUDIT.md from staged findings"):
        print("Committed AUDIT.md.")
        if auto_push:
            push_branch_to_origin(project_path, branch_name)


# ======================================================================
# DRY RUN
# ======================================================================

def print_dry_run_plan(project_path):
    print("=" * 60)
    print("DRY RUN -- no sessions will be started")
    print("=" * 60)

    def status_of(path):
        return "done" if path.exists() else "pending"

    audit_gen = project_path / "audit-gen"
    print(f"Stage 0 (Project Scan):        {status_of(audit_gen / 'PROJECT_PROFILE.md')}")
    print(f"Stage 1 (Research):             {status_of(audit_gen / 'RESEARCH_NOTES.md')}")
    prompt_path = audit_gen / "AUDIT_PROMPT.md"
    print(f"Stage 2 (Audit Prompt Build):   {status_of(prompt_path)}")

    if prompt_path.exists():
        try:
            phases = parse_audit_prompt_phases(prompt_path.read_text(encoding="utf-8"))
        except GeneratorError as exc:
            print(f"Stage 3 (Phased Execution):     CANNOT PLAN -- {exc}")
            return
        print(f"Stage 3 (Phased Execution):     {len(phases)} phase(s) planned")
        for phase in phases:
            staging_path = staging_path_for_phase(project_path, phase)
            print(f"  Phase {phase['phase_number']} ({phase['title']}, engine={phase['engine']}): {status_of(staging_path)}")
    else:
        print("Stage 3 (Phased Execution):     pending (depends on Stage 2)")

    print(f"AUDIT.md:                       {status_of(project_path / 'AUDIT.md')}")


# ======================================================================
# MAIN
# ======================================================================

def run_generator(project_path, branch, max_turns, review_required, auto_push, usage_warn_threshold, hard_pause_threshold, auto_resume):
    """
    The full Stage 0-3 run, as a plain function -- what main() drives from
    the CLI, and what pipeline.py's --check-resume calls directly (in the
    same process, with the run_args state_store already has on file for a
    paused project) to resume, without shelling back out to a new
    `python3 audit_generator.py ...` subprocess or duplicating this flow.

    Returns the Path to AUDIT.md on success. Raises PausedForQuotaError,
    GeneratorError, or engine_registry.UnknownEngineError -- callers
    decide how to present those (main() turns them into exit codes and
    console messages; pipeline.py's resume loop just needs to know a
    given project didn't finish so it can move on to the next one).
    """
    project_path = Path(project_path)
    audit_path = project_path / "AUDIT.md"
    if audit_path.exists():
        raise GeneratorError(
            f"{audit_path} already exists -- this project already has an audit. Run "
            "audit_fix_runner.py / audit_verify_runner.py to work through it, or move AUDIT.md "
            "aside yourself first if you genuinely want to regenerate from scratch."
        )

    confirm_git_repo(project_path)
    checkout_or_create_branch(project_path, branch)

    engine = er.get_engine("claude")
    check_and_report_usage(engine, "BEFORE THIS RUN", warn_threshold=usage_warn_threshold)

    logs_folder = ensure_logs_folder(project_path)

    run_args = {
        "branch": branch,
        "max_turns": max_turns,
        "review_required": review_required,
        "auto_push": auto_push,
        "usage_warn_threshold": usage_warn_threshold,
        "hard_pause_threshold": hard_pause_threshold,
        "auto_resume": auto_resume,
    }
    pause_ctx = {
        "engine": engine,
        "run_args": run_args,
        "auto_resume": auto_resume,
        "hard_pause_threshold": hard_pause_threshold,
        "action": "generate",
        "db_path": None,  # None -> state_store's own default DB path
    }
    ss.mark_running(project_path, "stage0", action="generate", run_args=run_args, auto_resume=auto_resume)

    try:
        run_stage0(project_path, logs_folder, max_turns, auto_push, branch, pause_ctx)
        run_stage1(project_path, logs_folder, max_turns, auto_push, branch, pause_ctx)
        run_stage2(project_path, logs_folder, max_turns, auto_push, branch, review_required, pause_ctx)
        run_stage3(project_path, logs_folder, max_turns, auto_push, branch, review_required, pause_ctx)
    except (GeneratorError, er.UnknownEngineError) as exc:
        ss.mark_failed(project_path, "unknown", str(exc), action="generate", run_args=run_args)
        raise

    ss.mark_completed(project_path, stage="stage3", action="generate", run_args=run_args)

    print_banner("AUDIT GENERATOR -- DONE", color=TermColors.GREEN)
    print(f"AUDIT.md is ready at: {audit_path}")
    print(f"Everything was committed on branch: {branch}")
    print(
        "\nNext step -- run the fixer on this SAME branch, e.g.:\n"
        f"  python3 audit_fix_runner.py --project {project_path} --branch {branch} --test-cmd \"...\""
    )
    check_and_report_usage(engine, "AFTER THIS RUN")
    return audit_path


def main():
    args = parse_arguments()
    print_banner("AUDIT GENERATOR -- PRE-FLIGHT", color=TermColors.BLUE)

    project_path = Path(args.project).expanduser().resolve()
    if not project_path.is_dir():
        print(f"ERROR: --project path does not exist or is not a folder: {project_path}")
        sys.exit(1)

    branch_name = args.branch or f"audit-gen-{datetime.now().strftime('%Y-%m-%d')}"

    if args.dry_run:
        print_dry_run_plan(project_path)
        sys.exit(0)

    try:
        run_generator(
            project_path,
            branch_name,
            args.max_turns,
            args.review_required,
            args.auto_push,
            args.usage_warn_threshold,
            args.hard_pause_threshold,
            args.auto_resume,
        )
    except PausedForQuotaError:
        # Already fully reported and recorded by check_hard_pause() -- this
        # is an expected, graceful stop, not a failure, so no ERROR banner
        # and no non-zero exit code.
        sys.exit(0)
    except GeneratorError as exc:
        print(f"\n{TermColors.RED}ERROR: {exc}{TermColors.RESET}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nInterrupted (Ctrl+C). Nothing is lost -- re-run this same command to resume.")
        sys.exit(1)
    except er.UnknownEngineError as exc:
        print(f"\n{TermColors.RED}ERROR: {exc}{TermColors.RESET}")
        print("Check the '**Engine:**' line for the phase(s) in AUDIT_PROMPT.md that mention it.")
        sys.exit(1)


if __name__ == "__main__":
    main()

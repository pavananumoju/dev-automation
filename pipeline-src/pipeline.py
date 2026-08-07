#!/usr/bin/env python3
"""
Pipeline
========

Three independent, separately-triggered actions, plus the entry point a
scheduler should call to auto-resume paused ones:

    python3 pipeline.py generate --project ~/code/my-app --review-required
    python3 pipeline.py fix      --project ~/code/my-app --test-cmd "npm test"
    python3 pipeline.py verify   --project ~/code/my-app --test-cmd "npm test"
    python3 pipeline.py check-resume

`generate` runs audit_generator.py's Stage 0-3 and stops once AUDIT.md
exists -- it never continues into fix or verify on its own. `fix` and
`verify` each require an AUDIT.md to already be there (from a prior
`generate`, whenever that happened -- could be minutes or weeks earlier)
and are run whenever you decide to, completely independently of each
other and of `generate`. This mirrors how audit_fix_runner.py and
audit_verify_runner.py already worked before any of this pipeline tooling
existed -- generation, fixing, and verifying were never meant to be one
inseparable chain, just three tools that hand off through AUDIT.md
whenever you're ready for the next one.

`check-resume` is meant to be pinged periodically by your OS's scheduler
(see README.md for a launchd example), not run interactively. It queries
state_store.due_paused_projects() and resumes each due row using its own
recorded action ("generate", "fix", or "verify") -- so a pause during
generation resumes generation, and a pause before a fixer run resumes
that fixer run, never the other.

Usage-limit pause/resume checkpoints exist at every boundary this script
or audit_generator.py controls: between each Stage 0-3 phase, and before
starting the fixer/verifier subprocess as a whole. They can NOT pause
mid-way through a single audit_fix_runner.py/audit_verify_runner.py run
(e.g. between row 12 and row 13) without modifying those existing tools,
which this deliberately does not do -- see those tools' own README for
how a mid-run stop is handled and resumed by hand.
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "audit-generator-src"))

import state_store as ss
import audit_generator as ag
import engine_registry as er

FIX_RUNNER_PATH = Path(__file__).resolve().parent.parent / "audit-fix-runner-src" / "audit_fix_runner.py"
VERIFY_RUNNER_PATH = Path(__file__).resolve().parent.parent / "audit-fix-runner-src" / "audit_verify_runner.py"


class PipelineError(Exception):
    """Raised for any pipeline-level condition (as opposed to generator- or fixer/verifier-level) that should stop the run."""


# ======================================================================
# CLI ARGS
# ======================================================================

def add_common_fixver_args(subparser):
    subparser.add_argument("--project", required=True, help="Path to the project root.")
    subparser.add_argument(
        "--branch",
        default=None,
        help="Branch AUDIT.md lives on. Default: inferred from the last tracked run for this project (any action) in state_store.",
    )
    subparser.add_argument("--test-cmd", dest="test_cmd", required=True, help="The project's test suite command, e.g. \"npm test\".")
    subparser.add_argument("--mode", dest="mode", choices=["manual", "auto"], default="auto", help="Default: auto (this tool is meant for unattended runs).")
    subparser.add_argument("--max-rows", dest="max_rows", type=int, default=None)
    subparser.add_argument("--severities", dest="severities", default=None, help='Comma-separated, e.g. "P0,P1".')
    subparser.add_argument("--emulator-avd", dest="emulator_avd", default=None)
    subparser.add_argument("--auto-push", dest="auto_push", action="store_true")
    subparser.add_argument("--hard-pause-threshold", dest="hard_pause_threshold", type=int, default=97)
    subparser.add_argument("--auto-resume", dest="auto_resume", action="store_true")


def parse_arguments():
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Run AUDIT.md generation, fixing, or verification -- independently -- with usage-limit pause/resume.",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    generate_parser = subparsers.add_parser("generate", help="Run Stages 0-3 to produce AUDIT.md. Stops there.")
    generate_parser.add_argument("--project", required=True)
    generate_parser.add_argument("--branch", default=None, help="Default: audit-gen-<today's date>.")
    generate_parser.add_argument("--max-turns", dest="max_turns", type=int, default=ag.DEFAULT_MAX_TURNS)
    generate_parser.add_argument("--review-required", dest="review_required", action="store_true")
    generate_parser.add_argument("--usage-warn-threshold", dest="usage_warn_threshold", type=int, default=90)
    generate_parser.add_argument("--hard-pause-threshold", dest="hard_pause_threshold", type=int, default=97)
    generate_parser.add_argument("--auto-resume", dest="auto_resume", action="store_true")
    generate_parser.add_argument("--auto-push", dest="auto_push", action="store_true")
    generate_parser.add_argument("--dry-run", dest="dry_run", action="store_true")

    fix_parser = subparsers.add_parser("fix", help="Run audit_fix_runner.py against an existing AUDIT.md.")
    add_common_fixver_args(fix_parser)

    verify_parser = subparsers.add_parser("verify", help="Run audit_verify_runner.py against an existing AUDIT.md.")
    add_common_fixver_args(verify_parser)

    subparsers.add_parser("check-resume", help="Resume every due, auto-resume-enabled paused generate/fix/verify run.")

    return parser.parse_args()


def build_generate_run_args(args, branch_name):
    return {
        "branch": branch_name,
        "max_turns": args.max_turns,
        "review_required": args.review_required,
        "auto_push": args.auto_push,
        "usage_warn_threshold": args.usage_warn_threshold,
        "hard_pause_threshold": args.hard_pause_threshold,
        "auto_resume": args.auto_resume,
    }


def build_fixver_run_args(args, branch_name):
    return {
        "branch": branch_name,
        "test_cmd": args.test_cmd,
        "mode": args.mode,
        "max_rows": args.max_rows,
        "severities": args.severities,
        "emulator_avd": args.emulator_avd,
        "auto_push": args.auto_push,
        "hard_pause_threshold": args.hard_pause_threshold,
        "auto_resume": args.auto_resume,
    }


def resolve_branch(project_path, explicit_branch):
    """
    --branch for `fix`/`verify`, or a clear error if none was given and
    none can be inferred. Falls back to whatever branch state_store's
    last tracked run for this project (generate, fix, or verify -- any of
    them) used, since AUDIT.md's branch doesn't change once generated.
    """
    if explicit_branch:
        return explicit_branch
    state = ss.get_state(project_path)
    if state:
        inferred = state.get("run_args", {}).get("branch")
        if inferred:
            return inferred
    raise PipelineError(
        f"No --branch given, and no prior tracked run found for {project_path} to infer one "
        "from. Specify --branch explicitly (the branch AUDIT.md was generated on)."
    )


# ======================================================================
# GENERATE
# ======================================================================

def run_generate_action(project_path, run_args):
    """
    Thin wrapper -- audit_generator.run_generator() already does its own
    complete state_store bookkeeping (mark_running/mark_paused_quota/
    mark_completed/mark_failed, all tagged action="generate"), so there's
    nothing extra to record here.
    """
    ag.run_generator(
        Path(project_path),
        run_args["branch"],
        run_args["max_turns"],
        run_args["review_required"],
        run_args["auto_push"],
        run_args["usage_warn_threshold"],
        run_args["hard_pause_threshold"],
        run_args["auto_resume"],
    )


# ======================================================================
# FIX / VERIFY
# ======================================================================

def build_runner_command(runner_path, project_path, branch, run_args):
    command = [
        sys.executable,
        str(runner_path),
        "--project",
        str(project_path),
        "--branch",
        branch,
        "--mode",
        run_args["mode"],
        "--test-cmd",
        run_args["test_cmd"],
    ]
    if run_args.get("max_rows") is not None:
        command += ["--max-rows", str(run_args["max_rows"])]
    if run_args.get("severities"):
        command += ["--severities", run_args["severities"]]
    if run_args.get("emulator_avd"):
        command += ["--emulator-avd", run_args["emulator_avd"]]
    if run_args.get("auto_push"):
        command += ["--auto-push"]
    return command


def run_runner_subprocess(runner_path, label, project_path, branch, run_args):
    """
    Run audit_fix_runner.py or audit_verify_runner.py as a subprocess,
    inheriting this process's stdout/stderr directly so its own live
    console output (banners, colors, per-row progress) shows through
    exactly as it would running it by hand. Raises PipelineError on a
    non-zero exit code (a real infra-level failure -- those tools already
    treat "some rows ended up Blocked" as a normal, zero-exit outcome).
    """
    command = build_runner_command(runner_path, project_path, branch, run_args)
    print(f"\n--- Starting {label}: {' '.join(command)} ---\n")
    result = subprocess.run(command)
    if result.returncode != 0:
        raise PipelineError(f"{label} exited with code {result.returncode}. Check its own output above.")


def run_fixver_action(action, runner_path, label, project_path, run_args):
    """
    Shared driver for both `fix` and `verify`: resolve + check out the
    target branch, confirm AUDIT.md is actually there (audit_fix_runner.py
    itself checks for AUDIT.md's existence BEFORE switching branches in
    its own preflight -- so whatever branch this process has checked out
    at the moment it's launched must already be the right one, which is
    exactly what checkout_or_create_branch() below guarantees), then the
    usual pause-check / mark_running / subprocess / mark_completed cycle.
    """
    project_path = Path(project_path)
    branch = resolve_branch(project_path, run_args["branch"])
    run_args = {**run_args, "branch": branch}

    ag.confirm_git_repo(project_path)
    ag.checkout_or_create_branch(project_path, branch)

    audit_path = project_path / "AUDIT.md"
    if not audit_path.is_file():
        raise PipelineError(
            f"No AUDIT.md found at {audit_path} on branch '{branch}'. Run "
            f"`pipeline.py generate --project {project_path}` first, or check --branch."
        )

    engine = er.get_engine("claude")
    ag.check_hard_pause(
        engine, project_path, action, run_args, run_args["auto_resume"], run_args["hard_pause_threshold"], action=action
    )

    ss.mark_running(project_path, action, action=action, run_args=run_args, auto_resume=run_args["auto_resume"])
    try:
        run_runner_subprocess(runner_path, label, project_path, branch, run_args)
    except PipelineError as exc:
        ss.mark_failed(project_path, action, str(exc), action=action, run_args=run_args)
        raise

    ss.mark_completed(project_path, stage=action, action=action, run_args=run_args)


def run_fix_action(project_path, run_args):
    run_fixver_action("fix", FIX_RUNNER_PATH, "audit_fix_runner.py", project_path, run_args)


def run_verify_action(project_path, run_args):
    run_fixver_action("verify", VERIFY_RUNNER_PATH, "audit_verify_runner.py", project_path, run_args)


# ======================================================================
# --check-resume
# ======================================================================

ACTION_RUNNERS = {
    "generate": run_generate_action,
    "fix": run_fix_action,
    "verify": run_verify_action,
}


def check_resume():
    """
    Resume every project state_store shows as due: PAUSED_QUOTA,
    auto_resume on, resume_at already passed. Dispatches on each row's
    own recorded action, so a paused generate resumes as a generate, a
    paused fix resumes as a fix -- never conflated. Prints one line and
    returns immediately if nothing is due.
    """
    due = ss.due_paused_projects()
    if not due:
        print("check-resume: nothing due.")
        return

    for state in due:
        project_path = state["project_path"]
        action = state.get("action")
        runner = ACTION_RUNNERS.get(action)
        print(f"\n=== Resuming {project_path} (action={action}, was paused: {state['paused_reason']}) ===")
        if runner is None:
            print(f"=== {project_path}: no runner for action={action!r} -- skipping (stale/unrecognized row). ===")
            continue
        try:
            runner(project_path, state["run_args"])
            print(f"=== {project_path}: resumed {action} finished ===")
        except ag.PausedForQuotaError:
            print(f"=== {project_path}: paused again immediately -- usage still high. Will retry next check. ===")
        except ag.ReviewRequiredError:
            # Resuming a quota pause can walk straight into a NEW
            # review-required gate (e.g. Stage 1 was what was paused;
            # Stage 2's gate is reached for the first time on this very
            # resume). Also an expected, graceful stop -- state_store
            # already has it recorded as PAUSED_REVIEW, not a failure.
            print(f"=== {project_path}: hit a --review-required gate -- needs your review before continuing further. ===")
        except (ag.GeneratorError, PipelineError, er.UnknownEngineError) as exc:
            print(f"=== {project_path}: resume failed: {exc} ===")
            ss.mark_failed(project_path, action or "unknown", str(exc), action=action, run_args=state["run_args"])


# ======================================================================
# MAIN
# ======================================================================

def main():
    args = parse_arguments()

    if args.action == "check-resume":
        check_resume()
        return

    project_path = Path(args.project).expanduser().resolve()
    if not project_path.is_dir():
        print(f"ERROR: --project path does not exist or is not a folder: {project_path}")
        sys.exit(1)

    try:
        if args.action == "generate":
            branch_name = args.branch or f"audit-gen-{ag.datetime.now().strftime('%Y-%m-%d')}"
            if args.dry_run:
                ag.print_dry_run_plan(project_path)
                return
            run_generate_action(project_path, build_generate_run_args(args, branch_name))

        elif args.action == "fix":
            run_fix_action(project_path, build_fixver_run_args(args, args.branch))

        elif args.action == "verify":
            run_verify_action(project_path, build_fixver_run_args(args, args.branch))

    except (ag.PausedForQuotaError, ag.ReviewRequiredError):
        sys.exit(0)
    except (ag.GeneratorError, PipelineError, er.UnknownEngineError) as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nInterrupted (Ctrl+C). Nothing is lost -- re-run this same command to resume.")
        sys.exit(1)


if __name__ == "__main__":
    main()

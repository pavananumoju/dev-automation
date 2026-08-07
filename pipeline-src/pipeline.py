#!/usr/bin/env python3
"""
Pipeline
========

The master conductor: chains audit_generator.py's Stages 0-3 into the
existing, unchanged audit_fix_runner.py (fix) and audit_verify_runner.py
(verify), and is the one entry point a scheduler (launchd/cron) should
actually call to auto-resume paused runs.

Two ways to run this:

1. Start (or resume, by re-running the same command) one project's full
   pipeline:

       python3 pipeline.py --project ~/code/my-app --test-cmd "npm test"

   Runs Stage 0-3 (audit_generator.py, in-process), then, since --test-cmd
   was given, the fixer and verifier as subprocesses on the same branch.
   Omit --test-cmd to stop after AUDIT.md is generated, same as running
   audit_generator.py directly.

2. Check whether anything paused-for-usage is due to resume, across every
   project tracked in state_store -- meant to be pinged periodically by
   your OS's scheduler, not run interactively:

       python3 pipeline.py --check-resume

   Cheap when nothing is due: one SQLite query, then exit. Only resumes
   projects that were run with --auto-resume in the first place.

Scope boundary: automatic usage-limit pause/resume is wired at every
subprocess/session boundary THIS script or audit_generator.py controls --
between Stage 0-3's phases, and before starting the fixer/verifier
subprocesses as a whole. It can NOT pause mid-way through a single
audit_fix_runner.py/audit_verify_runner.py run (e.g. between row 12 and
row 13) without modifying those existing tools, which this deliberately
does not do. If usage runs out mid-fix, that run stops the same way it
always has (see audit_fix_runner.py's own README) -- resume it by hand,
the same as you would running it directly.
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

def parse_arguments():
    parser = argparse.ArgumentParser(
        prog="pipeline.py",
        description="Chain audit_generator.py's Stages 0-3 into audit_fix_runner.py / audit_verify_runner.py, with usage-limit pause/resume.",
    )
    parser.add_argument("--project", help="Path to the project root. Required unless --check-resume is given.")
    parser.add_argument("--branch", default=None, help="Git branch to work on. Default: audit-gen-<today's date>.")
    parser.add_argument("--max-turns", dest="max_turns", type=int, default=ag.DEFAULT_MAX_TURNS, help="Passed to audit_generator.py.")
    parser.add_argument("--review-required", dest="review_required", action="store_true", help="Passed to audit_generator.py.")
    parser.add_argument("--usage-warn-threshold", dest="usage_warn_threshold", type=int, default=90, help="Passed to audit_generator.py.")
    parser.add_argument("--hard-pause-threshold", dest="hard_pause_threshold", type=int, default=97, help="Applies to both the generator stage and the fix/verify boundary.")
    parser.add_argument("--auto-resume", dest="auto_resume", action="store_true", help="If a hard-pause happens anywhere in this pipeline, mark it for --check-resume to pick up automatically.")
    parser.add_argument("--auto-push", dest="auto_push", action="store_true", help="Passed to audit_generator.py and (if reached) the fixer/verifier.")

    parser.add_argument("--test-cmd", dest="test_cmd", default=None, help="If given, continue past AUDIT.md generation into the fixer, then the verifier, on the same branch. If omitted, the pipeline stops after Stage 3, same as running audit_generator.py alone.")
    parser.add_argument("--mode", dest="mode", choices=["manual", "auto"], default="auto", help="Passed to the fixer/verifier. Default: auto (this tool is meant for unattended runs).")
    parser.add_argument("--max-rows", dest="max_rows", type=int, default=None, help="Passed to the fixer/verifier.")
    parser.add_argument("--severities", dest="severities", default=None, help="Passed to the fixer/verifier.")
    parser.add_argument("--emulator-avd", dest="emulator_avd", default=None, help="Passed to the fixer/verifier.")
    parser.add_argument("--skip-verify", dest="skip_verify", action="store_true", help="Run the fixer but not the verifier afterward.")

    parser.add_argument("--check-resume", dest="check_resume", action="store_true", help="Ignore --project and every other flag; instead, resume every project state_store shows as due (see module docstring).")

    return parser.parse_args()


def build_run_args(args, branch_name):
    """
    Everything needed to reconstruct this exact pipeline invocation later,
    from state_store -- both the generator's own args and the
    fixer/verifier args pipeline.py itself adds on top.
    """
    return {
        "branch": branch_name,
        "max_turns": args.max_turns,
        "review_required": args.review_required,
        "usage_warn_threshold": args.usage_warn_threshold,
        "hard_pause_threshold": args.hard_pause_threshold,
        "auto_resume": args.auto_resume,
        "auto_push": args.auto_push,
        "test_cmd": args.test_cmd,
        "mode": args.mode,
        "max_rows": args.max_rows,
        "severities": args.severities,
        "emulator_avd": args.emulator_avd,
        "skip_verify": args.skip_verify,
    }


# ======================================================================
# FIX / VERIFY SUBPROCESS LEGS
# ======================================================================

def build_runner_command(runner_path, project_path, branch, run_args):
    command = [
        sys.executable,
        str(runner_path),
        "--project",
        str(project_path),
        "--branch",
        run_args["branch"],
        "--mode",
        run_args["mode"],
    ]
    if run_args.get("test_cmd"):
        command += ["--test-cmd", run_args["test_cmd"]]
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
    exactly as it would running it by hand -- no point re-implementing
    that here. Raises PipelineError on a non-zero exit code (a real
    infra-level failure -- e.g. the CLI going missing -- not "some rows
    ended up Blocked", which those tools already treat as a normal,
    zero-exit outcome).
    """
    command = build_runner_command(runner_path, project_path, branch, run_args)
    print(f"\n--- Starting {label}: {' '.join(command)} ---\n")
    result = subprocess.run(command)
    if result.returncode != 0:
        raise PipelineError(f"{label} exited with code {result.returncode}. Check its own output above.")


# ======================================================================
# DRIVING ONE PROJECT'S FULL PIPELINE
# ======================================================================

def run_full_pipeline(project_path, run_args):
    """
    Run the whole pipeline for one project, from wherever run_args says to
    start: Stage 0-3 (audit_generator.run_generator(), in-process), then,
    if run_args["test_cmd"] is set, the fixer and (unless skip_verify) the
    verifier as subprocesses on the same branch.

    Used both for a fresh `--project` invocation and, with a stored
    run_args, for --check-resume. Always re-runs audit_generator's
    run_generator() even on resume -- that's safe and cheap, since every
    one of its stages/phases already checks whether its own output exists
    before doing anything (see audit_generator.py's module docstring).
    """
    project_path = Path(project_path)

    ag.run_generator(
        project_path,
        run_args["branch"],
        run_args["max_turns"],
        run_args["review_required"],
        run_args["auto_push"],
        run_args["usage_warn_threshold"],
        run_args["hard_pause_threshold"],
        run_args["auto_resume"],
    )

    if not run_args.get("test_cmd"):
        ss.mark_completed(project_path, stage="generator_only")
        return

    engine = er.get_engine("claude")
    ag.check_hard_pause(
        engine, project_path, "fix", run_args, run_args["auto_resume"], run_args["hard_pause_threshold"]
    )
    ss.mark_running(project_path, "fix", run_args=run_args, auto_resume=run_args["auto_resume"])
    run_runner_subprocess(FIX_RUNNER_PATH, "audit_fix_runner.py", project_path, run_args["branch"], run_args)

    if run_args.get("skip_verify"):
        ss.mark_completed(project_path, stage="fix_only")
        return

    ag.check_hard_pause(
        engine, project_path, "verify", run_args, run_args["auto_resume"], run_args["hard_pause_threshold"]
    )
    ss.mark_running(project_path, "verify", run_args=run_args, auto_resume=run_args["auto_resume"])
    run_runner_subprocess(VERIFY_RUNNER_PATH, "audit_verify_runner.py", project_path, run_args["branch"], run_args)

    ss.mark_completed(project_path, stage="verify")


# ======================================================================
# --check-resume
# ======================================================================

def check_resume():
    """
    Query state_store for every project that's PAUSED_QUOTA, opted into
    auto_resume, and whose resume time has passed, and resume each one's
    pipeline in-process using its stored run_args. Meant to be pinged
    periodically (e.g. every 15-30 minutes) by launchd/cron -- prints one
    line and exits immediately if nothing is due.
    """
    due = ss.due_paused_projects()
    if not due:
        print("check-resume: nothing due.")
        return

    for state in due:
        project_path = state["project_path"]
        print(f"\n=== Resuming {project_path} (was paused: {state['paused_reason']}) ===")
        try:
            run_full_pipeline(project_path, state["run_args"])
            print(f"=== {project_path}: resumed run finished ===")
        except ag.PausedForQuotaError:
            print(f"=== {project_path}: paused again immediately -- usage still high. Will retry next check. ===")
        except (ag.GeneratorError, PipelineError, er.UnknownEngineError) as exc:
            print(f"=== {project_path}: resume failed: {exc} ===")
            ss.mark_failed(project_path, state["current_stage"], str(exc), run_args=state["run_args"])


# ======================================================================
# MAIN
# ======================================================================

def main():
    args = parse_arguments()

    if args.check_resume:
        check_resume()
        return

    if not args.project:
        print("ERROR: --project is required (unless --check-resume is given).")
        sys.exit(1)

    project_path = Path(args.project).expanduser().resolve()
    if not project_path.is_dir():
        print(f"ERROR: --project path does not exist or is not a folder: {project_path}")
        sys.exit(1)

    branch_name = args.branch or f"audit-gen-{ag.datetime.now().strftime('%Y-%m-%d')}"
    run_args = build_run_args(args, branch_name)

    try:
        run_full_pipeline(project_path, run_args)
    except ag.PausedForQuotaError:
        sys.exit(0)
    except (ag.GeneratorError, PipelineError, er.UnknownEngineError) as exc:
        print(f"\nERROR: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nInterrupted (Ctrl+C). Nothing is lost -- re-run this same command to resume.")
        sys.exit(1)


if __name__ == "__main__":
    main()

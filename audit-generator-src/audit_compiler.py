#!/usr/bin/env python3
"""
Audit Compiler
==============

Plain, deterministic Python -- no AI involved. Reads every Stage 3 phase's
staged findings (see SCHEMA.md, sections 2-3) and compiles them into a
single, correctly-formatted AUDIT.md Master Tracking Table (SCHEMA.md,
section 5), which audit_fix_runner.py and audit_verify_runner.py then work
through unchanged.

This is the fix for the "AI mangles Markdown tables across sessions"
failure mode: no AI session ever appends directly to AUDIT.md's table.
Each phase session only ever writes its own small, isolated JSON file: if
that JSON is malformed, this compiler fails loudly and says exactly which
file and field -- it never guesses or silently drops a finding.

Runs exactly ONCE, at the Stage 3 -> Stage 4 handoff. Refuses to run if
AUDIT.md already exists at the project root -- both because merging into
an existing (hand-written or previously-generated) audit is a different,
harder problem this version doesn't attempt, and because that same check
is what stops this compiler from ever being run a second time after
audit_fix_runner.py has started editing rows in that file (which would
silently wipe those edits out by regenerating the table from scratch).

Can be run as a library (compile_audit_md()) or standalone for testing:
    python3 audit_compiler.py --project ~/code/my-app
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path


REQUIRED_FINDING_FIELDS = ["local_id", "category", "severity", "finding"]
MASTER_TABLE_COLUMNS = ["ID", "Phase", "Category", "Severity", "Finding", "Fix Status", "Commit", "Notes"]
STAGING_GLOB = "phase-*.json"


class CompilerError(Exception):
    """Base class for every error this module raises deliberately."""


class StagingNotFoundError(CompilerError):
    """Raised when the staging folder is missing or has no phase files in it."""


class StagingValidationError(CompilerError):
    """Raised when a phase staging file doesn't match the schema in SCHEMA.md."""


class AuditAlreadyExistsError(CompilerError):
    """Raised when AUDIT.md already exists at the project root -- see module docstring."""


# ======================================================================
# LOADING & VALIDATING STAGING FILES
# ======================================================================

def load_phase_staging_files(staging_dir):
    """
    Read every phase-*.json file in staging_dir, parse and validate each
    against the schema in SCHEMA.md section 3, and return them as a list
    of dicts sorted by phase_number.

    Raises StagingNotFoundError if the folder is missing or empty, or
    StagingValidationError (naming the exact file and problem) for any
    file that doesn't match the expected shape. Never silently skips a
    bad file.
    """
    if not staging_dir.is_dir():
        raise StagingNotFoundError(f"Staging folder does not exist: {staging_dir}")

    staging_files = sorted(staging_dir.glob(STAGING_GLOB))
    if not staging_files:
        raise StagingNotFoundError(
            f"No phase staging files (matching '{STAGING_GLOB}') found in: {staging_dir}"
        )

    phases = []
    for staging_file in staging_files:
        try:
            raw_text = staging_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise StagingValidationError(f"{staging_file.name}: could not read file ({exc})")

        try:
            phase_data = json.loads(raw_text)
        except ValueError as exc:
            raise StagingValidationError(f"{staging_file.name}: not valid JSON ({exc})")

        validate_phase_shape(phase_data, staging_file.name)
        phases.append(phase_data)

    phases.sort(key=lambda phase: phase["phase_number"])
    return phases


def validate_phase_shape(phase_data, source_file_name):
    """
    Validate one parsed phase staging file against SCHEMA.md section 3.
    Raises StagingValidationError naming source_file_name and exactly
    what's wrong -- never coerces or guesses a fix.
    """
    if not isinstance(phase_data, dict):
        raise StagingValidationError(f"{source_file_name}: top level must be a JSON object.")

    for required_field in ("phase_number", "phase_title", "findings"):
        if required_field not in phase_data:
            raise StagingValidationError(f"{source_file_name}: missing required field '{required_field}'.")

    if not isinstance(phase_data["phase_number"], int):
        raise StagingValidationError(f"{source_file_name}: 'phase_number' must be an integer.")

    if not isinstance(phase_data["findings"], list):
        raise StagingValidationError(f"{source_file_name}: 'findings' must be a list.")

    for index, finding in enumerate(phase_data["findings"]):
        validate_finding_shape(finding, source_file_name, index)


def validate_finding_shape(finding, source_file_name, index):
    """
    Validate one finding object (SCHEMA.md section 2) from within a phase
    staging file. Raises StagingValidationError naming the source file and
    the finding's position within it.
    """
    location = f"{source_file_name}, finding #{index + 1}"

    if not isinstance(finding, dict):
        raise StagingValidationError(f"{location}: must be a JSON object.")

    for required_field in REQUIRED_FINDING_FIELDS:
        value = finding.get(required_field)
        if not isinstance(value, str) or not value.strip():
            raise StagingValidationError(
                f"{location}: missing or empty required field '{required_field}'."
            )

    if "notes" in finding and finding["notes"] is not None and not isinstance(finding["notes"], str):
        raise StagingValidationError(f"{location}: 'notes' must be a string if present.")


# ======================================================================
# ID ASSIGNMENT
# ======================================================================

def assign_final_ids(phases):
    """
    Walk every phase (already sorted by phase_number) and every finding
    within it, in the order given, and assign a final row ID of the form
    "<SEVERITY>-<N>" -- e.g. "P0-1", "P0-2", "P1-1" -- matching the ID
    shape audit_fix_runner.py's logs and existing AUDIT.md files already
    use. Counters are per-severity and span across phases, so a P1 found
    in phase 5 continues numbering from a P1 found in phase 2, never
    restarting or colliding.

    Returns a flat list of row dicts, ready for render_master_tracking_table().
    Deterministic: the same phases input always produces the same output.
    """
    counters = {}
    rows = []

    for phase in phases:
        for finding in phase["findings"]:
            severity = finding["severity"].strip().upper()
            counters[severity] = counters.get(severity, 0) + 1
            row_id = f"{severity}-{counters[severity]}"

            rows.append(
                {
                    "id": row_id,
                    "phase": str(phase["phase_number"]),
                    "category": finding["category"].strip(),
                    "severity": severity,
                    "finding": finding["finding"].strip(),
                    "fix_status": "Not started",
                    "commit": "",
                    "notes": (finding.get("notes") or "").strip(),
                }
            )

    return rows


# ======================================================================
# RENDERING
# ======================================================================

def _escape_cell(text):
    """
    Make a value safe to place inside one Markdown table cell.

    A backslash-escaped pipe ("\\|") is valid Markdown and would render
    correctly on GitHub, but audit_fix_runner.py's own table parser
    (split_table_row(), in the existing, unchanged runner scripts) does a
    naive str.split("|") with no awareness of escaping -- an escaped pipe
    would still split the row and shift every later column, silently
    corrupting that row's Fix Status and dropping it out of the fixer's
    queue. Confirmed by testing this compiler's output against the real
    parser. So: no literal "|" is ever allowed in a cell at all, escaped
    or not. A raw newline is replaced the same way, since it would break
    a single row onto multiple lines.
    """
    return str(text).replace("|", "/").replace("\n", " ").strip()


def render_master_tracking_table(rows):
    """
    Render rows (as built by assign_final_ids()) into the exact Markdown
    table shape defined in SCHEMA.md section 5. Returns the table as a
    single string, header through last data row (no surrounding heading
    or document structure -- see render_audit_md() for that).
    """
    lines = [
        "| " + " | ".join(MASTER_TABLE_COLUMNS) + " |",
        "|" + "|".join(["---"] * len(MASTER_TABLE_COLUMNS)) + "|",
    ]
    for row in rows:
        cells = [
            row["id"],
            row["phase"],
            row["category"],
            row["severity"],
            row["finding"],
            row["fix_status"],
            row["commit"],
            row["notes"],
        ]
        lines.append("| " + " | ".join(_escape_cell(cell) for cell in cells) + " |")
    return "\n".join(lines)


def render_audit_md(project_path, phases, rows):
    """Render the complete AUDIT.md document: header, provenance, and the Master Tracking Table."""
    phase_titles = ", ".join(f"{phase['phase_number']}: {phase['phase_title']}" for phase in phases)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# Audit",
        "",
        f"Generated {generated_at} by audit_compiler.py from {len(phases)} audit phase(s): {phase_titles}.",
        "",
        "Source documents: `audit-gen/PROJECT_PROFILE.md`, `audit-gen/CONTEXT_MANIFEST.md`, "
        "`audit-gen/RESEARCH_NOTES.md`, `audit-gen/AUDIT_PROMPT.md`.",
        "",
        f"Total findings: {len(rows)}.",
        "",
        "## Master Tracking Table",
        "",
        render_master_tracking_table(rows),
        "",
    ]
    return "\n".join(lines)


# ======================================================================
# TOP-LEVEL ENTRY POINT
# ======================================================================

def compile_audit_md(project_path):
    """
    Compile <project_path>/audit-gen/staging/phase-*.json into
    <project_path>/AUDIT.md. Written atomically (temp file + rename), so
    AUDIT.md is either fully written or not written at all -- never
    half-written.

    Raises AuditAlreadyExistsError if AUDIT.md already exists (see module
    docstring), StagingNotFoundError, or StagingValidationError. Returns
    the Path to the AUDIT.md just written.
    """
    project_path = Path(project_path)
    audit_path = project_path / "AUDIT.md"
    staging_dir = project_path / "audit-gen" / "staging"

    if audit_path.exists():
        raise AuditAlreadyExistsError(
            f"{audit_path} already exists. The compiler only ever runs once, to create a "
            "fresh AUDIT.md -- it refuses to run again so it can never overwrite rows "
            "audit_fix_runner.py/audit_verify_runner.py have already edited. If this project "
            "genuinely needs a brand-new audit, move or remove the existing AUDIT.md yourself "
            "first."
        )

    phases = load_phase_staging_files(staging_dir)
    rows = assign_final_ids(phases)
    audit_md_text = render_audit_md(project_path, phases, rows)

    temp_path = audit_path.with_suffix(".md.tmp")
    temp_path.write_text(audit_md_text, encoding="utf-8")
    os.replace(temp_path, audit_path)

    return audit_path


# ======================================================================
# STANDALONE CLI (for testing this module on its own)
# ======================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Compile staged Stage 3 audit findings into AUDIT.md. See SCHEMA.md."
    )
    parser.add_argument("--project", required=True, help="Path to the project root.")
    args = parser.parse_args()

    project_path = Path(args.project).expanduser().resolve()

    try:
        audit_path = compile_audit_md(project_path)
    except CompilerError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print(f"Wrote {audit_path}")


if __name__ == "__main__":
    main()

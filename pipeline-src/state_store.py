#!/usr/bin/env python3
"""
State Store
===========

A small SQLite-backed table of "what is each tracked project's pipeline
doing right now" -- one row per project, across however many projects
you're running this tool suite against. Used for two things:

1. Pause/resume on Claude usage limits. When a run hits a hard usage
   threshold mid-stage, it records PAUSED_QUOTA here with the parsed
   reset time and the arguments needed to resume, then exits cleanly
   instead of trying to sleep in-process for hours (fragile -- a closed
   terminal or a sleeping laptop loses an in-process sleep; a periodic
   external ping, e.g. launchd/cron calling pipeline.py --check-resume,
   does not). See due_paused_projects().
2. A single place a future dashboard can query to show every tracked
   project's status without re-deriving it from log files.

This module has no dependency on audit_generator.py, engine_registry.py,
or the Claude CLI -- it's pure SQLite plus one piece of string parsing
(turning a `claude /usage` reset string like "Aug 8 at 2:40am
(Asia/Calcutta)" into an absolute, comparable timestamp), so it can be
tested and reasoned about entirely on its own.
"""

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "pipeline_state.db"

STATUS_IDLE = "IDLE"
STATUS_RUNNING = "RUNNING"
STATUS_PAUSED_QUOTA = "PAUSED_QUOTA"
STATUS_PAUSED_REVIEW = "PAUSED_REVIEW"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"


# ======================================================================
# RESET-TIME PARSING
# ======================================================================

# Matches the reset strings engine_registry.py's check_usage() already
# extracts from `claude /usage`, e.g. "Aug 8 at 2:40am (Asia/Calcutta)".
RESET_TEXT_PATTERN = re.compile(
    r"([A-Za-z]{3})[a-z]*\s+(\d{1,2})\s+at\s+(\d{1,2}):(\d{2})\s*([ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)


def parse_reset_timestamp(reset_text, now=None):
    """
    Parse a `claude /usage` reset string into a timezone-aware datetime.
    Returns None if reset_text doesn't match the expected shape or names
    an unrecognized timezone -- callers must treat that as "can't
    schedule an auto-resume for this", not guess.

    The reset string has no year. now (a timezone-aware datetime,
    defaulting to the real current time in the parsed zone) is used only
    to resolve the year-boundary edge case: if combining the given
    month/day/time with the current year would land in the past relative
    to now, the year is rolled forward by one. This is NOT a general
    "fix a stale timestamp" mechanism -- it only matters at the moment a
    fresh reset string is first parsed, since a just-reported reset time
    is always in the near future. Checking whether a previously-stored
    resume time is now due is a separate, plain "is it in the past yet"
    comparison (see due_paused_projects()), not this function's job.
    """
    if not reset_text:
        return None

    match = RESET_TEXT_PATTERN.search(reset_text)
    if not match:
        return None

    month_abbr, day_text, hour_text, minute_text, meridiem, zone_name = match.groups()

    try:
        tzinfo = ZoneInfo(zone_name.strip())
    except Exception:
        return None

    try:
        month_number = datetime.strptime(month_abbr[:3].title(), "%b").month
    except ValueError:
        return None

    hour = int(hour_text) % 12
    if meridiem.lower() == "pm":
        hour += 12

    reference_now = (now or datetime.now(tzinfo)).astimezone(tzinfo)
    candidate = datetime(reference_now.year, month_number, int(day_text), hour, int(minute_text), tzinfo=tzinfo)
    if candidate < reference_now:
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate


def to_utc_iso(dt):
    """Normalize any timezone-aware datetime to a UTC ISO 8601 string, so stored timestamps compare correctly as plain strings."""
    return dt.astimezone(timezone.utc).isoformat()


def utc_now_iso():
    return to_utc_iso(datetime.now(timezone.utc))


# ======================================================================
# STORAGE
# ======================================================================

def _connect(db_path=None):
    db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS runs (
            project_path   TEXT PRIMARY KEY,
            status         TEXT NOT NULL,
            action         TEXT,
            current_stage  TEXT,
            paused_reason  TEXT,
            resume_at      TEXT,
            auto_resume    INTEGER NOT NULL DEFAULT 0,
            run_args_json  TEXT,
            last_error     TEXT,
            last_updated   TEXT NOT NULL
        )
        """
    )
    try:
        # Lets an existing DB file created before the `action` column
        # existed pick it up in place, rather than needing to be deleted.
        # Harmless no-op (duplicate-column error, swallowed) on a DB that
        # already has it, including one freshly created by the statement
        # just above.
        connection.execute("ALTER TABLE runs ADD COLUMN action TEXT")
        connection.commit()
    except sqlite3.OperationalError:
        pass
    connection.commit()
    return connection


def _row_to_dict(row):
    if row is None:
        return None
    state = dict(row)
    state["auto_resume"] = bool(state["auto_resume"])
    state["run_args"] = json.loads(state["run_args_json"]) if state["run_args_json"] else {}
    return state


def get_state(project_path, db_path=None):
    with _connect(db_path) as connection:
        row = connection.execute(
            "SELECT * FROM runs WHERE project_path = ?", (str(project_path),)
        ).fetchone()
        return _row_to_dict(row)


def list_states(db_path=None):
    with _connect(db_path) as connection:
        rows = connection.execute("SELECT * FROM runs ORDER BY last_updated DESC").fetchall()
        return [_row_to_dict(row) for row in rows]


def _upsert(project_path, db_path=None, **fields):
    fields["project_path"] = str(project_path)
    fields["last_updated"] = utc_now_iso()
    columns = list(fields.keys())
    placeholders = ", ".join("?" for _ in columns)
    update_clause = ", ".join(f"{column} = excluded.{column}" for column in columns if column != "project_path")
    with _connect(db_path) as connection:
        connection.execute(
            f"""
            INSERT INTO runs ({", ".join(columns)}) VALUES ({placeholders})
            ON CONFLICT(project_path) DO UPDATE SET {update_clause}
            """,
            [fields[column] for column in columns],
        )
        connection.commit()


def mark_running(project_path, stage, action=None, run_args=None, auto_resume=False, db_path=None):
    """
    Record that project_path is actively running stage. Clears any prior
    pause/error state. action is one of "generate", "fix", "verify" --
    generate/fix/verify are independent, separately-triggered operations
    (see pipeline.py's module docstring), so this is what tells
    due_paused_projects()/check_resume() which one to actually resume,
    since current_stage alone (a free-text label) isn't reliably
    machine-dispatchable on its own.
    """
    _upsert(
        project_path,
        db_path=db_path,
        status=STATUS_RUNNING,
        action=action,
        current_stage=stage,
        paused_reason=None,
        resume_at=None,
        auto_resume=int(bool(auto_resume)),
        run_args_json=json.dumps(run_args or {}),
        last_error=None,
    )


def mark_paused_quota(project_path, stage, reason, resume_at_dt, action=None, run_args=None, auto_resume=False, db_path=None):
    """Record a usage-limit pause, with the absolute time it's safe to resume."""
    _upsert(
        project_path,
        db_path=db_path,
        status=STATUS_PAUSED_QUOTA,
        action=action,
        current_stage=stage,
        paused_reason=reason,
        resume_at=to_utc_iso(resume_at_dt) if resume_at_dt else None,
        auto_resume=int(bool(auto_resume)),
        run_args_json=json.dumps(run_args or {}),
        last_error=None,
    )


def mark_paused_review(project_path, stage, reason, action=None, run_args=None, db_path=None):
    """Record a --review-required pause. Never auto-resumed -- only a human re-running the tool clears this."""
    _upsert(
        project_path,
        db_path=db_path,
        status=STATUS_PAUSED_REVIEW,
        action=action,
        current_stage=stage,
        paused_reason=reason,
        resume_at=None,
        auto_resume=0,
        run_args_json=json.dumps(run_args or {}),
        last_error=None,
    )


def mark_completed(project_path, stage=None, action=None, db_path=None):
    _upsert(
        project_path,
        db_path=db_path,
        status=STATUS_COMPLETED,
        action=action,
        current_stage=stage,
        paused_reason=None,
        resume_at=None,
        auto_resume=0,
        run_args_json=json.dumps({}),
        last_error=None,
    )


def mark_failed(project_path, stage, reason, action=None, run_args=None, db_path=None):
    _upsert(
        project_path,
        db_path=db_path,
        status=STATUS_FAILED,
        action=action,
        current_stage=stage,
        paused_reason=None,
        resume_at=None,
        auto_resume=0,
        run_args_json=json.dumps(run_args or {}),
        last_error=reason,
    )


def clear_project(project_path, db_path=None):
    """Remove a project's row entirely (e.g. after you've dealt with a FAILED run by hand, or for tests)."""
    with _connect(db_path) as connection:
        connection.execute("DELETE FROM runs WHERE project_path = ?", (str(project_path),))
        connection.commit()


def due_paused_projects(db_path=None):
    """
    Every project whose status is PAUSED_QUOTA, opted into auto_resume,
    and whose resume_at has already passed (compared as UTC ISO 8601
    strings, which sort correctly lexically). This is exactly what a
    periodic external ping (pipeline.py --check-resume) should act on.
    """
    now_iso = utc_now_iso()
    with _connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM runs
            WHERE status = ? AND auto_resume = 1 AND resume_at IS NOT NULL AND resume_at <= ?
            """,
            (STATUS_PAUSED_QUOTA, now_iso),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

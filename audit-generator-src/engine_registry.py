#!/usr/bin/env python3
"""
Engine Registry
===============

A small, pluggable wrapper around whichever AI CLI actually runs one audit
phase session. Today there is exactly one engine ("claude", wrapping the
Claude Code CLI's headless `claude -p` mode) but every caller in this tool
suite talks to engines only through get_engine()/EngineResult -- never by
shelling out to a specific CLI directly -- so a second engine (e.g. a
"gemini" adapter around Google's Gemini CLI, for the creative UI/UX cross-
check pass) can be added later as one more class in this file without
touching audit_generator.py at all.

The ClaudeEngine.run_session() implementation is a direct, generalized
port of run_claude_session() from audit_fix_runner.py / audit_verify_runner.py
-- same subprocess shape (`--output-format stream-json`), same live
console progress, same ANSI-stripped full-transcript logging, same
turn-limit/external-kill detection. Kept as its own copy rather than an
import from those scripts, matching this codebase's existing convention of
each tool being a single, standalone, dependency-free file.
"""

import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


# ======================================================================
# CONSOLE / LOGGING HELPERS
# (mirrors audit_fix_runner.py's versions exactly -- see that file for
# the reasoning behind each piece)
# ======================================================================

ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi_codes(text):
    """Remove terminal color/control escape codes so log files stay plain text."""
    return ANSI_ESCAPE_PATTERN.sub("", text)


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


FILE_EDIT_TOOL_NAMES = {"Edit", "Write", "MultiEdit"}
BASH_COMMAND_CONSOLE_PREVIEW_LENGTH = 80


def short_path(file_path, project_path):
    """Truncates absolute paths relative to project root for cleaner output."""
    if not file_path:
        return "file"
    try:
        rel = Path(file_path).relative_to(project_path)
        return f"./{rel}"
    except ValueError:
        return str(file_path)


def console_line_for_tool_use(tool_name, tool_input, project_path):
    """
    Returns a styled, human-readable console progress line for a tool call,
    or None for tool types not worth printing. See audit_fix_runner.py's
    version of this function for the full reasoning -- unchanged here.
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

    if tool_name in ("WebSearch", "WebFetch"):
        query = (tool_input or {}).get("query") or (tool_input or {}).get("url", "")
        return f"  {G}\U0001F310 Researching: {query}{R}"

    if tool_name == "Read":
        file_path = tool_input.get("file_path", "") if tool_input else ""
        clean_path = short_path(file_path, project_path)
        return f"  {G}\U0001F4D6 Reading {clean_path}...{R}"

    return None


def console_lines_for_stream_json_event(event, project_path):
    """
    Given one successfully parsed stream-json event, return a list of short
    console progress lines to print for it. See audit_fix_runner.py's
    version for full reasoning -- unchanged here, plus WebSearch/WebFetch
    handling above for the research-heavy phases this tool suite adds.
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
    True if a parsed stream-json "result" event shows the session was cut
    off by --max-turns, False if it ended normally, None if undetermined.
    See audit_fix_runner.py's version for the full reasoning -- unchanged.
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


def start_caffeinate():
    """Start `caffeinate -dim` for the duration of one session. See
    audit_fix_runner.py's start_caffeinate_for_row() for full reasoning."""
    global _caffeinate_warning_printed
    try:
        return subprocess.Popen(["caffeinate", "-dim"])
    except FileNotFoundError:
        if not _caffeinate_warning_printed:
            print("caffeinate not available -- sleep prevention disabled")
            _caffeinate_warning_printed = True
        return None


def stop_caffeinate(caffeinate_process):
    """Stop a caffeinate process started for one session, if any."""
    if caffeinate_process is None:
        return
    caffeinate_process.terminate()
    try:
        caffeinate_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        caffeinate_process.kill()
        caffeinate_process.wait()


# ======================================================================
# RESULT SHAPES & ERRORS
# ======================================================================

STATUS_SUCCESS = "success"
STATUS_TURN_LIMIT = "turn_limit"
STATUS_KILLED_EXTERNALLY = "killed_externally"
STATUS_ERROR = "error"


@dataclass
class EngineResult:
    """
    What every engine's run_session() returns, regardless of which
    underlying CLI produced it -- this is the seam that keeps
    audit_generator.py from ever needing to know which engine it's talking
    to.

    status: one of the STATUS_* constants above. STATUS_SUCCESS means the
        session finished on its own (whether or not it judged its own task
        "successful" -- e.g. a phase session that gets partway through and
        stops with an error message it wrote itself still finished on its
        own). Callers determine actual task success by checking that the
        expected output file was actually written, not from this field.
    returncode: the subprocess's raw exit code.
    result_event: the parsed stream-json "result" event, if one was seen
        (may contain the CLI's own turn count, cost, and usage figures --
        passed through as-is rather than re-shaped, since its exact fields
        can change between CLI versions).
    log_path: where this session's full, ANSI-stripped transcript was
        written.
    """

    status: str
    returncode: int
    result_event: dict | None
    log_path: Path


class UnknownEngineError(Exception):
    """Raised by get_engine() for an engine name with no registered adapter."""


class EngineCLINotFoundError(Exception):
    """Raised when the underlying CLI binary for an engine isn't on PATH."""


@dataclass
class UsageInfo:
    session_percent: int | None
    week_percent: int | None
    session_reset: str | None
    week_reset: str | None


# ======================================================================
# CLAUDE ENGINE
# ======================================================================


class ClaudeEngine:
    """Adapter around the Claude Code CLI's headless mode (`claude -p`)."""

    name = "claude"

    def run_session(self, prompt, cwd, log_path, max_turns):
        """
        Run `claude -p <prompt> --permission-mode acceptEdits --verbose
        --output-format stream-json --max-turns <max_turns>` with its
        working directory set to cwd, streaming live console progress and
        writing the full ANSI-stripped transcript to log_path (append
        mode -- callers may have already written a header to this file).

        A `caffeinate -dim` process runs for the duration of this call
        only, and is always stopped before returning or raising.

        Returns an EngineResult. Raises EngineCLINotFoundError if `claude`
        isn't on PATH. KeyboardInterrupt is caught just long enough to
        terminate the subprocess cleanly, then re-raised.
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

        print("Preventing sleep for this session...")
        caffeinate_process = start_caffeinate()

        try:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                print("ERROR: the 'claude' command was not found on your PATH.")
                print("Install Claude Code and make sure 'claude' is runnable from your shell.")
                raise EngineCLINotFoundError("'claude' was not found on PATH")

            saw_result_event = False
            result_event = None

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
                            continue

                        if event.get("type") == "result":
                            saw_result_event = True
                            result_event = event

                        for console_line in console_lines_for_stream_json_event(event, cwd):
                            print(console_line)
                except KeyboardInterrupt:
                    process.terminate()
                    process.wait()
                    raise

            process.wait()

            # Same detection logic as audit_fix_runner.py's returncode/
            # saw_result_event handling: a negative/143 exit with no
            # "result" event ever seen means something killed the process
            # from outside (OS, supervisor, sleep) rather than the CLI
            # itself deciding to stop.
            if process.returncode in (-15, 143) and not saw_result_event:
                status = STATUS_KILLED_EXTERNALLY
            elif result_event is not None and detect_turn_limit_hit(result_event):
                status = STATUS_TURN_LIMIT
            elif process.returncode == 0:
                status = STATUS_SUCCESS
            else:
                status = STATUS_ERROR

            return EngineResult(
                status=status,
                returncode=process.returncode,
                result_event=result_event,
                log_path=log_path,
            )
        finally:
            if caffeinate_process is not None:
                stop_caffeinate(caffeinate_process)
                print("Sleep prevention released.")

    def check_usage(self):
        """
        Runs `claude /usage` and parses session/week percentages and reset
        times. Returns a UsageInfo with all-None fields if the command
        fails for any reason -- never raises.
        """
        try:
            completed = subprocess.run(
                ["claude", "/usage"], capture_output=True, text=True, timeout=30
            )
            output_text = completed.stdout
        except Exception:
            output_text = ""

        session_percent = week_percent = None
        session_reset = week_reset = None

        session_match = re.search(
            r"Current session[:\s].*?(\d+)%\s*used" r"(?:.*?resets\s+([^\n]+?)(?:\n|$))?",
            output_text,
            re.IGNORECASE,
        )
        if session_match:
            session_percent = int(session_match.group(1))
            if session_match.group(2):
                session_reset = session_match.group(2).strip()

        week_match = re.search(
            r"Current week[^\n:]*[:\s].*?(\d+)%\s*used" r"(?:.*?resets\s+([^\n]+?)(?:\n|$))?",
            output_text,
            re.IGNORECASE,
        )
        if week_match:
            week_percent = int(week_match.group(1))
            if week_match.group(2):
                week_reset = week_match.group(2).strip()

        return UsageInfo(
            session_percent=session_percent,
            week_percent=week_percent,
            session_reset=session_reset,
            week_reset=week_reset,
        )


# ======================================================================
# REGISTRY
# ======================================================================

_ENGINES = {
    "claude": ClaudeEngine(),
}


def get_engine(name):
    """
    Look up a registered engine by name (e.g. "claude"). Raises
    UnknownEngineError with the list of currently available engines if
    name isn't registered -- callers should surface this directly rather
    than guessing or silently falling back to a default, since silently
    running a different engine than the one an audit phase asked for would
    undermine the whole point of per-phase engine selection.
    """
    engine = _ENGINES.get(name)
    if engine is None:
        available = ", ".join(sorted(_ENGINES.keys()))
        raise UnknownEngineError(
            f"No engine registered for '{name}'. Available engines: {available}."
        )
    return engine

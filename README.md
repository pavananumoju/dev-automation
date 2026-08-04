# Audit Fix Runner

A small, dependency-free Python script that automates working through a
project's `AUDIT.md` file, one finding at a time.

## What it does

Your project's `AUDIT.md` file has a **Master Tracking Table**: a markdown
table listing findings or features, each with a `Fix Status` column (e.g.
`Not started`, `Fixed`, `Blocked`).

This tool:

1. Reads that table and finds every row still marked `Not started`.
2. Works through those rows **one at a time**, in `P0 → P1 → P2 → P3` order.
3. For **each row**, starts a brand-new, memory-free headless Claude Code
   session (`claude -p ...`). That session:
   - reads `AUDIT.md` and the relevant source files itself (fresh, no
     leftover context from any other row),
   - implements the fix,
   - writes a test that fails on the old behavior and passes with the fix,
   - runs your project's full test suite,
   - commits the change if everything passes,
   - updates its own row in `AUDIT.md` (`Fixed` + commit hash + a plain-
     language note, or `Blocked` + a reason).

The Python script itself is **only an orchestrator**. It never edits your
source code and never edits `AUDIT.md` directly — it only *reads*
`AUDIT.md` to decide what's left to do and to report progress. All real
work happens inside each `claude -p` session, which has proper file-editing
tools.

Because every row gets a completely fresh Claude session, context never
dilutes across a long run of many findings — session #40 knows exactly as
much (and as little unrelated clutter) as session #1.

## Requirements

- Python 3.8 or newer (standard library only — nothing to `pip install`)
- `git`, available on your `PATH`
- The Claude Code CLI (`claude`), available on your `PATH`
- A project whose root folder contains an `AUDIT.md` file with a Master
  Tracking Table — a markdown table with a header row containing columns
  named `ID`, `Severity`, `Fix Status`, and (optionally) `Notes`
- On macOS, this tool manages sleep prevention (`caffeinate`) automatically
  for you, per row — no need to run `caffeinate` manually anymore

## Every argument, explained

| Argument | Required? | Default | What it does |
|---|---|---|---|
| `--project PATH` | Yes | — | Path to the project root. Must contain `AUDIT.md` at its root. |
| `--branch NAME` | No | `audit-<today's date, YYYY-MM-DD>` | Git branch to work on. If it already exists, it's checked out and reused — **this is how resuming works.** |
| `--mode {manual,auto}` | No | `manual` | `manual`: pause after every row and wait for you to press Enter. `auto`: keep going automatically, only stopping at the end or on Ctrl+C. |
| `--severities LIST` | No | all severities | Comma-separated list like `"P0,P1"` to only process rows of those severities this run. |
| `--max-rows N` | No | no limit | Stop after processing N rows this run, as a safety valve. |
| `--max-turns N` | No | `60` | Cap each row's Claude session at N agent turns, so a session that gets stuck (e.g. polling in a loop waiting on a slow command) fails fast and cleanly instead of silently running out of budget. |
| `--test-cmd "CMD"` | No | none | The shell command that runs your project's full test suite, e.g. `"npm test"` or `"./gradlew testDebugUnitTest"`. Handed to each row's Claude session as the required verification step. If omitted, you'll be asked to type `yes` to confirm before continuing without one. |
| `--dry-run` | No | off | Print the full plan (branch, row order, count) and exit without running anything. |

### Examples

Show what would happen, without doing anything:

```bash
python3 audit_fix_runner.py \
  --project ~/code/my-app \
  --test-cmd "npm test" \
  --dry-run
```

Run every `Not started` row, pausing between each one so you can watch and
review:

```bash
python3 audit_fix_runner.py \
  --project ~/code/my-app \
  --test-cmd "npm test"
```

Run only the P0s, fully unattended:

```bash
python3 audit_fix_runner.py \
  --project ~/code/my-app \
  --test-cmd "npm test" \
  --severities P0 \
  --mode auto
```

Process at most 3 rows this run, as a safety valve while you build trust in
the tool:

```bash
python3 audit_fix_runner.py \
  --project ~/code/my-app \
  --test-cmd "npm test" \
  --max-rows 3
```

Continue an interrupted run on a specific branch:

```bash
python3 audit_fix_runner.py \
  --project ~/code/my-app \
  --branch audit-2026-08-01 \
  --test-cmd "npm test"
```

## Folder structure after a run

Nothing is created outside your project folder. Inside it, you'll find a
new `logs/` directory:

```
my-app/
├── AUDIT.md                  (updated in place by each Claude session)
├── logs/
│   ├── 20260803_142031_P0-3.log     (full console output of one row's session)
│   ├── 20260803_143512_P0-7.log
│   ├── 20260803_150022_P1-1.log
│   └── run_summary_20260803_142010.md   (one file per run of this tool)
└── ... your existing source files ...
```

- One `.log` file per row, named `<timestamp>_<row-id>.log`, containing
  that row's full Claude session output (colors/escape codes stripped, so
  it's readable in any plain text editor).
- One `run_summary_<start-timestamp>.md` file per invocation of this tool,
  updated after every row as the run progresses (so a summary of partial
  progress always exists, even if the run is interrupted), with a final
  summary appended at the end.

## Pausing and resuming

You can stop at any point — the tool is designed so this is always safe:

- **`--mode manual`** (the default) pauses after every row and asks you to
  press Enter to continue, or type `q` to stop there.
- **Ctrl+C** works at any time, including mid-row. The tool catches it,
  prints a friendly message, and exits cleanly — no raw Python traceback.
- Either way, nothing is lost. `AUDIT.md` and your git history reflect
  whatever was last fully completed.

**To resume, just run the exact same command again.** As long as you pass
the same `--branch` (or don't pass one, so the date-based default matches —
note the default is tied to *today's* date, so if you resume on a different
day, pass `--branch` explicitly to point back at the original branch), the
tool checks that branch back out, re-reads `AUDIT.md` fresh, and picks up
with whatever rows are still `Not started`.

One safety rule to know: if you start a run and the git working tree is
already dirty (uncommitted changes), the tool stops immediately and tells
you which row it was about to start. This almost always means a previous
row's session was interrupted mid-fix. Look at `git status` / `git diff`,
decide whether to commit, stash, or discard those changes (and reset that
row back to `Not started` in `AUDIT.md` if needed), then re-run.

### Hitting the turn limit vs. being genuinely Blocked

A row hitting `--max-turns` is a **distinct outcome** from a row being
genuinely `Blocked` — it means the session ran out of turns, not that the
finding was judged unfixable. If this happens the tool tells you clearly
in the console output. The working tree may still be left dirty in that
case (same as any other interrupted row), so the RESUME CHECK / dirty-tree
safety rule above still applies before the next row can start — inspect
`git status`/`git diff`, decide what to do with any partial work, and
consider re-running that specific row by hand with a higher `--max-turns`
before assuming it's simply a hard finding.

## A full example

```bash
python3 audit_fix_runner.py \
  --project ~/code/my-app \
  --branch audit-2026-08-03 \
  --mode manual \
  --severities P0,P1 \
  --max-rows 5 \
  --test-cmd "npm test"
```

This works through up to 5 `Not started` P0/P1 rows from
`~/code/my-app/AUDIT.md`, on branch `audit-2026-08-03`, pausing for your
review after each one, verifying every fix with `npm test`. Nothing is ever
pushed — review with `git log` and `git diff main..audit-2026-08-03` and
push it yourself when you're happy.

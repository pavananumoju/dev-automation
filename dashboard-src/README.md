# Dashboard

A local, browser-based status/control panel over `state_store.py` and
each tracked project's own `logs/`/`AUDIT.md` — check status, read recent
session output, and start or resume a run without a terminal.

This is the **only** piece of this whole tool suite with a dependency
outside Python's standard library (Streamlit). Every other tool —
`audit_generator.py`, `engine_registry.py`, `audit_compiler.py`,
`state_store.py`, `pipeline.py` — stays dependency-free on purpose. If you
never set this up, everything else still works exactly the same from the
terminal.

## One-time setup

Homebrew-managed Python (the default on macOS) refuses a bare
`pip install streamlit` — it requires either a virtualenv or
`--break-system-packages` (not recommended: it installs into the same
Python environment other tools rely on). This repo uses a dedicated,
isolated virtualenv:

```bash
cd dashboard-src
python3 -m venv .venv
.venv/bin/pip install streamlit
```

(`.venv/` is gitignored — nothing about it is meant to be committed. Delete
the folder any time to fully undo this with zero side effects elsewhere.)

## Running it

```bash
cd dashboard-src
.venv/bin/streamlit run app.py
```

Opens in your browser automatically (usually `http://localhost:8501`).
Leave the terminal running while you use it; `Ctrl+C` to stop.

## What it does

- **Overview table** — every project `state_store.py` knows about, with
  its current status (`RUNNING` / `PAUSED_QUOTA` / `PAUSED_REVIEW` /
  `COMPLETED` / `FAILED`) and stage.
- **Per-project detail** — pause reason and resume time (if paused), the
  last error (if failed), a live count of `AUDIT.md`'s findings by fix
  status, and the tail of that project's most recent session log.
- **"Run --check-resume now"** — trigger a resume check immediately
  instead of waiting for the next scheduled `launchd`/`cron` ping (see
  `../pipeline-src/README.md`).
- **"Start a new project"** — launch `pipeline.py` against a new project
  path right from the browser.

Every action launches `pipeline.py` as a detached background process
(`subprocess.Popen`, never awaited) — the dashboard itself never blocks on
a long-running audit, and refreshing or closing the browser tab never
kills an in-progress run. The dashboard only *reads* `state_store.py` and
log files; it holds no state of its own, so multiple browser tabs (or
restarting the dashboard) are always safe.

Auto-refresh (a sidebar checkbox, off by default) re-runs the whole page
every 15 seconds when enabled — a straightforward polling refresh, not
true log streaming; good enough to watch a run's progress without
constantly clicking refresh by hand.

# Pipeline

Three independent, separately-triggered actions, plus usage-limit
pause/resume via `state_store.py`'s SQLite table:

- **generate** — runs `audit_generator.py` (Stage 0-3, in
  `../audit-generator-src/`) and stops once `AUDIT.md` exists.
- **fix** — runs the existing, unchanged `audit_fix_runner.py` (in
  `../audit-fix-runner-src/`) against an already-generated `AUDIT.md`.
- **verify** — runs the existing, unchanged `audit_verify_runner.py`
  against an already-generated `AUDIT.md`.

These are deliberately **not** chained. Generating an audit, fixing what
it found, and verifying those fixes were always three separate concerns
for `audit_fix_runner.py`/`audit_verify_runner.py` even before any of
this pipeline tooling existed — you'd generate `AUDIT.md` once, then come
back to fixing/verifying whenever you had time, possibly days later.
`pipeline.py` keeps that shape: run `generate`, then whenever you're
ready (an hour later, a week later, doesn't matter), run `fix` and
`verify` against whatever `AUDIT.md` is sitting there.

## The four commands

```bash
python3 pipeline.py generate --project ~/code/my-app --review-required
python3 pipeline.py fix      --project ~/code/my-app --test-cmd "npm test"
python3 pipeline.py verify   --project ~/code/my-app --test-cmd "npm test"
python3 pipeline.py check-resume
```

`fix`/`verify` infer `--branch` from the last tracked run for that
project in `state_store.py` if you don't pass one explicitly — so you
don't need to remember or retype the branch `generate` used.

`check-resume` is meant to be pinged periodically by your OS's scheduler
(see below), not run interactively. It resumes every project that's
`PAUSED_QUOTA`, was launched with `--auto-resume`, and whose usage-reset
time has passed — dispatching each one by its own recorded action, so a
paused `generate` resumes as a `generate` and a paused `fix` resumes as a
`fix`, never conflated.

There's also `pipeline.py resume-project --project X`, for resuming one
specific paused project **right now** regardless of due time or whether
`--auto-resume` was set — this is what the dashboard's per-project
"Resume now" button calls. Works for both a usage pause and a
`--review-required` pause (`check-resume` only ever touches usage
pauses).

## Setting up the periodic ping (macOS `launchd`)

To actually get automatic resume rather than just the *capability* for
it, something needs to call `pipeline.py check-resume` on a timer. A
`launchd` user agent is the standard macOS way to do this (survives
reboots; does not require a terminal window to stay open):

1. Create `~/Library/LaunchAgents/com.devautomation.pipeline-resume.plist`:

   ```xml
   <?xml version="1.0" encoding="UTF-8"?>
   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
   <plist version="1.0">
   <dict>
     <key>Label</key><string>com.devautomation.pipeline-resume</string>
     <key>ProgramArguments</key>
     <array>
       <string>/usr/bin/python3</string>
       <string>/Users/YOURNAME/projects/Hustle/dev-automation/pipeline-src/pipeline.py</string>
       <string>check-resume</string>
     </array>
     <key>StartInterval</key><integer>1800</integer>
     <key>StandardOutPath</key><string>/tmp/pipeline-resume.log</string>
     <key>StandardErrorPath</key><string>/tmp/pipeline-resume.log</string>
   </dict>
   </plist>
   ```

   (`StartInterval` is in seconds — `1800` = every 30 minutes. Adjust the
   two paths for your actual username/checkout location.)

2. Load it: `launchctl load ~/Library/LaunchAgents/com.devautomation.pipeline-resume.plist`
3. Check it's running: `launchctl list | grep pipeline-resume`
4. To stop it: `launchctl unload ~/Library/LaunchAgents/com.devautomation.pipeline-resume.plist`

A plain `crontab -e` entry (`*/30 * * * * /usr/bin/python3 /path/to/pipeline.py check-resume >> /tmp/pipeline-resume.log 2>&1`) works too and is simpler to set up, but `launchd` is the more idiomatic macOS choice and handles the machine sleeping/waking a bit more gracefully.

Either way: if the Mac is fully asleep when a check would have fired, that
check simply doesn't happen until the next one after it wakes — there's no
way around that from user-space, on either scheduler.

## `--review-required` pauses don't block on input()

Unlike a naive "ask y/N and wait" implementation, `generate --review-required`
records a `PAUSED_REVIEW` state and exits cleanly the moment it needs your
review (after `AUDIT_PROMPT.md` is written, and again before compiling
`AUDIT.md`) — it never blocks on a terminal prompt. This matters because
`pipeline.py` is routinely launched as a detached background process (the
dashboard does this for every action) with no attached stdin, where a
blocking prompt would just hang forever, invisibly. Review the file, then
either re-run the exact same `generate` command or click **Resume now** in
the dashboard (`pipeline.py resume-project`) whenever you're ready — same
resume model as a usage-limit pause, just never picked up automatically by
`check-resume` (only you decide when a review pause is actually resolved).

## Scope boundary: what pause/resume does *not* cover

Automatic pause/resume is wired at every subprocess/session boundary this
script or `audit_generator.py` actually controls: between each Stage 0-3
phase, and before starting the fixer/verifier subprocess as a whole. It
can **not** pause partway through a single `fix`/`verify` run (e.g.
between row 12 and row 13) without modifying those existing, already-
tested tools — which this deliberately does not do. If usage runs out
mid-fix, that run stops the way it always has (see
`audit_fix_runner.py`'s own README's "Pausing and resuming" section) —
resume it by hand (or `fix` again with the same `--branch`), the same as
running it directly.

## `state_store.py`'s database

A single SQLite file, `pipeline_state.db` in this folder (gitignored — it's
local runtime state, not something to commit). One row per project path,
tracking status, which action (`generate`/`fix`/`verify`) it's on, and
everything needed to resume it. Safe to delete if you want a clean slate —
nothing about resuming a project depends on history beyond its current row.

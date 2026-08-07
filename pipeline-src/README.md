# Pipeline

The master conductor: chains `audit_generator.py` (Stages 0-3, in
`../audit-generator-src/`) into the existing, unchanged
`audit_fix_runner.py` and `audit_verify_runner.py` (in
`../audit-fix-runner-src/`), and adds usage-limit pause/resume across the
whole thing via `state_store.py`'s SQLite table.

## Two ways to run this

**Start (or resume, by re-running the exact same command) one project's
full pipeline:**

```bash
python3 pipeline.py --project ~/code/my-app --test-cmd "npm test"
```

Runs Stage 0-3 in-process, then, since `--test-cmd` was given, the fixer
and verifier as subprocesses on the same branch (each inheriting this
process's console, so you see their own live output exactly as if you'd
run them by hand). Omit `--test-cmd` to stop after `AUDIT.md` is
generated — same as running `audit_generator.py` directly.

**Check whether anything paused-for-usage is due to resume:**

```bash
python3 pipeline.py --check-resume
```

Meant to be pinged periodically by your OS's scheduler, not run
interactively. Cheap when nothing is due — one SQLite query, then exit.
Only ever acts on projects that were originally run with `--auto-resume`.

## Setting up the periodic ping (macOS `launchd`)

To actually get automatic resume rather than just the *capability* for
it, something needs to call `pipeline.py --check-resume` on a timer. A
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
       <string>--check-resume</string>
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

A plain `crontab -e` entry (`*/30 * * * * /usr/bin/python3 /path/to/pipeline.py --check-resume >> /tmp/pipeline-resume.log 2>&1`) works too and is simpler to set up, but `launchd` is the more idiomatic macOS choice and handles the machine sleeping/waking a bit more gracefully.

Either way: if the Mac is fully asleep when a check would have fired, that
check simply doesn't happen until the next one after it wakes — there's no
way around that from user-space, on either scheduler.

## Scope boundary: what pause/resume does *not* cover

Automatic pause/resume is wired at every subprocess/session boundary this
script or `audit_generator.py` actually controls: between each Stage 0-3
phase, and before starting the fixer/verifier subprocesses as a whole. It
can **not** pause partway through a single `audit_fix_runner.py` or
`audit_verify_runner.py` run (e.g. between row 12 and row 13) without
modifying those existing, already-tested tools — which this deliberately
does not do. If usage runs out mid-fix, that run stops the way it always
has (see `audit_fix_runner.py`'s own README's "Pausing and resuming"
section) — resume it by hand, the same as running it directly.

## `state_store.py`'s database

A single SQLite file, `pipeline_state.db` in this folder (gitignored — it's
local runtime state, not something to commit). One row per project path,
tracking status, current stage, and (for a `PAUSED_QUOTA` row) everything
needed to resume it. Safe to delete if you want a clean slate — nothing
about resuming a project depends on history beyond its current row.

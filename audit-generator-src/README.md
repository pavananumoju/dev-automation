# Audit Generator

Turns a bare project path into a fully-populated `AUDIT.md` — the file
`audit_fix_runner.py` and `audit_verify_runner.py` (in `../audit-fix-runner-src/`,
unchanged) already know how to work through. This replaces manually
copy-pasting a phased audit prompt between Claude Code sessions.

## What it does

Runs four stages, each a fresh, memory-free headless Claude Code session
(so context never dilutes the way it would in one long session):

1. **Project Scan** — explores the project, writes `audit-gen/PROJECT_PROFILE.md`
   (thorough) and `audit-gen/CONTEXT_MANIFEST.md` (short — tech stack, output
   surface, design tokens if any; read by every later phase).
2. **Competitive & UX Research** — web-searches comparable projects and
   current best practices for this project's specific tech/output surface,
   writes `audit-gen/RESEARCH_NOTES.md`.
3. **Audit Prompt Build** — reads all of the above, decides how many audit
   phases make sense for this specific project (typically 4-8, only the
   relevant categories), writes `audit-gen/AUDIT_PROMPT.md`.
4. **Phased Execution** — runs each phase from `AUDIT_PROMPT.md` as its own
   fresh session, each writing its findings as JSON to its own file under
   `audit-gen/staging/`. Once every phase has staged its findings,
   `audit_compiler.py` — plain Python, no AI — compiles them into
   `AUDIT.md`'s Master Tracking Table in one deterministic pass.

See `SCHEMA.md` for the exact file formats every stage produces and
depends on.

## Requirements

Same as `audit_fix_runner.py`: Python 3.8+ (standard library only), `git`
on your `PATH`, the Claude Code CLI (`claude`) on your `PATH`, and a
project that's a git repository. WebSearch/WebFetch tool access for
`claude` is needed for Stage 1 (research) to do anything useful.

## Resuming

Every stage checks whether its own output file already exists before
running — and, within Stage 3, every phase checks whether its own staging
file already exists. Re-running the exact same command after an
interruption, a `--review-required` pause, or a usage-limit stop picks up
exactly where it left off; nothing already done is redone. Same idea as
`audit_fix_runner.py`'s branch-based resume, just at the file level.

## `--review-required`

Off by default. When set, the run pauses twice for your `y/N` confirmation:
right after `AUDIT_PROMPT.md` is generated (a chance to read it, or
hand-edit it, before any phase actually executes it), and again after every
phase has staged its findings but before they're compiled into `AUDIT.md`.
Declining either pause stops the run cleanly — re-run the same command
whenever you're ready to continue.

## Example

```bash
python3 audit_generator.py \
  --project ~/code/my-app \
  --review-required
```

Then, once `AUDIT.md` exists, hand off to the fixer **on the same branch**:

```bash
python3 ../audit-fix-runner-src/audit_fix_runner.py \
  --project ~/code/my-app \
  --branch audit-gen-2026-08-08 \
  --test-cmd "npm test"
```

(The generator prints this exact next-step command, with the real branch
name filled in, when it finishes.)

## What's out of scope (v1)

- **Re-auditing a project that already has an `AUDIT.md`.** The generator
  refuses to run at all if one already exists — merging new findings into
  an existing, possibly hand-edited or partially-fixed table is a real but
  different problem, not attempted here yet.
- **Engines other than `claude`.** `AUDIT_PROMPT.md` can already mark a
  phase with `**Engine:** gemini`, but no Gemini adapter is registered in
  `engine_registry.py` yet — that phase will fail clearly, naming which
  phase and pointing at the registry, rather than silently running on the
  wrong engine.

# Audit Generator — Shared Schema

This file is the single source of truth for every file format the audit
generation pipeline (Stages 0-3) produces and consumes. `audit_generator.py`
writes to these shapes; `audit_fix_runner.py` and `audit_verify_runner.py`
(unchanged, existing tools) read the end result. If this file changes, both
sides must stay in sync.

Two different risk levels get two different treatments here:

- **The audit prompt** (`AUDIT_PROMPT.md`) is written **once**, by a single
  Stage 2 session, and only ever read after that — never incrementally
  appended to by multiple sessions. Low drift risk. It stays a plain,
  human-editable Markdown file, matching how you already work today, and
  giving you a real point to review/hand-edit it before Stage 3 runs.
- **Findings** (what becomes `AUDIT.md`'s table) are appended by *many*
  independent Stage 3 phase sessions, one phase at a time, into the same
  eventual table. That's the actual high-risk pattern (malformed rows,
  duplicate IDs, shifted columns). Each phase session emits structured JSON
  instead of editing Markdown directly, and a small deterministic Python
  script (no AI involved) compiles all of it into the final `AUDIT.md`
  table in one pass, once.

---

## 1. `AUDIT_PROMPT.md` — the phased audit prompt (Stage 2 output)

Plain Markdown, authored once by a single Stage 2 Claude session, meant to be
read (and optionally hand-edited by you) before Stage 3 executes it. Stage 3
parses it defensively — the same spirit as how `audit_fix_runner.py` already
parses `AUDIT.md`'s table without requiring perfect formatting.

Required structure, one block per phase:

```markdown
## Phase <N>: <Title>

**Category focus:** <e.g. UI/UX & Visual Design, Performance, Security>
**Engine:** claude

**Objective:** <one paragraph — what this phase is trying to find>

**Prompt:**
```
<the literal, full prompt text sent verbatim to this phase's fresh
claude -p session — everything from here to the closing fence>
```
```

Parsing rules Stage 3 uses (defensive, matches existing style):
- A phase block starts at a line matching `## Phase (\d+):\s*(.+)`.
- `**Engine:**` is optional; defaults to `claude` if missing or unrecognized.
- The `**Prompt:**` fenced block is the only thing actually sent to that
  phase's session — everything else (category, objective) is
  bookkeeping/display only, never sent as-is.
- If no phase blocks are found at all, Stage 3 refuses to run and tells you
  to check `AUDIT_PROMPT.md`'s formatting rather than guessing.

## 2. Per-finding JSON object (what each phase session emits)

Each Stage 3 phase session's job is to investigate its focus area and emit
zero or more findings as JSON — not to write directly into a shared
Markdown table.

```json
{
  "local_id": "phase03-001",
  "category": "UI/UX",
  "severity": "P1",
  "finding": "Primary CTA button uses the same gray as disabled buttons, making it look inactive.",
  "notes": "Observed on the Home and Checkout screens; contrast ratio is 1.8:1 against the background."
}
```

| Field | Required | Notes |
|---|---|---|
| `local_id` | Yes | Scoped to this phase only, e.g. `"phase03-001"`. Never the final row ID — the compiler (below) assigns real IDs later. Just needs to be unique within this phase's own output. |
| `category` | Yes | Free text, but keep to a consistent vocabulary: `UI/UX`, `Performance`, `Accessibility`, `Security`, `Backend/API`, `Database`, `Offline & Sync`, `Legal/Compliance`, `DevEx/Tooling`, `Testing`, `Documentation`, `Other`. |
| `severity` | Yes | `P0` / `P1` / `P2` / `P3` (`P0` = highest). Matches `audit_fix_runner.py`'s existing sort order exactly. |
| `finding` | Yes | One or two sentences: what's wrong and where. This is the "Finding" column text. |
| `notes` | No | Any extra context worth keeping (how it was found, a source cited from research, etc.). Becomes the starting value of the row's `Notes` column — the fix session will overwrite this later. |

`fix_status`, `id`, and `commit` are **not** set by the phase session — the
compiler fills `fix_status` to `Not started` and assigns `id` for every
finding; `commit` starts blank and is only ever filled in later by
`audit_fix_runner.py` itself.

## 3. Phase staging files (how findings actually reach disk)

Each phase session doesn't edit any shared file. It writes **its own**
staging file:

```
<project>/audit-gen/staging/phase-<NN>-<slug>.json
```

e.g. `audit-gen/staging/phase-03-ui-ux.json`:

```json
{
  "phase_number": 3,
  "phase_title": "UI/UX & Visual Design",
  "findings": [ { "...": "one or more finding objects, per §2" } ]
}
```

Written atomically (temp file + rename), so a file is either fully present
and complete, or not there at all — never half-written. This is also what
makes Stage 3 resumable: on a re-run, a phase whose staging file already
exists is treated as done and skipped; only phases without one are
(re-)executed. No separate "progress" tracking needed for this part.

## 4. The compiler — plain Python, not AI (Stage 3 → Stage 4 handoff)

Runs exactly once, after every phase's staging file exists. Reads every
`audit-gen/staging/phase-*.json` file in phase-number order, and for each
finding, assigns a final global ID by severity in the order encountered:
`P0-1`, `P0-2`, ..., `P1-1`, `P1-2`, ... — the same `SEVERITY-N` shape your
existing logs and scripts already use. Then writes the full Master Tracking
Table (§5) into `AUDIT.md` at the project root in one shot.

This is deterministic: same staging files in, same table out, every time.
That determinism is exactly what makes it safe to treat as a pure compile
step rather than another AI call.

**Scope boundary for v1:** the compiler only runs against a project that
does *not* already have an `AUDIT.md`. If one exists, Stage 3 stops and
tells you, rather than trying to silently merge into hand-written or
previously-generated content. Re-running audits on an already-audited
project is a real need eventually, but it's a different, harder problem
(matching existing rows, avoiding re-flagging fixed issues) — not solving it
now.

**Important footgun to avoid:** once `audit_fix_runner.py` starts editing
individual rows in `AUDIT.md` (Stage 4), the compiler must never run again
over that same file — it would blow away Stage 4's edits by regenerating the
whole table from staging. The compiler is a one-time handoff, not something
in the ongoing loop.

## 5. The Master Tracking Table (final shape, in `AUDIT.md`)

```markdown
## Master Tracking Table

| ID | Phase | Category | Severity | Finding | Fix Status | Commit | Notes |
|---|---|---|---|---|---|---|---|
| P0-1 | 3 | UI/UX | P0 | Primary CTA button uses the same gray as disabled buttons. | Not started | | Observed on Home and Checkout screens. |
| P1-1 | 5 | Performance | P1 | Cold start takes 4.2s on a Pixel 3a. | Not started | | Profiled with the Android Studio CPU profiler during Phase 5. |
```

| Column | Read by the runner scripts? | Notes |
|---|---|---|
| `ID` | Yes | Falls back to first column if not literally named `ID`, but always include it explicitly. |
| `Phase` | No (display/traceability only) | Which Stage 3 phase found this — lets you trace back to `AUDIT_PROMPT.md` and `RESEARCH_NOTES.md`. |
| `Category` | No (display only) | From §2. |
| `Severity` | Yes | `P0`-`P3`; anything else sorts last, alphabetically. |
| `Finding` | No (display only) | What's wrong. |
| `Fix Status` | Yes — this is the required column; its exact name (case-insensitive) is how `audit_fix_runner.py` locates this table at all. | New rows from generation are always `Not started`. Later becomes `Fixed`, `Fixed (unverified)`, `Fixed (verified)`, `Partially fixed (unverified)`, or `Blocked` — set by the fix/verify sessions themselves, never by the generator. |
| `Commit` | No (display only, filled in later) | Left blank at generation time; `audit_fix_runner.py` fills it in when a row is fixed. |
| `Notes` | Yes (optional) | Seeded from §2's `notes` field; overwritten with verification detail later. |

This is a superset of the 4 columns (`ID`, `Severity`, `Fix Status`, `Notes`)
`audit_fix_runner.py`/`audit_verify_runner.py` already require — the extra
columns (`Phase`, `Category`, `Finding`, `Commit`) are ignored by their
column-position lookup, so nothing about them needs to change.

## 6. Generator working folder layout

```
<project>/
├── AUDIT.md                          (final output — same file the existing runners already expect)
└── audit-gen/                        (new — everything the generator itself produces)
    ├── PROJECT_PROFILE.md            (Stage 0)
    ├── CONTEXT_MANIFEST.md           (Stage 0 — slim, carried into every later phase)
    ├── RESEARCH_NOTES.md             (Stage 1)
    ├── AUDIT_PROMPT.md               (Stage 2 — see §1)
    └── staging/
        ├── phase-01-....json         (Stage 3, one file per phase — see §3)
        ├── phase-02-....json
        └── ...
```

Mirrors the existing `logs/` convention — everything this tool produces
lives in its own subfolder, nothing scattered into the project root except
the one file downstream tools already expect: `AUDIT.md` itself.

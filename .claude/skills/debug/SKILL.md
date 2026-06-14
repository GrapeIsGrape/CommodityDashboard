---
name: debug
description: Use when the user wants to investigate, diagnose, or root-cause a bug or unexpected behaviour in CommodityDashboard (bad/missing/stale/duplicate data, a failing ETL job, a broken panel). Read-only — gathers evidence, forms ranked hypotheses, and produces a structured bug summary without editing any code.
allowed-tools: Read, Grep, Glob, Bash
---

# Debug Skill — Symptom-Driven Debugger for CommodityDashboard

You are acting as a **read-only debugger** for the **CommodityDashboard** project. Your role is to investigate bugs by gathering evidence, forming ranked hypotheses, and producing a structured bug summary — **never touching any code**.

---

## Step 1 — Load project context

Read the following before doing anything else:

1. `CLAUDE.md` at the repo root — architecture, tech stack, key tables, ETL sources, engineering principles, deployment targets (usually already in context — only re-read if it isn't)
2. [README.md](README.md) — for the panel or data source relevant to the reported symptom, read the matching section (Panel A–D, the source notes, or the phase plan)

Do not skip this step. The data-source boundaries and the append-only / idempotency rules are non-obvious and must be grounded before any investigation.

---

## Step 2 — Ask targeted clarifying questions (single message)

Ask all of the following in **one message**. Do not split them across turns.

1. **Environment:** Is this happening on Railway, Synology NAS, or local?
2. **Component:** Which ETL source/module, table, or dashboard panel is involved? (e.g. the FRED macro job, the CFTC COT job, the Volatility panel, the `iv_metrics` table)
3. **Expected vs observed:** What did you expect to happen? What actually happened?
4. **Scope:** Is this affecting all symbols, one complex, a single symbol, or one data source?
5. **Recency:** Did this start after a recent deployment, schema/migration change, or an upstream-API change (FRED/EIA/USDA/CFTC/Yahoo)? If so, what changed?
6. **Reproducibility:** Is this consistently reproducible, or intermittent?

Do not proceed to Step 3 until you have answers.

---

## Step 3 — Read relevant source files

Based on the answers from Step 2, read the relevant source files. Start from the ETL module or dashboard component named by the user, then follow references to shared services, the symbol config, and helpers as needed.

Typical areas to consider (resolve to actual paths once the Phase 1 scaffold exists):

| Symptom area | Where to look |
|--------------|---------------|
| Macro data wrong/missing | FRED ETL module, `macro_metrics` table, symbol config |
| Energy inventory wrong/missing | EIA ETL module, `inventories` table |
| Grains data wrong/missing | USDA ETL module, `inventories` table |
| Positioning wrong/missing | CFTC COT ETL module, `cot` table |
| IV / vol wrong/missing | yfinance IV module (the `get_iv` interface), `iv_metrics` table, proxy mapping in config |
| Prices / macro-context wrong | yfinance price module, `prices` table |
| Curve shape wrong | curve-shape module, `curve_shape` table |
| Duplicate / overwritten rows | the source module's upsert + the migration defining the natural-key constraint |
| Dashboard panel broken | the FastAPI route/service for that panel, the query it runs |
| Job runs but others fail | per-source error isolation; check the failing module's try/except and logging |
| DB connection / config | the shared DB/connection helper, `.env` / settings |

Read only what is needed. Do not read files speculatively.

---

## Step 4 — Check recent git history

Run the following and review the output for commits that could relate to the symptom:

```bash
git log --oneline -20
```

If a suspicious commit is identified, read the diff:

```bash
git show <commit-hash>
```

Note any commits that touched the affected source module, changed an upsert or natural-key constraint, altered a migration, or changed a dashboard query.

---

## Step 5 — Query the database (if needed)

If the symptom involves missing, incorrect, duplicated, or stale data, query the database read-only using the project's connection helper via a Python one-liner or psql.

**Hard rules for DB queries:**
- Any query against an append-only time-series table (`prices`, `macro_metrics`, `inventories`, `cot`, `iv_metrics`, `curve_shape`) **must** include a `WHERE` clause on the natural-key columns (e.g. `symbol`, `metric`, `date`). Never run a full table scan.
- Treat all DB access as read-only. Do not issue INSERT, UPDATE, DELETE, or DDL.
- Ask the user for the relevant symbol, metric, and date before querying if not already provided.

Example safe queries:

```sql
-- Is there macro data for a metric on a date?
SELECT * FROM macro_metrics WHERE symbol = 'DXY' AND metric = 'level' AND date = '2026-06-12';

-- Check for duplicate rows on a natural key (idempotency bug)
SELECT symbol, metric, date, COUNT(*)
FROM cot
WHERE symbol = 'GC' AND date = '2026-06-10'
GROUP BY symbol, metric, date
HAVING COUNT(*) > 1;

-- Latest IV snapshots for a proxy
SELECT * FROM iv_metrics WHERE symbol = 'SLV' ORDER BY date DESC LIMIT 10;
```

---

## Step 6 — Form a ranked hypothesis list

Based on all evidence gathered (source code, git history, DB data, user answers), produce a ranked list of hypotheses from most to least likely. For each:

- State the hypothesis clearly
- Cite the specific evidence supporting it (file path and line number where relevant)
- Identify what would confirm or rule it out

Example format:

```
1. [Most likely] COT job not idempotent — duplicate rows on same-day re-run
   — Evidence: cot has 2 rows for (GC, 2026-06-10); upsert missing ON CONFLICT on the natural key
   — Confirms if: the migration has no unique constraint on (symbol, metric, date)

2. IV missing because the proxy mapping is absent for this commodity
   — Evidence: iv_metrics has no rows for HG; config has no optionable-proxy entry for copper
   — Confirms if: adding the proxy mapping yields a chain on the next run
```

---

## Step 7 — Suggest one investigation step at a time

Suggest exactly **one** targeted investigation step. This may be:
- A specific log line to look up
- A DB query to run (with full SQL, respecting the WHERE-clause rules above)
- A value to manually verify (e.g. compare a stored metric against the upstream API)
- A specific code path to trace

**Do not suggest multiple fixes simultaneously.** One step, then wait.

---

## Step 8 — Check in after each step

After each investigation step, ask:

> "Did that confirm, rule out, or change anything? Is the issue resolved, or should we continue investigating?"

Only proceed to the next step after the user responds. Adjust the hypothesis ranking based on new information.

---

## Step 9 — Produce structured bug summary (read-only — no code changes)

Once the root cause is confirmed, produce the following structured summary and **stop**. Do not implement any fix.

```
## Bug Summary

### Observed behaviour
<What actually happened — be specific: environment, source/panel, symbol/metric/date if relevant>

### Expected behaviour
<What should have happened>

### Root cause
<One-paragraph explanation of why the bug occurs — cite specific files and line numbers>

### Affected files
- `path/to/module.py` line XX — <what is wrong here>
- `path/to/migration.sql` line YY — <what else is affected>

### Proposed fix
<Concrete description of the change needed — no code, just the intent>
<If multiple files need changes, list each one>

### Notes
<Any caveats, edge cases, or related issues worth flagging>
```

Then tell the user:

> **Start a new `/ba` conversation and paste the above summary as your opening message to create a properly tracked GitHub ticket for this bug.**

---

## Hard constraints

- **Never write, edit, or create any file** at any point during this skill.
- **Never suggest running a fix** — only investigation steps.
- **Never run full table scans** against the append-only time-series tables.
- **Never assume** behaviour not grounded in `CLAUDE.md` or `README.md`.
- **Never skip Step 1** — always load context before investigating.

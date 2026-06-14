---
name: implement
description: Use when the user wants to implement, build, or work on a CommodityDashboard GitHub issue by number. Fetches the ticket, clarifies ambiguities, confirms a plan, builds following project conventions, adds tests, runs a security audit, and produces a commit message.
---

# Implement Skill — Ticket Implementation for CommodityDashboard

You are acting as the implementer for the **CommodityDashboard** project. Given a GitHub issue number, you fetch the ticket, load project context, ask clarifying questions if needed, then implement the changes following the project's conventions.

---

## Step 1 — Load project context

Read `CLAUDE.md` at the repo root in full before doing anything else (it is usually already loaded into context — only re-read if it isn't). Ground yourself in:
- Architecture: FastAPI dashboard (read-only), standalone Python ETL modules (one per source), single shared PostgreSQL, Docker Compose (`postgres` / `etl` / `dashboard`)
- Engineering principles: append-only / idempotent ETL, config-driven symbol list, secrets in env, migrations for every schema change, each source isolated so one failure can't break the others
- Deployment targets: Railway or Synology NAS, plus local — never hardcode hosts/ports/credentials/keys
- Key tables and the panel ↔ table ↔ source mapping
- Current build phase

If the scaffold already exists, also skim the repo structure (services, ETL modules, migrations dir, symbol config) before planning — the §7 of CLAUDE.md should point to it.

---

## Step 2 — Fetch the GitHub issue

```
Use mcp__github__issue_read with:
  owner: GrapeIsGrape
  repo: CommodityDashboard
  issue_number: <number provided by the user>
```

Read the issue title, body, acceptance criteria, technical notes, and any comments. If the issue number does not exist or returns an error, tell the user and stop.

---

## Step 3 — Clarify before coding

Before writing a single line of code, identify any ambiguities in the ticket. Ask **all clarifying questions in a single message** — do not drip-feed them. Categories to check:

### Scope & behaviour
- Is the expected behaviour fully specified, or are there decision points the ticket leaves open?
- Are there edge cases mentioned in the ticket that lack a defined outcome?
- Is any part of this actually Writer 2 (sentiment) work, which is out of scope for this project?

### Data, schema & idempotency
- Does this require a new table or column? If yes, is the schema (types, nullability, defaults, indexes) clear?
- What is the natural key for the upsert (e.g. `(symbol, metric, date)`), and must a same-date re-run be a no-op?
- Is history/backfill required, and how far back?
- Is backward compatibility with existing rows required?

### ETL source
- Which provider (FRED/EIA/USDA/CFTC/yfinance/other), and does it need an API key (→ `.env` + `.env.example`)?
- Is the source free, or does it need a paid feed / scraping that should be flagged?
- What cadence should the job run at, and does the scheduler need touching?

### Config
- Does this need a new symbol, complex, or commodity → optionable-proxy mapping in the symbol config?

### Dashboard
- Which panel does this surface in, and is the layout/highlight (COT extreme, rich IV, backwardation flag) specified clearly enough to build without guessing?

If the ticket is fully clear, skip this step and proceed directly to Step 4. State explicitly: "The ticket is clear — proceeding to implementation."

**Do not touch any code until clarifications are resolved.**

---

## Step 4 — Confirm implementation plan

Before coding, briefly state your plan:
- Which files you will create or modify (by path)
- Whether a DB migration is needed and what it does
- Whether a config change is needed (symbol / proxy mapping)
- Whether tests will be added or updated and where
- Any deployment or configuration steps required (new env var → `.env.example`)

Present this as a short bullet list. Ask: "Does this plan look right before I start?"

Wait for the user to confirm. If they say yes (or equivalent), proceed. If they redirect, update your plan and confirm again.

---

## Step 5 — Implement

Follow these conventions throughout:

### ETL modules
- Each data source is its **own module** with its own error handling and logging. One failing source must not break the others or the dashboard — never let an exception in one source propagate to another.
- **Append-only & idempotent:** insert new dated rows; never overwrite. Re-running a job for the same date must not create duplicates — upsert on the natural key (e.g. `(symbol, metric, date)`).
- Read the symbol list and any provider config from the **config file** — never hardcode symbols, and never hardcode hosts, ports, credentials, or API keys.
- Keep the IV source behind the swappable interface (e.g. `get_iv(symbol)`) so IBKR can replace yfinance later.
- Log via the project's logging pattern, not `print()`.

### Database
- PostgreSQL. New migrations go in the migrations directory following the existing naming convention (numbered SQL or Alembic — match what's there).
- Every schema change ships with a migration. Time-stamp every table; design so `sentiment_*` tables can be added later without touching data tables.
- Index foreign keys and columns used in `WHERE` clauses (e.g. `symbol`, `metric`, `date`).
- Use parameterised SQL — never build queries with f-strings / `.format()` / concatenation of external input.

### FastAPI dashboard
- The dashboard is **read-only** — it never writes market data and never executes trades or moves money.
- Compute values in the handler/service layer; keep logic out of templates.
- Match the existing route/template/component patterns once they exist.

### Secrets & config
- Any new key reads from env (`os.environ` / settings object). Add the key name to the committed `.env.example`; never commit `.env`.

### General
- No comments unless the WHY is non-obvious.
- No unused code, dead branches, or backwards-compatibility shims.
- No features beyond what the ticket requires. If the ticket needs a paid feed or scraping, flag it rather than silently adding it.

---

## Step 6 — Migrations

If the implementation includes a new migration (new table or new column), after writing the migration file:

1. Tell the user the migration file path and show the SQL.
2. Remind them how to run it for their target (local / Railway / Synology) per the project's migration approach.
3. Confirm the migration is idempotent-safe to re-apply where the tooling expects it.

---

## Step 7 — Tests

### 7a — Check for a tests/ directory

If no `tests/` directory exists at all, ask the user: "No `tests/` directory exists — should I create one before writing tests for this ticket?" Wait for their answer before proceeding. Do not create the directory or any test files without confirmation.

### 7b — Read existing tests before writing anything

Before writing a single test, read the existing files in `tests/` to understand:
- Which test framework and runner is in use (e.g. pytest)
- How tests are structured (fixtures, helpers, mocks, DB setup/teardown, how external APIs like yfinance/FRED are mocked)
- Naming conventions for test files, classes, and functions
- What is already covered, so you don't duplicate

### 7c — Determine what tests are needed

Based on the ticket changes, identify:
- **Add** — new tests for any new behaviour (especially: idempotency — re-running a job for the same date inserts no duplicates; one source failing doesn't break others)
- **Edit** — existing tests that need updating because behaviour changed
- **Remove** — existing tests that cover deleted functionality and would now fail or be meaningless

### 7d — Ask before writing if anything is unclear

If the expected test behaviour, edge cases, or test scope is unclear for any of the changes, ask all your questions in a **single message** and wait for answers before writing tests. Do not guess at expected outcomes or silently skip edge cases.

### 7e — Write or update the tests

Implement the tests identified in 7c, following the conventions observed in 7b. Mock external APIs — tests must not hit FRED/EIA/USDA/CFTC/Yahoo live. Do not introduce a new test framework.

### 7f — Run pytest and fix failures

Run `pytest` and check whether all tests pass.

If any tests fail:
1. Read the failure output carefully.
2. Determine whether the failure is in the test (wrong assertion, stale fixture) or in the implementation (broken logic).
3. Fix whichever is wrong.
4. Run `pytest` again.

Repeat until all tests pass cleanly. Do not consider the implementation complete until pytest exits with no failures.

---

## Step 8 — Security audit

After implementation is complete, invoke the security auditor sub-agent to review all changed files:

> "Use the security-auditor agent to audit the files changed in this ticket."

Wait for the security audit report. If any CRITICAL or HIGH findings are returned, resolve them before providing the commit message. MEDIUM findings should be shown to the user to decide whether to fix now or raise a separate ticket.

Do not provide the commit message until the security audit is clean or the user explicitly decides to proceed.

---

## Step 9 — Commit message

Once implementation is complete, provide a git commit message following the project format:

```
<type>: <short summary> #<issue number>

<optional body — explain WHY, not WHAT, if the change is non-trivial>
```

Types: `feat`, `fix`, `refactor`, `chore`, `docs`

Example:
```
feat: add FRED macro ETL source for DXY and real yields #12

Pulls DXY, 10Y real yield, and CPI into macro_metrics with idempotent
upsert on (symbol, metric, date). Backfills 5y of history on first run.
```

---

## Step 10 — CLAUDE.md staleness check

After implementation, review the sections of `CLAUDE.md` that relate to what was changed. Flag any sections that may be outdated as a result:

- If a new DB table was added → check **§5 Dashboard panels & key tables**
- If a new ETL source/module was added → check **§5** and the **§7 repository structure**
- If a new scheduled job or scheduler change was made → check **§2 / §6**
- If a new env var is required → check **§2 / §3** (env & config)
- If the repo scaffold was created or restructured → check **§7 repository structure** (currently a placeholder until Phase 1)
- If a phase was completed → check **§6 build status**

State which sections may need updating and what the change should be. Do **not** edit `CLAUDE.md` directly — show the suggested diff and wait for the user to approve.

---
name: ba
description: Use when the user wants to raise, file, draft, or report a new GitHub issue (feature, enhancement, bug, chore) for the CommodityDashboard repo — either for a specific task they describe, or to "draft the next ticket" where the skill infers what comes next from the roadmap, recent issues, and commits. Gathers detail through targeted questions, drafts the issue, and creates it only after explicit approval.
---

# BA Skill — Business Analyst for CommodityDashboard GitHub Issues

You are acting as a Business Analyst for the **CommodityDashboard** project. Your job is to help the user raise a well-structured GitHub issue by gathering enough detail through targeted questions, then drafting an issue for explicit approval before creating it.

---

## Step 0 — Determine which mode you are in

There are **two ways** the user invokes this skill. Detect which one from their request:

- **Mode A — Directed:** the user names a specific thing to do, e.g. *"add a curve-shape ETL source,"* *"file a bug about duplicate COT rows,"* *"raise a ticket to add UNG to the symbol config."* You already know the subject — proceed straight to **Step 1** then **Step 2**.

- **Mode B — Next ticket:** the user asks you to figure out what comes next, e.g. *"draft the next ticket,"* *"what should we work on next?,"* *"raise the next issue."* You do **not** yet know the subject — you must first reconstruct where the project is and propose the next unit of work. Do **Step 1**, then **Step 1B**, then continue to Step 2.

When in doubt, ask: *"Do you have a specific change in mind, or should I propose the next ticket from the roadmap?"*

---

## Step 1 — Load project context

Read `CLAUDE.md` at the repo root to ground yourself in (it is usually already loaded into context — only re-read if it isn't):
- The system's purpose: a **read-only, single-user** dashboard aggregating market data for commodity options selling
- Architecture: Python + FastAPI dashboard, standalone Python ETL modules, single shared PostgreSQL, Docker Compose
- The four panels (Macro, Fundamentals/Inventory, Positioning, Volatility), the macro-context sub-panel, and the placeholder sentiment panel
- Key tables: `prices`, `macro_metrics`, `inventories`, `cot`, `iv_metrics`, `curve_shape`, placeholder `sentiment_*`
- Data sources: FRED, EIA, USDA, CFTC, yfinance — all free; flag anything needing a paid feed or scraping
- Engineering principles: append-only / idempotent ETL, config-driven symbol list, secrets in env, migrations for every schema change, one module per source
- Deployment targets: Railway or Synology NAS, plus local
- Current build phase (stop after each phase for review)

If `CLAUDE.md` references the README for detail, read [README.md](README.md) for the affected area.

**In Mode A, this is enough — go to Step 2.** In Mode B, continue to Step 1B.

---

## Step 1B — (Mode B only) Reconstruct project state and propose the next ticket

When the user asked you to "draft the next ticket" (Mode B), do not guess — build an evidence-based picture of where the project is before proposing anything.

**1. Pin down the roadmap position.** From `CLAUDE.md` §6 ("Build status & phased plan") and the README, identify:
- The current phase and what is marked ✅ done vs in progress vs not started.
- The explicit "Current position" line and any "next" items called out (e.g. *"FRED done; EIA/USDA/CFTC follow the same pattern"*).
- The note to **stop after each phase for review** — surface this if the next logical ticket would cross a phase boundary.

**2. List the last 10 GitHub issues** (open and closed) to see what has just been done and what is already queued:

```
Use mcp__github__list_issues with:
  owner: GrapeIsGrape
  repo: CommodityDashboard
  state: all
  perPage: 10
  sort: created
  direction: desc
```

Read titles, labels, and state. An already-open issue covering the obvious next step means you should point the user to it rather than draft a duplicate.

**3. Check the last 20 commits if you need more signal** — to confirm what actually landed (issues can lag reality) or resolve ambiguity about what is half-done:

```
Use mcp__github__list_commits with:
  owner: GrapeIsGrape
  repo: CommodityDashboard
  perPage: 20
```

You may inspect a specific commit's message and changed files with `mcp__github__get_commit` (or local `git log`/`git show`) when a commit subject alone doesn't tell you whether a piece of work is complete. Look at commit messages, touched paths (which ETL source, migration, config, panel), and the `#N` issue references that tie commits to tickets.

**4. Synthesize the next unit of work.** Cross-reference the three sources:
- The roadmap says what *should* come next; the issues say what is *already filed*; the commits say what is *actually built*.
- Prefer the smallest coherent next step that matches the established phase pattern (e.g. if FRED is the done template for Phase 2, the next ETL source — EIA — is the natural candidate).
- Respect the "stop after each phase" rule: do not silently propose work from the next phase without flagging the boundary.

**5. Propose before drafting.** Present your reasoning to the user briefly: *"You're in Phase 2; FRED (#3) is done; issues #1–#3 are closed and nothing newer is open; the last commits added `etl/sources/fred.py`. The natural next ticket is the EIA ETL source following the same pattern. Want me to draft that, or did you have something else in mind?"* Let the user confirm or redirect, **then** continue to Step 2 with the agreed subject.

If the GitHub MCP calls fail or return nothing, fall back to local git (`git log -20`, `git show <sha>`) and say so — never fabricate the project state.

---

## Step 2 — Determine ticket type

Ask the user: **What type of ticket is this?** Present the options:

| Type | When to use |
|------|-------------|
| **Feature** | New capability that doesn't exist today (e.g. a new ETL source, a new panel) |
| **Enhancement** | Improvement to something that already exists (UX, performance, an existing ETL job) |
| **Bug** | Something is broken or behaving incorrectly (e.g. duplicate rows, stale data, failing job) |
| **Chore** | Maintenance, refactoring, dependency updates, config/schema changes — no user-facing change |
| **Other** | Anything that doesn't fit above — ask the user to describe it |

If the type is ambiguous from context, suggest the most likely one and ask for confirmation rather than presenting the full menu.

Then ask focused clarifying questions based on type. Do not ask all questions at once — start with the most important, then follow up based on answers. Aim for 2–4 rounds maximum.

### For a feature, establish:
- What user problem or goal does this address? (e.g. "spot rich premium," "see COT extremes")
- Which part of the system is affected? (a specific ETL source/module, a panel, a DB table, the symbol config, the dashboard)
- If it's a new data source: which provider (FRED/EIA/USDA/CFTC/yfinance/other)? Free or paid? What natural key uniquely identifies a row (e.g. `(symbol, metric, date)`)?
- What should the user be able to see/do that they cannot today?
- Constraints: append-only/idempotency implications, history/backfill needs, deployment portability (Railway vs Synology)?
- Does this touch a scheduled ETL job, and if so which one and at what cadence?
- Any new DB tables or columns needed? Any migration?
- Does it need a config change (new symbol, new proxy mapping)?

### For an enhancement, establish:
- What currently exists and what is unsatisfying or limited about it?
- What does the improved experience look like?
- Which ETL module, table, or dashboard panel is affected?
- Is this a UX change, a performance improvement, or a data/behavioural change?
- Any risk of regression to adjacent ETL sources or panels?

### For a bug report, establish:
- What is the observed behaviour vs. the expected behaviour?
- Which environment did this occur in? (Railway / Synology / local)
- Which ETL module, table, or dashboard panel is involved?
- Is this reproducible? Steps to reproduce?
- What is the impact? (duplicate/overwritten rows, missing data, stale panel, failed job, dashboard error)
- Any error messages or log output? Did one source's failure affect others?
- When did it start? Was there a recent deployment, schema change, or upstream-API change?

### For a chore, establish:
- What is being cleaned up, updated, or restructured?
- Is there any risk of unintended behaviour change?
- Which modules, config files, or tables are affected?
- Is a DB migration or deployment step involved?

### For other, establish:
- Ask the user to describe the intent in their own words, then map it to the closest type or leave as "Other".

---

## Step 3 — Check for duplicate issues

Before drafting, search existing GitHub issues for duplicates:

```
Use mcp__github__search_issues with:
  owner: GrapeIsGrape
  repo: CommodityDashboard
  query: <keywords from the user's request>
```

If a duplicate or closely related issue is found, show it to the user and ask whether to proceed with a new issue or comment on the existing one.

---

## Step 4 — Draft the GitHub issue

Once you have enough information, produce a **draft issue** in this structure. Show it to the user inline — do NOT create it yet.

```
**Title:** <concise, imperative — e.g. "Add FRED macro ETL source for DXY and real yields" or "Fix: COT ETL inserts duplicate rows on same-day re-run">

**Type:** Feature | Enhancement | Bug | Chore | Other

---

## Description

<2–4 sentences: what this is, why it matters, what part of the system is affected>

---

## Acceptance Criteria

- [ ] <concrete, testable criterion>
- [ ] <criterion>
- [ ] ...

---

## Edge Cases & Constraints

- <edge case or constraint>
- <append-only / idempotency considerations — must re-running for the same date be a no-op?>
- <history/backfill depth if relevant>
- <deployment portability (Railway vs Synology) if relevant>

---

## Technical Notes

<Reference specific CommodityDashboard components as appropriate:>
- Affected ETL module(s) / source: FRED / EIA / USDA / CFTC / yfinance / other
- Affected dashboard panel(s): Macro / Fundamentals / Positioning / Volatility / Sentiment
- Affected DB table(s): `prices` / `macro_metrics` / `inventories` / `cot` / `iv_metrics` / `curve_shape` / `sentiment_*`
- Natural key for idempotent upsert: e.g. `(symbol, metric, date)`
- New migration needed: Yes / No
- New config entry needed (symbol / proxy mapping): Yes / No
- Schedule impact: Yes / No — <which job and cadence>
- Paid feed / scraping required: Yes / No — <flag it explicitly>
- Deployment notes: any Railway or Synology-specific considerations

---

## Out of Scope

<anything explicitly not included in this ticket — e.g. Writer 2 sentiment work is always out of scope here>
```

---

## Step 5 — Await explicit approval

After showing the draft, say:

> "Does this draft look good? Reply **approve** to create the issue, or let me know what to change."

Do **not** call `mcp__github__issue_write` until the user says "approve" (or equivalent confirmation).

---

## Step 6 — Create the issue

Once approved, create the issue:

```
mcp__github__issue_write:
  owner: GrapeIsGrape
  repo: CommodityDashboard
  title: <from draft>
  body: <full markdown body from draft>
  labels: ["feature"] | ["enhancement"] | ["bug"] | ["chore"] — match to ticket type
```

Confirm with the issue URL once created.

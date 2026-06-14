---
name: enrich-ticket
description: Use when the user wants to enrich, flesh out, restructure, or add detail to an existing CommodityDashboard GitHub issue. Asks targeted clarifying questions and rewrites the issue body into a structured form, updating only after explicit approval.
---

# Enrich Ticket Skill — GitHub Issue Enricher for CommodityDashboard

You are enriching an existing GitHub issue on the **CommodityDashboard** repo (owner: `GrapeIsGrape`, repo: `CommodityDashboard`). Your job is to take a raw or lightweight issue, ask targeted clarifying questions, then produce a fully structured issue body for explicit approval before updating it.

**Do not update the issue until the user explicitly approves the draft.**

---

## Step 1 — Get the issue number

If the user has not provided an issue number in their invocation message, ask:

> Which issue number would you like to enrich?

---

## Step 2 — Fetch the existing issue

Use `mcp__github__issue_read` with:
- `owner`: `GrapeIsGrape`
- `repo`: `CommodityDashboard`
- `issue_number`: the number provided

If the issue does not exist or returns an error, tell the user and stop.

Display to the user:
- Issue number, title, and current state (open/closed)
- The existing body (summarised if long)

---

## Step 3 — Load project context

Read `CLAUDE.md` at the repo root to ground yourself in (it is usually already loaded into context — only re-read if it isn't):
- The system's purpose: a read-only, single-user commodity-options market-data dashboard
- Architecture: FastAPI dashboard, standalone Python ETL modules, single shared PostgreSQL, Docker Compose
- Key tables: `prices`, `macro_metrics`, `inventories`, `cot`, `iv_metrics`, `curve_shape`, placeholder `sentiment_*`
- Data sources: FRED, EIA, USDA, CFTC, yfinance (free) — flag any paid feed or scraping
- Engineering principles: append-only / idempotent ETL, config-driven symbols, secrets in env, migrations per schema change, one isolated module per source
- Deployment targets: Railway or Synology NAS, plus local

If the issue touches a specific area, read [README.md](README.md) for that area's detail.

---

## Step 4 — Check for overlapping issues

Search existing GitHub issues for any open issues that cover the same ground as this one:

```
Use mcp__github__search_issues with:
  owner: GrapeIsGrape
  repo: CommodityDashboard
  query: <keywords from the issue title and body>
```

Exclude the issue being enriched from the results. If one or more overlapping open issues are found, surface them to the user:

> "I found issue(s) that may overlap with this one: #N — [title]. Do you want to merge the scope into one of those instead, or continue enriching this ticket?"

Wait for the user's answer before proceeding. If no overlapping issues are found, continue silently.

---

## Step 5 — Infer ticket type from the existing issue

Based on the issue title and body, infer one of:

| Type | When to use |
|------|-------------|
| **Feature** | New capability that doesn't exist today (new ETL source, new panel) |
| **Enhancement** | Improvement to something that already exists |
| **Bug** | Something is broken or behaving incorrectly |
| **Chore** | Maintenance, refactoring, config/schema changes — no user-facing change |
| **Other** | Doesn't fit above |

State your inferred type and ask the user to confirm or correct it before continuing.

---

## Step 6 — Ask clarifying questions

Using the issue title and any existing body content as context, ask targeted clarifying questions. Do **not** ask about things already clearly stated in the existing issue. Aim for 2–4 rounds maximum — ask the most important questions first, then follow up.

### For a Feature, ask about any gaps in:
- The user problem or goal being addressed
- Which part of the system is affected (ETL module/source, panel, DB table, symbol config)
- If a new data source: provider, free vs paid, the natural key for idempotent upsert
- What the user can see/do after this that they cannot today
- Constraints: append-only/idempotency, history/backfill depth, Railway vs Synology portability
- New DB tables or columns, and whether a migration is needed
- Schedule/cadence impact

### For an Enhancement, ask about any gaps in:
- What currently exists and what is unsatisfying about it
- What the improved experience looks like
- Which ETL module, table, or panel is affected
- Risk of regression to adjacent sources or panels

### For a Bug, ask about any gaps in:
- Observed vs expected behaviour
- Environment (Railway / Synology / local)
- Steps to reproduce
- Impact (duplicate/overwritten rows, missing data, stale panel, failed job, dashboard error)
- Error messages or log output; did one source's failure affect others
- When it started and whether a recent deployment, schema change, or upstream-API change preceded it

### For a Chore, ask about any gaps in:
- What is being cleaned up, updated, or restructured
- Risk of unintended behaviour change
- Modules, config files, or tables affected
- Whether a DB migration or deployment step is involved

### For Other:
- Ask the user to describe the intent in their own words, then map to the closest type.

---

## Step 7 — Draft the enriched issue body

Once you have enough information, produce a **draft** of the full issue body. Show it to the user inline — do **not** update the issue yet. Keep the original title unless the user asks to change it.

Use this structure:

```
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
- <append-only / idempotency considerations — must a same-date re-run be a no-op?>
- <history/backfill depth if relevant>
- <Railway vs Synology portability if relevant>

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
- Paid feed / scraping required: Yes / No — <flag it>
- Deployment notes: any Railway or Synology-specific considerations

---

## Out of Scope

<anything explicitly not included — Writer 2 sentiment work is always out of scope here>
```

---

## Step 8 — Await explicit approval

After showing the draft, say:

> "Does this draft look good? Reply **approve** to update the issue, or let me know what to change."

Do **not** call `mcp__github__issue_write` until the user says "approve" (or equivalent explicit confirmation).

If the user requests changes, revise the draft and show it again. Repeat until approved.

---

## Step 9 — Update the existing issue

Once approved, update the **existing** issue using `mcp__github__issue_write` with:
- `owner`: `GrapeIsGrape`
- `repo`: `CommodityDashboard`
- `issue_number`: the original issue number (do **not** create a new issue)
- `body`: the full approved markdown body

Do **not** change the issue title unless the user explicitly asked to change it.

---

## Step 10 — Confirm to the user

Report back:

> Issue #N — "[title]" has been updated.

Provide a link to the issue.

---

## Notes

- Never skip Step 8 (approval). This is a hard requirement.
- If the existing issue already has a well-structured body, acknowledge what is already good and focus clarifying questions only on gaps.
- If the user provides all necessary detail upfront in their invocation message, you may skip clarifying questions and proceed directly to drafting — but still show the draft for approval before updating.
- Do not modify any other issues or repos.
- Do not create a new issue — always update the existing one at the original issue number.

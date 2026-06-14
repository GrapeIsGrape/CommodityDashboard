---
name: list-issues
description: Use when the user wants to list, see, or browse the open GitHub issues in the CommodityDashboard repo.
---

# List Issues Skill — GitHub Issue Lister for CommodityDashboard

You are listing open GitHub issues from the **CommodityDashboard** repo (owner: `GrapeIsGrape`, repo: `CommodityDashboard`).

---

## Step 1 — Fetch open issues

Use the `mcp__github__list_issues` tool with:
- `owner`: `GrapeIsGrape`
- `repo`: `CommodityDashboard`
- `state`: `open`

---

## Step 2 — Display the results

If there are no open issues, say:

> There are no open issues in the CommodityDashboard repo.

If there are open issues, display them as a clean numbered list in this format:

> **Open Issues — CommodityDashboard**
>
> 1. #12 — Issue title here
> 2. #9 — Another issue title
> 3. #3 — Yet another issue

Use the issue's actual `number` field (prefixed with `#`) and `title` field. Order by issue number ascending (oldest first).

---

## Notes

- Only list issues with `state: open`. Do not show closed issues.
- Do not add commentary, summaries, or suggestions unless the user asks.
- Do not take any action on the issues (no closing, commenting, or editing).

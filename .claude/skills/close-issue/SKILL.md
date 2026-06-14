---
name: close-issue
description: Use when the user wants to close, resolve, or mark done a CommodityDashboard GitHub issue. Verifies the acceptance criteria are met, finds the related commit, drafts a closing comment, and closes only after explicit confirmation.
---

# Close Issue Skill — GitHub Issue Closer for CommodityDashboard

You are closing a GitHub issue on the **CommodityDashboard** repo (owner: `GrapeIsGrape`, repo: `CommodityDashboard`).

**Do not close or comment on any issue until the user explicitly confirms.**

---

## Step 1 — Get the ticket number

If the user has not provided an issue number in their invocation message, ask:

> Which issue number would you like to close?

---

## Step 2 — Fetch and confirm the issue exists

Use the `mcp__github__issue_read` tool with:
- `owner`: `GrapeIsGrape`
- `repo`: `CommodityDashboard`
- `issue_number`: the number provided

If the issue does not exist or returns an error, tell the user and stop.

If it exists, display:
- Issue number and title
- Current state (open/closed)

If the issue is already closed, tell the user and stop.

---

## Step 3 — Verify the work is actually done

Before closing, sanity-check that the ticket's acceptance criteria appear to be met. Read the issue's acceptance criteria and confirm with the user that each is satisfied. If a related commit exists (Step 4), use it as evidence. Do not close a ticket whose criteria are unmet — surface the gap instead.

---

## Step 4 — Find the related commit

Search recent git history for commits referencing this issue number:

```bash
git log --oneline -20 | grep -w "#<issue_number>"
```

If a matching commit is found:
- Read the full commit message using `git show <sha>`
- Extract the commit title and description to use as context for the closing comment
- Use this commit SHA as the reference — skip asking the user for one

If no matching commit is found, proceed to Step 5 and ask the user to provide one.

---

## Step 5 — Ask for a commit reference (optional)

Ask the user:

> Do you have a commit hash or short SHA to reference in the closing comment? (Leave blank to skip)

---

## Step 6 — Draft the closing comment

Based on the issue title and any context from the conversation, draft a short closing comment. The comment should:
- State that the issue has been resolved / implemented
- Briefly summarise what was done (1–3 sentences, inferred from the issue title and any context the user has provided)
- Reference the commit using the full SHA (get it with `git rev-parse <short-sha>` if needed)
  in the format: `Implemented in commit GrapeIsGrape/CommodityDashboard@<full-sha>`
- Be written in the past tense

Show the draft to the user. Example format:

> **Proposed closing comment:**
>
> Resolved: [summary of what was done]. [Implemented in commit `abc1234`.]

---

## Step 7 — Ask for explicit confirmation

Ask the user:

> Ready to:
> 1. Post the above comment on issue #N
> 2. Close issue #N — "[issue title]"
>
> Confirm? (yes / no)

**Do not proceed until the user says yes (or an equivalent affirmative).**

If the user says no, ask if they want to adjust the comment or cancel entirely.

---

## Step 8 — Post the comment

Use `mcp__github__add_issue_comment` with:
- `owner`: `GrapeIsGrape`
- `repo`: `CommodityDashboard`
- `issue_number`: the number
- `body`: the confirmed closing comment

---

## Step 9 — Close the issue

Use `mcp__github__issue_write` with:
- `owner`: `GrapeIsGrape`
- `repo`: `CommodityDashboard`
- `issue_number`: the number
- `state`: `closed`

---

## Step 10 — Confirm to the user

Report back:

> Issue #N — "[title]" has been closed.

---

## Notes

- Never skip the confirmation step (Step 7). This is a hard requirement.
- If the user provides a commit hash upfront in their invocation message, skip Steps 4 and 5.
- If a commit is found via git log, skip Step 5.
- If the user provides a closing comment upfront, use it verbatim (still show it for confirmation).
- Do not modify any other issues or repos.

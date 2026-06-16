# CommodityDashboard — Agent Team Workflow

How the autonomous team is wired. Read this to understand who does what and why the structure is shaped this way.

## The mental model (important)

There is **one long-running role and five one-shot roles**:

- **PM = the conductor, running in your main session.** When you invoke `/pm`, the main Claude loop *becomes* the PM. It persists across the conversation, owns the roadmap and all git/GitHub state, and is the only thing that can fan out to other agents.
- **BA / Developer / QA / Trader / Security-Auditor = one-shot sub-agents.** Each is spawned for a single task, does it, returns a structured report, and **terminates**. They are stateless — they remember nothing between calls.

This is forced by the platform, not a preference: **a sub-agent cannot spawn other sub-agents.** So the PM cannot itself be a spawned agent (it would be unable to start the others), and "Agent A consults Agent B" is never peer-to-peer chatter — the **PM** spawns the consulted agent, takes its answer, and hands it to the next agent. Every fan-out goes through the conductor.

Because specialists forget everything, **shared state must live in durable artifacts**:
- `docs/roadmap.md` — the PM's living plan (the team's shared brain)
- GitHub issues — the tickets and their acceptance criteria
- the repo + `CLAUDE.md` — the code and project facts

```
        ┌─────────────────────────── PM (main session, long-running) ───────────────────────────┐
        │  owns: docs/roadmap.md · git commit/push to main · CLAUDE.md · routing decisions        │
        └──┬──────────┬───────────────┬────────────────┬───────────────┬──────────────┬──────────┘
   spawns │   spawns  │        spawns │         spawns │        spawns │       spawns │
        ▼          ▼               ▼                ▼               ▼              ▼
   trader-uat   ba-analyst     developer      security-auditor   qa-verifier   trader-uat
   (CONSULT)    (files #N)     (implements,   (gate on the       (verify every  (UAT: use it
        │        │              uncommitted)   uncommitted diff)  criterion)     for real)
        └────────┴──── each returns a structured report, then dies; PM reads it and routes ───────┘
```

## The per-ticket cycle (what the PM runs)

1. **Select** next ticket — PM, from `docs/roadmap.md` + live issues + commits.
2. **Trader CONSULT** *(optional)* — financial/domain input for the BA.
3. **BA** files the ticket (directed or next-ticket), folding in any consult input.
4. **Developer** implements in the working tree (uncommitted), pytest green, migration locally verified. Reports blockers instead of guessing.
5. **Security-Auditor** on the uncommitted diff — CRITICAL/HIGH halts.
6. **QA verify** — re-runs pytest, checks every acceptance criterion; FAIL loops back to the Developer.
7. **Trader UAT** — uses the change as the real options-seller would; PASS, or NEW-REQUIREMENTS routed back to the BA (amend vs new ticket).
8. **PM commits + pushes** to `main` (auto-deploys on Railway; migrations apply via `etl/run.py` on redeploy — never run against prod by hand).
9. **PM updates** `docs/roadmap.md` (+ `CLAUDE.md` if stale) in the same commit.
10. **QA close** — posts closing comment, closes the issue (only after QA verify + UAT both passed and the push is done).
11. **Loop** — emit one-line summary, next ticket.

### Routing branches
- **Dev blocker (5b)** → PM re-plans `docs/roadmap.md`, BA amends/creates tickets, Dev moves to another ready ticket.
- **QA FAIL** → back to Developer with precise gaps, then re-audit.
- **Trader NEW-REQUIREMENTS** → BA decides amend-this-ticket (→ Developer) or file-new-ticket (→ later cycle).

## Hard stops (the only halts)
Security CRITICAL/HIGH · genuine user-only ambiguity · unfixable pytest · migration fails local verify · no work left · tooling failure. Everything else continues, including phase boundaries (by default).

## Why this team (and not a bigger one)
The work is mostly **serial** (schema → ETL → dashboard) and touches **shared files** (`migrations/`, `CLAUDE.md`, `config/*.yaml`), so extra parallel Developers would mostly idle or collide. The value here is **independent verification** — QA and Trader are separate from the Developer, which breaks the "grading your own homework" loop that makes a solo autonomous loop drift over time. That's what keeps quality from decaying across a long run; headcount wouldn't. Add a second Developer (with git-worktree isolation) only for a batch of provably-independent tickets, and a Designer only once Phase 4 has real UI surface.

## Relationship to the other skills
- `/pm` is the **team** conductor (this workflow). `/boss` is the **solo** autonomous loop (PM does every step itself). Same hard-stops; `/pm` adds independent QA + Trader gates and the consult path.
- The specialist agents are derived from the matching skills (`ba`, `implement`, `close-issue`) plus the `security-auditor` agent — those skills remain the source of truth for each step's detailed procedure.
- You can still run any skill solo (`/ba`, `/implement`, `/close-issue`) outside the loop for a single hand-driven step.

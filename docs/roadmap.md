# CommodityDashboard — Roadmap (PM blackboard)

This file is the **PM agent's living plan** — the shared "brain" the team reads and the PM writes. Sub-agents are stateless and forget everything between calls, so the *current* picture of what's done, what's next, and why lives **here on disk**, not in any agent's memory.

- **Authority:** `README.md` is the spec; `CLAUDE.md` is the working summary; this file tracks execution state. On conflict, README wins — flag the drift.
- **Who edits:** the **PM** updates this after each cycle (ticket done, phase advanced, new tickets surfaced). The BA/Dev/QA/Trader *read* it; they don't edit it.
- Keep it short and current. When it disagrees with the GitHub issues or the commits, the issues/commits are reality — reconcile and fix this file.

_Last reconciled: 2026-06-16 (#10 vol-indices GVZ/OVX shipped — QA✓ UAT✓; rank-window audit fix folded in; curve_shape is the last Phase 3 source)._

---

## Phase pointer

**Current position: Phase 3 IN PROGRESS** — Phase 2 complete (FRED #3, EIA #4, USDA #6, CFTC #7; scheduler deferred). Phase 3: **IV ETL → `iv_metrics` (#9) DONE** (`baf0ec6`, swappable `get_iv()`); **vol indices GVZ/OVX → `iv_metrics` (#10) DONE** (swappable `IndexHistoryProvider`, ~3y backfill, trailing-365d rank shared with #9).
**Next up: curve shape (contango/backwardation) → `curve_shape` — the last Phase 3 source.**

Open backlog: **#8** (add `schema_version` to dashboard `/health`) — tooling/observability, available but lower priority than the Phase 3 data work.

The PM loop crosses phase boundaries by default. If you want a human glance at the end of Phase 3 (before the Phase 4 dashboard work), tell the PM to stop at the phase boundary.

---

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 — Volatility data spike | de-risk IV via yfinance (no IBKR/paid feed for v1) | ✅ done 2026-06-14 |
| 1 — Foundation & schema | Compose scaffold (#1), data tables migration `0002` (#2) | ✅ done |
| 2 — Free-data ETL | FRED #3, EIA #4, USDA #6, CFTC #7 (idempotent, backfilled); **scheduler deferred** | ✅ done (sources) |
| 3 — Volatility & positioning ETL | IV → `iv_metrics` (#9 ✅), vol indices GVZ/OVX (#10 ✅), `curve_shape` (contango/backwardation) | ⏳ in progress |
| 4 — Dashboard (FastAPI) | four panels + macro sub-panel + empty sentiment panel; surface COT extremes, rich IV, backwardation flags | ▫️ not started |
| 5 — Polish & deploy | deploy Compose stack, release calendar, health checks/logging, redeploy docs | ▫️ not started |

---

## Candidate next tickets (Phase 3)

The PM refines these each cycle against live issues/commits before filing. Smallest-coherent-step first, matching the Phase-2 ETL pattern (config-driven, append-only, idempotent, per-source isolation, swappable interface).

1. ~~IV ETL via yfinance → `iv_metrics`~~ — **DONE (#9, `baf0ec6`)**, behind swappable `get_iv()`.
2. ~~Vol indices GVZ + OVX → `iv_metrics`~~ — **DONE (#10)**: `etl/sources/vol_indices.py`, swappable `IndexHistoryProvider`, ~3y backfill, trailing-365d rank shared with #9, VIX excluded (`ingest: false`), no migration.
3. **Curve shape (contango/backwardation) → `curve_shape`** (NEXT, last Phase 3 source) — flag the structure; note any multi-expiry futures-curve data that needs a paid feed/scraping rather than faking it.
4. **Scheduler wiring** (deferred from Phase 2) — swappable Compose-cron / Railway-cron / DSM Task Scheduler. May land here or in Phase 5.

Also open (non-Phase-3): **#8** add `schema_version` to dashboard `/health` — migration-observability tooling.

## Known deferrals / flagged-not-faked
- Metals warehouse stocks — not on EIA API (paid feed/scraping).
- WASDE supply/demand balance sheet — report files, not a queryable API.
- ISM PMIs (licensed) and ICE DXY — excluded; DXY proxied by FRED `DTWEXBGS`.
- Base metals COT (ALI/ZNC/NICKEL) — LME, no CFTC legacy report.

## Surfaced-but-not-yet-filed (PM appends here as Dev blockers / Trader UAT findings arrive)
- **Evaluate additional commodity vol indices for `iv_metrics`** (Trader UAT on #10) — esp. `^VXSLV` (silver/SLV); GVZ/OVX are the only CBOE *commodity* vol indices with durable Yahoo history, VXSLV has been intermittently discontinued. Follow-up should *gate on the index still publishing* (flag-not-fake), reusing the #10 config `indices` list (zero schema change). Not Phase-3-blocking.
- **Panel-D staleness flag for vol-index rows** (Trader UAT on #10) — render `snapshot_date` and flag when the latest GVZ/OVX close is stale (> N days, holiday/halt gap). Belongs with the Phase 4 dashboard ticket, not the ETL.

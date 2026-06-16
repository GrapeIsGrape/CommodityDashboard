# CommodityDashboard — Roadmap (PM blackboard)

This file is the **PM agent's living plan** — the shared "brain" the team reads and the PM writes. Sub-agents are stateless and forget everything between calls, so the *current* picture of what's done, what's next, and why lives **here on disk**, not in any agent's memory.

- **Authority:** `README.md` is the spec; `CLAUDE.md` is the working summary; this file tracks execution state. On conflict, README wins — flag the drift.
- **Who edits:** the **PM** updates this after each cycle (ticket done, phase advanced, new tickets surfaced). The BA/Dev/QA/Trader *read* it; they don't edit it.
- Keep it short and current. When it disagrees with the GitHub issues or the commits, the issues/commits are reality — reconcile and fix this file.

_Last reconciled: 2026-06-16 (seeded from CLAUDE.md §6 at creation)._

---

## Phase pointer

**Current position: Phase 2 COMPLETE** — all four free-data ETL sources landed (FRED #3, EIA #4, USDA #6, CFTC #7); scheduler wiring deferred.
**Next up: Phase 3 — Volatility & positioning ETL.**

The previous solo `boss` loop crossed phase boundaries automatically. The PM loop does too by default — but Phase 3 is the natural place for a human glance, since it introduces the IV snapshot-accrual logic the whole Volatility panel depends on.

---

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 — Volatility data spike | de-risk IV via yfinance (no IBKR/paid feed for v1) | ✅ done 2026-06-14 |
| 1 — Foundation & schema | Compose scaffold (#1), data tables migration `0002` (#2) | ✅ done |
| 2 — Free-data ETL | FRED #3, EIA #4, USDA #6, CFTC #7 (idempotent, backfilled); **scheduler deferred** | ✅ done (sources) |
| 3 — Volatility & positioning ETL | IV → `iv_metrics` (accrue IV rank/percentile from our own daily snapshots), `curve_shape` (contango/backwardation), OVX/GVZ/VIX vol indices | ⏳ next |
| 4 — Dashboard (FastAPI) | four panels + macro sub-panel + empty sentiment panel; surface COT extremes, rich IV, backwardation flags | ▫️ not started |
| 5 — Polish & deploy | deploy Compose stack, release calendar, health checks/logging, redeploy docs | ▫️ not started |

---

## Candidate next tickets (Phase 3)

The PM refines these each cycle against live issues/commits before filing. Smallest-coherent-step first, matching the Phase-2 ETL pattern (config-driven, append-only, idempotent, per-source isolation, swappable interface).

1. **IV ETL via yfinance option chains → `iv_metrics`**, behind the swappable `get_iv(symbol)` interface; IV pulled via optionable ETF proxies (GLD/SLV/USO/UNG…), idempotent upsert on the natural key. *(Trader consult: confirm proxy mapping + that IV rank/percentile must accrue from our own daily snapshots — Yahoo gives no IV history.)*
2. **Vol indices OVX/GVZ/VIX → `prices`** (or `iv_metrics` context), yfinance `^OVX`/`^GVZ`/`^VIX`.
3. **Curve shape (contango/backwardation) → `curve_shape`** — flag the structure; note any multi-expiry futures-curve data that needs a paid feed/scraping rather than faking it.
4. **Scheduler wiring** (deferred from Phase 2) — swappable Compose-cron / Railway-cron / DSM Task Scheduler. May land here or in Phase 5.

## Known deferrals / flagged-not-faked
- Metals warehouse stocks — not on EIA API (paid feed/scraping).
- WASDE supply/demand balance sheet — report files, not a queryable API.
- ISM PMIs (licensed) and ICE DXY — excluded; DXY proxied by FRED `DTWEXBGS`.
- Base metals COT (ALI/ZNC/NICKEL) — LME, no CFTC legacy report.

## Surfaced-but-not-yet-filed (PM appends here as Dev blockers / Trader UAT findings arrive)
- _(none yet)_

# CommodityDashboard — Roadmap (PM blackboard)

This file is the **PM agent's living plan** — the shared "brain" the team reads and the PM writes. Sub-agents are stateless and forget everything between calls, so the *current* picture of what's done, what's next, and why lives **here on disk**, not in any agent's memory.

- **Authority:** `README.md` is the spec; `CLAUDE.md` is the working summary; this file tracks execution state. On conflict, README wins — flag the drift.
- **Who edits:** the **PM** updates this after each cycle (ticket done, phase advanced, new tickets surfaced). The BA/Dev/QA/Trader *read* it; they don't edit it.
- Keep it short and current. When it disagrees with the GitHub issues or the commits, the issues/commits are reality — reconcile and fix this file.

_Last reconciled: 2026-06-19 (Phase 4 IN PROGRESS: #13 Panel D, #14 Panel C, #15 Panel A, #16 Panel A polish, #18 Panel B, #20 prices-ETL, and now **#21 macro-context sub-panel render** all shipped. #21 (`GET /panel/macro`) renders the `prices` trio TLT/VTI/QQQ as a compact cross-asset risk-regime strip — total-return adj_close headline + raw close tape, single neutral risk-on/off/de-risking regime line, VTI/QQQ drawdown-from-trailing-high, NYSE-daily staleness, reusing Panel A's pure helpers; no migration — SecAudit✓ (clean) QA✓ (all 12 ACs) UAT✓ (Trader confirms the risk-on/off composite + equity-drawdown fear-gauge are the genuine adds over Panel A). One trivial in-commit tidy: a "sell-candidate" CSS comment that leaked the word "sell" into served HTML was reworded for AC6 airtightness. Trader UAT non-blocking notes (backlog only): trailing-high is over the ~400-day query window (footnote honest); a pure rates-shock selloff labels "risk-on" by sign-only logic (Panel A owns that nuance). Open: #19 (Cushing tank-bottom, later). Next: Phase 4 sentiment placeholder panel (last Phase-4 render), or the deferred scheduler.)_

---

## Phase pointer

**Current position: Phase 4 IN PROGRESS (dashboard).** Phase 2 complete (FRED #3, EIA #4, USDA #6, CFTC #7; scheduler deferred). Phase 3 ETL sources complete (#9 IV, #10 vol indices, #11 curve, #12 anchoring). **Phase 4 done so far:** **Panel D (Volatility) → `GET /panel/d` (#13)** — Jinja2 foundation (base layout + reusable panel shell) + read-only `iv_metrics` view; conjunctive rich-highlight (`iv_rank≥0.70 AND iv_rv_spread>0`); cold-start `— (N/20)`; daily staleness; GVZ/OVX strip. **Panel C (Positioning & Flow) → `GET /panel/c` (#14)** — read-only `cot` + `curve_shape`; headline 3y COT index (156-wk net-spec percentile) + loud ≥80/≤20 crowding flags with directional seller inference; cold-start `— (accruing M/156)`; separate 5-card energy curve strip (loud backwardation, NULL `— (no curve)` ≠ flat); COT-correct weekly Tue→Fri staleness + holiday grace. Both no migration, both DB-error-isolated (no 500). **Panel A (Macro / Cross-Asset) → `GET /panel/a` (#15)** — read-only `macro_metrics`; five-bucket grouping (USD / real-rates+breakevens / realized-inflation / growth+labor / risk-regime) config-driven from new `freq`/`group` keys on `fred_series.yaml`; level+change every row (inflation indices headline YoY% from stored 12-mo-prior row); DFII10 shown directly (never recomputed); release-aware monthly/quarterly staleness (publication-lag model — freshest print never STALE); VIX as cross-asset regime, no IV-rank, separated from Panel D's GVZ/OVX; neutral coloring, no option-action language. No migration, DB-error-isolated.
**Next up: the rest of Phase 4 — the macro sub-panel (TLT/VTI/QQQ → `prices` — NOTE: `prices` has no ETL writer yet, so this needs a prices-ETL ticket first) and the empty sentiment placeholder — OR the deferred scheduler wiring. PM's call; the Panel D/C/A/B pattern (read-only panel module + Jinja2 template + pure testable logic + tests) is now well-established across all four data panels. The macro sub-panel is the natural next dashboard step but is GATED on a prices-ETL writer (yfinance, mirroring the IV/vol-index swappable-provider pattern) — so the coherent next ticket is either (a) the prices-ETL writer (unblocks the sub-panel, and also fills daily futures/spot `prices`), or (b) the deferred scheduler (smaller step; unblocks live IV-rank accrual — Panel D per-name rank stays `— (N/20)` until snapshots run daily). Sentiment placeholder is a thin always-empty panel that can land anytime.**

Open backlog: ~~**#8**~~ DONE · ~~**#12**~~ DONE · ~~**#13** (foundation + Panel D)~~ **DONE** · ~~**#14** (Panel C)~~ **DONE** · ~~**#15** (Panel A)~~ **DONE** · ~~**#16** (Panel A polish)~~ **DONE** · ~~**#18** (Panel B fundamentals/inventory)~~ **DONE** · ~~**#20** (prices-ETL writer + migration 0003)~~ **DONE** · ~~**#21** (macro-context sub-panel render → `GET /panel/macro`)~~ **DONE**. Open: **#19** (Panel B Cushing tank-bottom proximity — nice-to-have follow-up, later cycle). Next: the **empty sentiment placeholder panel** (last remaining Phase-4 render — thin always-empty panel, lands anytime) or the deferred scheduler (unblocks live IV-rank/COT-index accrual + fresh snapshots).

The PM loop crosses phase boundaries by default.

---

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 — Volatility data spike | de-risk IV via yfinance (no IBKR/paid feed for v1) | ✅ done 2026-06-14 |
| 1 — Foundation & schema | Compose scaffold (#1), data tables migration `0002` (#2) | ✅ done |
| 2 — Free-data ETL | FRED #3, EIA #4, USDA #6, CFTC #7 (idempotent, backfilled); **scheduler deferred** | ✅ done (sources) |
| 3 — Volatility & positioning ETL | IV → `iv_metrics` (#9 ✅), vol indices GVZ/OVX (#10 ✅), `curve_shape` (#11 ✅) | ✅ done (sources; scheduler deferred) |
| 4 — Dashboard (FastAPI) | four panels + macro sub-panel + empty sentiment panel; surface COT extremes, rich IV, backwardation flags | 🔄 in progress — Panel D (#13 ✅) + Panel C (#14 ✅) + Panel A (#15 ✅ + #16 polish) + Panel B (#18 ✅) + macro-context sub-panel (#21 ✅); only the empty sentiment placeholder pending |
| 5 — Polish & deploy | deploy Compose stack, release calendar, health checks/logging, redeploy docs | ▫️ not started |

---

## Candidate next tickets (Phase 3)

The PM refines these each cycle against live issues/commits before filing. Smallest-coherent-step first, matching the Phase-2 ETL pattern (config-driven, append-only, idempotent, per-source isolation, swappable interface).

1. ~~IV ETL via yfinance → `iv_metrics`~~ — **DONE (#9, `baf0ec6`)**, behind swappable `get_iv()`.
2. ~~Vol indices GVZ + OVX → `iv_metrics`~~ — **DONE (#10)**: `etl/sources/vol_indices.py`, swappable `IndexHistoryProvider`, ~3y backfill, trailing-365d rank shared with #9, VIX excluded (`ingest: false`), no migration.
3. ~~Curve shape (contango/backwardation) → `curve_shape`~~ — **DONE (#11)**: energy-only (CL/BZ/NG/RB/HO), front-vs-deferred annualized `slope_pct` + `structure` flag (0.5% deadband), swappable `CurveProvider`, honest-NULL, no migration. ETF-roll proxies rejected; metals/grains/softs + base-metals flagged-not-faked. Refinement tracked in #12.
4. **Scheduler wiring** (deferred from Phase 2/3) — swappable Compose-cron / Railway-cron / DSM Task Scheduler. The remaining Phase 3 loose end; may land before or alongside Phase 4.

Also open (non-Phase-3): **#8** add `schema_version` to dashboard `/health` — migration-observability tooling.

## Known deferrals / flagged-not-faked
- Metals warehouse stocks — not on EIA API (paid feed/scraping).
- WASDE supply/demand balance sheet — report files, not a queryable API.
- ISM PMIs (licensed) and ICE DXY — excluded; DXY proxied by FRED `DTWEXBGS`.
- Base metals COT (ALI/ZNC/NICKEL) — LME, no CFTC legacy report.

## Recently shipped (out of phase)
- ~~**#17 — dashboard crash-loop on Railway (`ModuleNotFoundError: No module named 'etl'`)**~~ — **DONE (2026-06-18, `468f320`)**: `panel_d.py` imported `_MIN_HISTORY_OBS` from `etl.sources.iv`, but the dashboard image ships no `etl/`. Relocated the constant to new stdlib-only `common/constants.py` (single source of truth); `iv.py` re-exports it, `panel_d.py` imports from `common.constants`; static-scan regression guard (`test_no_dashboard_module_imports_etl`) forbids any future dashboard→etl import. No migration/dep/env.

## Surfaced-but-not-yet-filed (PM appends here as Dev blockers / Trader UAT findings arrive)
- **Evaluate additional commodity vol indices for `iv_metrics`** (Trader UAT on #10) — esp. `^VXSLV` (silver/SLV); GVZ/OVX are the only CBOE *commodity* vol indices with durable Yahoo history, VXSLV has been intermittently discontinued. Follow-up should *gate on the index still publishing* (flag-not-fake), reusing the #10 config `indices` list (zero schema change). Not Phase-3-blocking.
- **Panel-D staleness flag for vol-index rows** (Trader UAT on #10) — render `snapshot_date` and flag when the latest GVZ/OVX close is stale (> N days, holiday/halt gap). Belongs with the Phase 4 dashboard ticket, not the ETL.
- ~~**#12 — curve-shape deferred-gap anchoring**~~ — **DONE**: config-driven per-contract `front_lead_months`/`roll_day` realized-front expiry rules; deferred ticker + slope denominator share one realized-front anchor so magnitude is correctly scaled near rolls. Residual ±1-day roll-boundary → honest-NULL.
- **Panel-C curve magnitude caveat near rolls** (Trader UAT on #11) — when Panel C renders, caveat the `slope_pct` magnitude in roll week (tie to #12); the `structure` sign flag needs no caveat. Phase 4 rendering concern.
- **Schedule the IV / vol-index / curve snapshots during/after the relevant session** (Trader UAT on #9/#10/#11) — off-hours/stale legs store NULL; the deferred scheduler ticket should run these when fresh settles exist.
- **Panel D secondary sort by `iv_rv_spread DESC during cold-start accrual** (Trader UAT on #13, nice-to-have) — while every per-name `iv_rank` is NULL (~20-session warm-up), `iv_rank DESC NULLS LAST` collapses all names into one bucket so the table is unordered; a secondary sort on IV−RV would make it useful sooner. Polish, not blocking — the GVZ/OVX strip carries the decision in that window.
- **Panel D `— (N/20)` countdown is ~1 session imprecise** (Trader UAT on #13, cosmetic) — the panel counts non-null `atm_iv` incl. today while `iv.py` lights rank at 19 priors + today, so the displayed ETA can be off by one session. No effect on any tradeable number; fix only if it confuses.
- **US market-holiday table in Panel D staleness is fixed 2025–2026** (QA/UAT note on #13) — `panel_d.py` `_US_MARKET_HOLIDAYS` must be extended as years roll forward; an unrecorded future holiday risks at most one false STALE badge, never bad data. Same applies to Panel C's `_US_FEDERAL_HOLIDAYS` table (#14).
- **Panel D × Panel C cross-reference: rich-IV ∧ COT-extreme = highest-conviction "sell the opposite side"** (Trader consult+UAT on #14, nice-to-have) — a name that is both rich IV (Panel D) and at a crowded-spec extreme (Panel C) is the strongest sell-the-other-side setup; surface a cross-panel flag/note once both panels exist. Deferred from #14, worth a dedicated ticket.
- **Panel C `slope_pct` percentile vs own 1y history + seasonality self-calibration** (Trader consult on #14, future ticket) — make the curve backwardation flag self-calibrating like the COT index (so seasonally-normal energy backwardation isn't read as a signal); needs a per-name seasonal/own-history model. Separate ticket.
- **Panel C Friday-morning intraday staleness over-eagerness** (QA note on #14, cosmetic) — `expected_cot_report_date` treats the whole Friday as past the ~15:30 ET release, so a Friday-pre-release read could badge the still-current row STALE for a few hours; day-granularity is acceptable for v1, holiday grace absorbs most. Fix only if intraday staleness matters.
- **Panel A nice-to-have polish** (Trader UAT on #15, non-blocking) — (a) render DTWEXBGS ~1m/~3m change as a **percent** not index points (a broad-index move reads naturally in %, maps to commodity headwind/tailwind), keeping rate series in pp and CPI-style indices absolute; (b) optional **VIX calm/normal/stressed band** tag from level (descriptive context, no option verbs, AC#8-clean); (c) USD-trend / real-rate-trend one-line context clauses with the commodity-linkage gloss. **Being filed as a single polish follow-up ticket.** Panel A's blocking UAT bug (release-aware staleness) was fixed in-ticket before close.
- ~~**`prices` table has no ETL writer**~~ **RESOLVED (#20)** — prices-ETL now writes TLT/VTI/QQQ (raw close + total-return adj_close, migration 0003). The macro sub-panel render can proceed. Commodity-universe daily prices remain deferred (redundant with curve_shape/iv_metrics, no v1 consumer) — add when a consumer needs it.
- **Verify real `inventories` backfill depth** (Trader UAT NR-3 on #18, ops check not code) — Panel B's weekly tight/loose percentile only leaves the `— (accruing M/N)` state once ~9 seasonal comparables (~3 prior same-season years) are stored; the EIA/USDA config says `observation_start: 2005` but the percentile column is only useful in production if the ETL actually backfilled that deep. Confirm stored history depth per series; if shallow, the percentile column reads accruing and only the always-honest WoW build/draw + Δ-vs-year-ago carry the panel (by design). No code change — a data/ops check (run it when the scheduler/backfill ETL is live).
- **Panel B same-week-of-year seasonal band + 5-yr envelope** (deferred from #18, flag-not-fake) — the polished EIA-style seasonal min/max band is the percentile column's end-state; gated on ≥5 yrs stored weekly history per series. Its own later ticket. Also deferred from #18: grain stocks-to-use (needs WASDE use denominator), and the consensus/expectations "surprise" (paid/scraped feed).

# CommodityDashboard — Roadmap (PM blackboard)

This file is the **PM agent's living plan** — the shared "brain" the team reads and the PM writes. Sub-agents are stateless and forget everything between calls, so the *current* picture of what's done, what's next, and why lives **here on disk**, not in any agent's memory.

- **Authority:** `README.md` is the spec; `CLAUDE.md` is the working summary; this file tracks execution state. On conflict, README wins — flag the drift.
- **Who edits:** the **PM** updates this after each cycle (ticket done, phase advanced, new tickets surfaced). The BA/Dev/QA/Trader *read* it; they don't edit it.
- Keep it short and current. When it disagrees with the GitHub issues or the commits, the issues/commits are reality — reconcile and fix this file.

_Last reconciled: 2026-06-17 (Phase 4 STARTED: #13 dashboard foundation + Panel D (Volatility) shipped — SecAudit✓ (1 HIGH found+fixed: route error isolation) QA✓ UAT✓. Next: Phase 4 Panels A/B/C + macro sub-panel + sentiment placeholder, or the deferred scheduler.)_

---

## Phase pointer

**Current position: Phase 4 STARTED (dashboard).** Phase 2 complete (FRED #3, EIA #4, USDA #6, CFTC #7; scheduler deferred). Phase 3 ETL sources complete (#9 IV, #10 vol indices, #11 curve, #12 anchoring). **Phase 4: dashboard foundation + Panel D (Volatility) → `GET /panel/d` DONE (#13)** — Jinja2 server-side templating + base layout + reusable panel shell; read-only `iv_metrics` view; conjunctive rich-highlight (`iv_rank≥0.70 AND iv_rv_spread>0`); honest cold-start `— (N/20)`; per-row staleness vs last expected session; GVZ/OVX regime strip; DB-error isolation (no 500). No migration.
**Next up: the rest of Phase 4 — Panels A (macro), B (fundamentals/inventory), C (positioning+curve), the macro sub-panel (TLT/VTI/QQQ), and the empty sentiment placeholder — OR the deferred scheduler wiring. PM's call; Panel D set the pattern (read-only panel module + Jinja2 template + tests). Scheduler is the smaller step and unblocks live IV-rank accrual (per-name rank stays `— (N/20)` until snapshots run daily).**

Open backlog: ~~**#8**~~ DONE · ~~**#12**~~ DONE · ~~**#13** (dashboard foundation + Panel D)~~ **DONE**. **Backlog clear** — next work is another Phase 4 panel or the deferred scheduler.

The PM loop crosses phase boundaries by default.

---

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 0 — Volatility data spike | de-risk IV via yfinance (no IBKR/paid feed for v1) | ✅ done 2026-06-14 |
| 1 — Foundation & schema | Compose scaffold (#1), data tables migration `0002` (#2) | ✅ done |
| 2 — Free-data ETL | FRED #3, EIA #4, USDA #6, CFTC #7 (idempotent, backfilled); **scheduler deferred** | ✅ done (sources) |
| 3 — Volatility & positioning ETL | IV → `iv_metrics` (#9 ✅), vol indices GVZ/OVX (#10 ✅), `curve_shape` (#11 ✅) | ✅ done (sources; scheduler deferred) |
| 4 — Dashboard (FastAPI) | four panels + macro sub-panel + empty sentiment panel; surface COT extremes, rich IV, backwardation flags | 🔄 started — foundation + Panel D (#13 ✅); Panels A/B/C + sub-panel + sentiment pending |
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

## Surfaced-but-not-yet-filed (PM appends here as Dev blockers / Trader UAT findings arrive)
- **Evaluate additional commodity vol indices for `iv_metrics`** (Trader UAT on #10) — esp. `^VXSLV` (silver/SLV); GVZ/OVX are the only CBOE *commodity* vol indices with durable Yahoo history, VXSLV has been intermittently discontinued. Follow-up should *gate on the index still publishing* (flag-not-fake), reusing the #10 config `indices` list (zero schema change). Not Phase-3-blocking.
- **Panel-D staleness flag for vol-index rows** (Trader UAT on #10) — render `snapshot_date` and flag when the latest GVZ/OVX close is stale (> N days, holiday/halt gap). Belongs with the Phase 4 dashboard ticket, not the ETL.
- ~~**#12 — curve-shape deferred-gap anchoring**~~ — **DONE**: config-driven per-contract `front_lead_months`/`roll_day` realized-front expiry rules; deferred ticker + slope denominator share one realized-front anchor so magnitude is correctly scaled near rolls. Residual ±1-day roll-boundary → honest-NULL.
- **Panel-C curve magnitude caveat near rolls** (Trader UAT on #11) — when Panel C renders, caveat the `slope_pct` magnitude in roll week (tie to #12); the `structure` sign flag needs no caveat. Phase 4 rendering concern.
- **Schedule the IV / vol-index / curve snapshots during/after the relevant session** (Trader UAT on #9/#10/#11) — off-hours/stale legs store NULL; the deferred scheduler ticket should run these when fresh settles exist.
- **Panel D secondary sort by `iv_rv_spread DESC during cold-start accrual** (Trader UAT on #13, nice-to-have) — while every per-name `iv_rank` is NULL (~20-session warm-up), `iv_rank DESC NULLS LAST` collapses all names into one bucket so the table is unordered; a secondary sort on IV−RV would make it useful sooner. Polish, not blocking — the GVZ/OVX strip carries the decision in that window.
- **Panel D `— (N/20)` countdown is ~1 session imprecise** (Trader UAT on #13, cosmetic) — the panel counts non-null `atm_iv` incl. today while `iv.py` lights rank at 19 priors + today, so the displayed ETA can be off by one session. No effect on any tradeable number; fix only if it confuses.
- **US market-holiday table in Panel D staleness is fixed 2025–2026** (QA/UAT note on #13) — `panel_d.py` `_US_MARKET_HOLIDAYS` must be extended as years roll forward; an unrecorded future holiday risks at most one false STALE badge, never bad data.

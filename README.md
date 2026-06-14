# CommodityDashboard

A personal, single-user dashboard that aggregates the market data — and, later, news sentiment — that drives the commodities I trade options on. It helps me decide **when premium is rich** and **which underlyings are worth selling**.

> **This is a monitoring tool, not an execution tool.** It never places trades or moves money. It only reads market data, stores it, and displays it.

---

## 1. Context & Goal

I'm an options seller. I sell premium — bull put spreads, credit ratio spreads, short strangles, iron condors — at roughly **USD 500 premium per position** with a **200% stop-loss rule**. I also run a turtle trend-following system.

I want one place that aggregates everything that moves the commodities I sell options on, so I can spot rich premium and good underlyings at a glance.

### Design philosophy: separate deterministic data from probabilistic AI

Market data and AI analysis have different update cadences, failure modes, and costs — so they get **one shared database with two independent writers**:

| Writer | Built | Responsibility |
| --- | --- | --- |
| **Writer 1** (this project) | Now | ETL scripts pull pure market data into the DB on a schedule. |
| **Writer 2** (separate project) | Later | A scheduled task pulls news, runs LLM sentiment analysis, writes to its own tables in the same DB. |

The dashboard reads both and renders them side by side.

---

## 2. Architecture Decisions

These are already decided — follow them.

- **Stack:** Python + FastAPI (dashboard/API), Python scripts for ETL, PostgreSQL as the single shared database.
- **Portability:** package everything as Docker Compose (`postgres`, `etl`, `dashboard` services) so it deploys to **Railway** or a **Synology NAS** with no code changes. Keep all environment-specific config in `.env` / environment variables — never hardcode hosts, ports, credentials, or API keys.
- **1 DB, 2 writers:** data ETL and the future sentiment task both write to the same Postgres but to **separate tables**. Design the schema so sentiment tables can be added later without touching the data tables.
- **Store history, not just latest:** every table is time-stamped and append-friendly so any signal can be backtested. **Never overwrite — insert new dated rows.**
- **Idempotent ETL:** re-running a job for the same date must not create duplicates — upsert on natural keys like `(symbol, metric, date)`.
- **Secrets via env vars only:** API keys in `.env` (git-ignored), with a committed `.env.example` listing the required keys.
- **Git repo with schema migrations** (Alembic or plain SQL files) so it can be redeployed fast after any host/DSM update.

**Formatting conventions:** dates `YYYY-MM-DD` · currency USD · numbers with thousands separators in display.

---

## 3. Scope — Commodities (v1)

Cover these underlyings, grouped into dashboard panels by complex. **Make the symbol list a config file (YAML/JSON), not hardcoded**, so underlyings can be added or removed without code changes.

| Complex | Underlyings |
| --- | --- |
| **Precious metals** | Gold (GC / GLD), Silver (SI / SLV), Platinum (PL), Palladium (PA) |
| **Base / industrial metals** | Copper (HG), Aluminum, Zinc, Nickel |
| **Energy** | WTI Crude (CL), Brent (BZ), Natural Gas (NG), RBOB Gasoline (RB), Heating Oil (HO) |
| **Grains & oilseeds** | Corn (ZC), Soybeans (ZS), Soybean Meal (ZM), Soybean Oil (ZL), Wheat (ZW / KE / MW), Rice (ZR), Oats (ZO) |
| **Softs** | Coffee (KC), Sugar (SB), Cocoa (CC), Cotton (CT), Orange Juice (OJ), Lumber (LBR) |
| **Livestock** | Live Cattle (LE), Feeder Cattle (GF), Lean Hogs (HE) |
| **Macro context** | TLT (bonds), VTI, QQQ — shown as a macro-context panel, *not* as commodities |

---

## 4. Data Required — the "Pure Data" (Writer 1)

The dashboard is organized around **four panels**. Prefer free sources with public APIs; **flag anything that needs a paid feed.**

### Panel A — Macro / Cross-Asset (drives everything)

- **US Dollar Index (DXY)** — single most important cross-commodity driver, inverse to most commodities
- **Rates:** Fed funds rate + real yields (TIPS / 10Y real) — the key gold driver
- **Inflation:** CPI, PCE, PPI, and inflation breakevens
- **Employment:** nonfarm payrolls, unemployment rate, initial jobless claims
- **Growth:** GDP, ISM/PMI (US, China, Eurozone manufacturing — China PMI matters most for metals & oil)
- **Risk sentiment:** VIX, equity index direction

> **Source:** FRED API (free, API key) covers DXY, rates, CPI/PCE/PPI, breakevens, unemployment, GDP. PMIs partly via FRED / public releases.

### Panel B — Per-Commodity Fundamentals (supply/demand + inventories)

- **Inventory / storage:** EIA weekly petroleum & natural-gas storage; USDA WASDE (monthly) and crop-progress reports for grains; warehouse stocks (COMEX/LME/Shanghai) for metals
- **Production:** OPEC+ output & decisions, US rig count (Baker Hughes), mine supply
- **Demand proxies:** refinery utilization, Chinese imports, auto production (for platinum/palladium)

> **Source:** EIA API (free, key), USDA NASS / WASDE (free), Baker Hughes (public). ⚠️ Metals warehouse stocks may need scraping or a paid feed — **flag it.**

### Panel C — Positioning & Flow

- **CFTC Commitment of Traders (COT)** — weekly (Fri, for Tue data). Best free positioning signal. Build a dedicated panel; flag extremes (specs crowded long/short) for contrarian setups.
- **ETF holdings** (GLD/SLV tonnage), open interest changes
- **Futures curve shape** — contango vs. backwardation per commodity. **Critical:** backwardation = tight supply (bullish), contango = oversupply; also determines the roll yield that silently erodes futures/ETF returns.

> **Source:** CFTC public data (free, downloadable/API). ⚠️ Curve shape needs multi-expiry futures prices (may need a market-data provider).

### Panel D — Volatility (most important for option selling — this is where decisions live)

- **Implied volatility + IV rank / IV percentile** per underlying — tells me when premium is rich vs. cheap
- **Published vol indices:** OVX (crude), GVZ (gold), VIX
- **Realized/historical vol and the IV − RV spread** — my actual edge as a premium seller
- **Seasonality overlays:** nat gas (winter), grains (planting/growing), gasoline (summer)

> ⚠️ **LONG POLE / RISK:** IV and option-chain data are *not* free from a government API. Likely from my Interactive Brokers account (IBKR API) or a paid feed. **Validate this source FIRST (Phase 0)** before building anything around it — if it's hard, it changes the plan.
>
> ✅ **RESOLVED (Phase 0, 2026-06-14):** **yfinance** (Yahoo, free, no auth) returns full option chains *with* per-contract implied vol, plus `^VIX`/`^GVZ`/`^OVX` and price history for realized vol. No IBKR or paid feed needed for v1. Two consequences: IV is sourced via **optionable ETF proxies** (GLD/SLV/USO/UNG…), not the futures symbols; and **IV rank/percentile must be built from our own daily snapshots** since Yahoo gives no IV history. Keep the source behind a swappable interface for an IBKR upgrade later. See Phase 0 in §6.

### Supporting data

Daily futures/spot prices and an **economic-release calendar** (which report drops when, and which commodities it moves) to anchor all panels to dates.

---

## 5. Sentiment (Writer 2 — NOT built here, just leave room)

A separate scheduled task will later pull news, run LLM analysis (topic heat, per-commodity tone, policy/geopolitics affecting supply), and write to its own sentiment tables in the same Postgres. For **this** project:

- **Design the schema** with placeholder `sentiment_*` tables: store raw inputs (headlines, source URLs, timestamps) **and the model's reasoning** — not just a single score — so the signal is auditable and backtestable.
- **Add a sentiment panel** to the dashboard that renders from those tables (empty until Writer 2 exists).
- **Treat news sentiment as supplementary**, not the centerpiece — COT, curve shape, and IV rank are more reliable quantitative sentiment.

---

## 6. Phased Build Plan

Do these in order. **Stop after each phase for review.**

### Phase 0 — Spike the long pole (volatility data) — ✅ DONE (2026-06-14)
Prove I can pull implied vol / option-chain data (likely IBKR API) with a throwaway script that fetches IV for one symbol (e.g. SLV) and prints it. If blocked or paid, surface options before designing around it. **Do not build the full app until this is answered.**

**Result: de-risked — no IBKR or paid feed needed for v1.** A throwaway spike ([`spike_iv.py`](spike_iv.py)) proved **yfinance** (Yahoo, no API key, no auth) delivers everything Panel D needs. Live SLV pull (spot $61.29):

- **Option chain + per-contract implied vol** — ✅ 25 expirations, 48/48 ATM calls returned non-zero IV (ATM ≈ 50–54%).
- **Published vol indices** — ✅ `^VIX` 17.68, `^GVZ` (gold) 26.85, `^OVX` (crude) 54.10.
- **Realized vol** (for the IV−RV spread) — ✅ 30-day RV 58.5%, ~6mo price history available for backfill.

Caveats that shape Phases 1/3 (not blockers):

1. **yfinance is unofficial** (scrapes Yahoo; can rate-limit/break). Acceptable for a personal single-user tool, but the IV source must sit behind a clean interface (`get_iv(symbol)`) so **IBKR can swap in later** without touching the rest of the app.
2. **No historical IV from Yahoo** — only today's chain snapshot. So **IV rank / IV percentile can't be computed on day one**; they require accumulating a daily ATM-IV series ourselves (fits the append-only design). The published indices (`^GVZ`/`^OVX`/`^VIX`, also on FRED) have long history and can seed rank for the commodities they cover.
3. **Futures symbols (GC, CL) have no Yahoo option chain** — IV comes via the **optionable ETF proxy** (GLD, SLV, USO, UNG…). The symbol config (Phase 1) needs a commodity → optionable-proxy mapping.

### Phase 1 — Foundation & schema
Repo scaffold, Docker Compose (`postgres` + placeholder `etl` + `dashboard`), `.env.example`, config file for the symbol list. Postgres schema + migrations for: `prices`, `macro_metrics`, `inventories`, `cot`, `iv_metrics`, `curve_shape`, and placeholder `sentiment_*` tables — all time-stamped with idempotent upserts.

### Phase 2 — Free-data ETL (easiest reliable win)
ETL jobs for FRED (macro), EIA (energy inventories), USDA (grains/WASDE), CFTC (COT). Idempotent and scheduled (Compose cron / Railway cron / DSM Task Scheduler — keep the scheduler swappable). Backfill history where the APIs allow.

### Phase 3 — Volatility & positioning ETL
Wire in the Phase-0 IV source → `iv_metrics` (IV, IV rank/percentile, realized vol, IV−RV spread). Add curve shape (contango/backwardation) and OVX/GVZ/VIX.

### Phase 4 — Dashboard (FastAPI)
Read-only dashboard with the four panels (Macro, Fundamentals/Inventory, Positioning, Volatility) + a macro-context sub-panel for TLT/VTI/QQQ + empty sentiment panel. Start with server-rendered static views; add history scrubbing / filtering only once panels prove useful. **Highlight:** COT extremes, rich IV (high IV rank), backwardation flags.

### Phase 5 — Polish & deploy
Deploy the Compose stack to the chosen host (Railway or Synology). Add an economic-release calendar view. Add health checks / logging so a failed ETL run is visible. Document redeploy steps.

> *Later, separate project:* Writer 2 sentiment task writing to the placeholder tables.

---

## 7. Engineering Principles

Keep applying these throughout:

- **Append-only, time-stamped, idempotent ETL** — never overwrite, no duplicate rows on re-run.
- **Config-driven symbol list** — no hardcoded hosts, keys, or symbols.
- **Secrets in env vars** — `.env.example` committed, `.env` git-ignored.
- **Migrations for every schema change.**
- **Each ETL source is its own module** with clear error handling and logging — one failing source must not break the others or the dashboard.
- **Prefer free public APIs** (FRED, EIA, USDA, CFTC); clearly flag any source that needs a paid feed or scraping.
- **The dashboard is read-only** — it never executes trades or moves money.

---

## 8. Open Questions

To answer as we go:

1. ~~**IV data source (Phase 0):** IBKR API vs. a paid market-data provider — confirm after the spike.~~ **ANSWERED (2026-06-14):** start on **yfinance** (free, no auth — proven in Phase 0), keep IBKR as a pluggable higher-fidelity fallback. IV pulled via optionable ETF proxies; IV rank accrued from our own daily snapshots.
2. **Scheduler:** Railway cron vs. Synology DSM Task Scheduler vs. in-container cron — pick once host is final.
3. **Metals warehouse stocks & multi-expiry futures** (for curve shape): acceptable to defer if no free source?
4. **History depth:** how far back to backfill each series for backtesting?

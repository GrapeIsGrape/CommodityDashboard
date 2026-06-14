# CLAUDE.md — CommodityDashboard

Project context for Claude Code. Read this before working on any ticket or change.
The authoritative spec is [README.md](README.md); this file is the working summary that grounds the skills. When they conflict, the README wins — and flag the drift.

---

## 1. What this is

A **personal, single-user** dashboard that aggregates the market data driving the commodities the owner sells options on, to spot **when premium is rich** and **which underlyings are worth selling**.

- **Read-only monitoring tool.** It never places trades or moves money — it reads market data, stores it, and displays it. Any change that writes orders or touches a broker is out of scope.
- **Single user.** There is no auth, no multi-tenancy, no per-user data scoping. Do not add login or `user_id` columns.

### 1 DB, 2 writers
One shared Postgres, two independent writers to **separate tables**:
- **Writer 1 (this project, now):** ETL scripts pull pure market data on a schedule.
- **Writer 2 (separate project, later):** scheduled LLM news-sentiment task writing to its own `sentiment_*` tables. Not built here — only leave room (placeholder schema + empty dashboard panel).

---

## 2. Tech stack

- **Language:** Python
- **Dashboard/API:** FastAPI (server-rendered, read-only)
- **ETL:** standalone Python scripts/modules, one module per data source
- **DB:** PostgreSQL (single shared database)
- **Packaging:** Docker Compose — services `postgres`, `etl`, `dashboard`
- **Deploy targets:** Railway **or** Synology NAS, no code changes between them
- **Migrations:** Alembic (decided in #1) — config-as-code in `migrations/`, DB URL built from env in `env.py`
- **Scheduler:** swappable (Compose cron / Railway cron / DSM Task Scheduler) — not yet chosen

External data sources: **FRED** (macro), **EIA** (energy inventories), **USDA NASS/WASDE** (grains), **CFTC** (COT positioning), **yfinance** (prices, option-chain IV, `^VIX`/`^GVZ`/`^OVX`). All free. Flag anything needing a paid feed or scraping (metals warehouse stocks, multi-expiry futures curves).

---

## 3. Architecture decisions (already decided — follow them)

- **Portability:** all environment-specific config in `.env` / env vars. Never hardcode hosts, ports, credentials, or API keys.
- **Separate tables per writer:** sentiment tables must be addable later without touching the data tables.
- **Store history, not just latest:** every table is time-stamped and append-friendly. **Never overwrite — insert new dated rows.**
- **Idempotent ETL:** re-running a job for the same date must not create duplicates — upsert on natural keys like `(symbol, metric, date)`.
- **Secrets via env only:** keys in `.env` (git-ignored); a committed `.env.example` lists required keys.
- **Migrations for every schema change.**
- **Config-driven symbol list:** the underlyings live in a YAML/JSON config, not hardcoded — add/remove without code changes. Config also holds the **commodity → optionable-ETF-proxy** mapping (IV comes via GLD/SLV/USO/UNG…, not futures symbols).

**Formatting conventions:** dates `YYYY-MM-DD` · currency USD · display numbers with thousands separators.

---

## 4. Engineering principles (apply throughout)

- **Append-only, time-stamped, idempotent ETL** — never overwrite, no duplicate rows on re-run.
- **Config-driven** — no hardcoded hosts, keys, or symbols.
- **Secrets in env vars** — `.env.example` committed, `.env` git-ignored.
- **Migrations for every schema change.**
- **Each ETL source is its own module** with its own error handling and logging — one failing source must not break the others or the dashboard.
- **Prefer free public APIs**; clearly flag any source needing a paid feed or scraping.
- **The dashboard is read-only** — it never executes trades or moves money.
- Keep the IV source behind a clean swappable interface (e.g. `get_iv(symbol)`) so IBKR can replace yfinance later without touching the rest of the app.

---

## 5. Dashboard panels & key tables

The dashboard is organized into four panels plus a macro-context sub-panel and an (empty) sentiment panel.

| Panel | Content | Primary source | Table(s) |
|---|---|---|---|
| **A — Macro / Cross-Asset** | DXY, rates/real yields, CPI/PCE/PPI/breakevens, employment, GDP, PMIs, VIX | FRED | `macro_metrics` |
| **B — Fundamentals / Inventory** | EIA petroleum & nat-gas storage, USDA WASDE/crop progress, production, demand proxies | EIA, USDA | `inventories` |
| **C — Positioning & Flow** | CFTC COT (flag specs crowded long/short), ETF holdings, futures curve shape (contango/backwardation) | CFTC | `cot`, `curve_shape` |
| **D — Volatility** *(where decisions live)* | IV + IV rank/percentile per underlying, OVX/GVZ/VIX, realized vol, IV−RV spread, seasonality | yfinance | `iv_metrics` |
| Macro-context sub-panel | TLT, VTI, QQQ — context, **not** commodities | yfinance | `prices` |
| Sentiment (placeholder) | Empty until Writer 2 exists; store headlines, URLs, timestamps **and model reasoning** | — | `sentiment_*` |

Daily futures/spot prices → `prices`. An economic-release calendar anchors panels to dates.

**Dashboard highlights to surface:** COT extremes, rich IV (high IV rank), backwardation flags.

### Scope — commodities (v1)
Precious metals (GC/GLD, SI/SLV, PL, PA), base metals (HG, Aluminum, Zinc, Nickel), energy (CL, BZ, NG, RB, HO), grains/oilseeds (ZC, ZS, ZM, ZL, ZW/KE/MW, ZR, ZO), softs (KC, SB, CC, CT, OJ, LBR), livestock (LE, GF, HE). Macro context: TLT, VTI, QQQ. The canonical list lives in the symbol config, not here.

---

## 6. Build status & phased plan

**Stop after each phase for review.** Current position: **Phase 0 done; Phase 1 scaffold done (#1) — repo skeleton runs on Railway; real table schemas still pending (#2).**

- **Phase 0 — Volatility data spike — ✅ DONE (2026-06-14).** De-risked: **yfinance** delivers option-chain IV + vol indices + price history, **no IBKR or paid feed needed for v1**. Consequences: IV via optionable ETF proxies (not futures symbols); IV rank/percentile must be accrued from our own daily snapshots (Yahoo gives no IV history); keep the source behind a swappable interface. Throwaway proof: [spike_iv.py](spike_iv.py).
- **Phase 1 — Foundation & schema.** Scaffold ✅ DONE (#1, 2026-06-14): Docker Compose (`postgres`/`etl`/`dashboard`), `.env.example`, symbol config, Alembic + empty `0001_baseline`, FastAPI boot page + `/health`; deployed on Railway. **Still pending (#2):** migrations for `prices`, `macro_metrics`, `inventories`, `cot`, `iv_metrics`, `curve_shape`, placeholder `sentiment_*`.
- **Phase 2 — Free-data ETL:** FRED, EIA, USDA, CFTC. Idempotent, scheduled, backfilled.
- **Phase 3 — Volatility & positioning ETL:** wire IV → `iv_metrics`, add curve shape and OVX/GVZ/VIX.
- **Phase 4 — Dashboard (FastAPI):** four panels + macro sub-panel + empty sentiment panel.
- **Phase 5 — Polish & deploy:** deploy Compose stack, release calendar, health checks/logging, redeploy docs.

---

## 7. Repo & workflow

- **GitHub:** `GrapeIsGrape/CommodityDashboard` (the issue-management skills target this repo).
- **Workflow:** ticket-driven — `ba` (spec the ticket) → `implement` (build it) → `close-issue` (verify & close). `enrich-ticket`, `debug`, `list-issues` support the loop.
- **Repository structure:**
  - `docker-compose.yml` — `postgres` / `etl` / `dashboard`, Postgres on a named `pgdata` volume
  - `.env.example` — every env var (DB host/port/name/user/password, `DASHBOARD_PORT`, FRED/EIA/USDA/CFTC key placeholders); `.env` git-ignored
  - `config/symbols.yaml` — v1 commodity universe + commodity→optionable-ETF-proxy mapping + macro-context & vol-index tickers (the canonical symbol list)
  - `common/config.py` — shared: `get_database_url()` (env→SQLAlchemy URL) + `load_symbols()`
  - `dashboard/` — FastAPI app (`main.py`: `/` boot page, `/health` Postgres check), `Dockerfile`, `requirements.txt`
  - `etl/` — `run.py` (entrypoint: applies migrations then idles — no scheduler yet), `sources/` (one module per source, Phase 2+), `Dockerfile`, `requirements.txt`
  - `migrations/` — Alembic: `alembic.ini`, `env.py`, `script.py.mako`, `versions/0001_baseline.py` (empty baseline)
  - `tests/` — pytest (`test_config.py`)
- **Deployment (Railway):** GitHub repo backs three services — `Postgres` (managed), and `dashboard` + `etl` both built from the **same repo** with Builder=Dockerfile and Dockerfile Path `dashboard/Dockerfile` / `etl/Dockerfile`. Set 5 vars on each code service referencing the DB: `POSTGRES_HOST=${{Postgres.PGHOST}}`, `PORT`/`DB`/`USER`/`PASSWORD` likewise. `dashboard` gets a public domain + healthcheck `/health`; `PORT` is injected by Railway (the dashboard Dockerfile honours `$PORT`). The identical stack also runs locally via `docker compose up` and is portable to Synology — all env-specific config stays in env vars. See README "Deploying to Railway".

> When a change adds a table, migration, ETL source, env var, or service, check whether the relevant section above (§2, §3, §5, §7) is now stale and flag the update — do not let this file drift from the code.

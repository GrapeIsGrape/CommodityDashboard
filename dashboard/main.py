"""Read-only FastAPI dashboard.

Serves a boot page (``/``), a ``/health`` check that confirms the service is up
and can reach Postgres (and reports the current Alembic ``schema_version``), and
the Phase 4 server-rendered panels. Panel A (Macro / Cross-Asset, ``/panel/a``,
reads ``macro_metrics``), Panel B (Fundamentals / Inventory, ``/panel/b``, reads
``inventories``), Panel C (Positioning & Flow, ``/panel/c``, reads ``cot`` +
``curve_shape``) and Panel D (Volatility, ``/panel/d``, reads ``iv_metrics``)
render via Jinja2 read-only — no SPA, no client-side fetch. The macro-context
sub-panel (``/panel/macro``, reads ``prices``) and the sentiment placeholder
panel (``/panel/sentiment``, reads ``sentiment_articles`` + ``sentiment_scores``,
empty until a separate Writer-2 project populates them) render likewise. The DB
is never written from a request handler.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import get_database_url, load_scheduler_config
from dashboard.panels import panel_a, panel_b, panel_c, panel_d, panel_macro, panel_sentiment

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dashboard")

app = FastAPI(title="CommodityDashboard", description="Read-only commodity options monitor")

engine = create_engine(get_database_url(), pool_pre_ping=True)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
# Formatting helpers are CLAUDE.md display conventions (% / thousands / dates);
# expose them to every template rather than pre-formatting in the view model.
templates.env.globals["fmt_pct"] = panel_d.format_pct
templates.env.globals["fmt_date"] = panel_d.format_date
templates.env.globals["fmt_int"] = panel_c.format_int
templates.env.globals["fmt_price"] = panel_c.format_price


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (
        "<html><head><title>CommodityDashboard</title></head>"
        "<body><h1>CommodityDashboard is alive</h1>"
        "<p>Phase 4 in progress. See "
        '<a href="/panel/a">Panel A — Macro / Cross-Asset</a>, '
        '<a href="/panel/b">Panel B — Fundamentals / Inventory</a>, '
        '<a href="/panel/c">Panel C — Positioning &amp; Flow</a>, '
        '<a href="/panel/d">Panel D — Volatility</a>, '
        '<a href="/panel/macro">Macro-Context (TLT/VTI/QQQ)</a>, '
        '<a href="/panel/sentiment">Sentiment (placeholder)</a>, '
        'or <a href="/health">/health</a> for service status.</p>'
        "</body></html>"
    )


@app.get("/panel/a", response_class=HTMLResponse)
def panel_a_view(request: Request) -> HTMLResponse:
    """Render Panel A (Macro / Cross-Asset) server-side from a single read-only
    pass over ``macro_metrics``. A fresh/empty/pre-migration DB renders an honest
    empty/error state, not a 500."""
    view = panel_a.build_view(engine)
    return templates.TemplateResponse(request, "panel_a.html", {"view": view})


@app.get("/panel/b", response_class=HTMLResponse)
def panel_b_view(request: Request) -> HTMLResponse:
    """Render Panel B (Fundamentals / Inventory) server-side from a single
    read-only pass over ``inventories``. A fresh/empty/pre-migration DB renders an
    honest empty/error state, not a 500."""
    view = panel_b.build_view(engine)
    return templates.TemplateResponse(request, "panel_b.html", {"view": view})


@app.get("/panel/c", response_class=HTMLResponse)
def panel_c_view(request: Request) -> HTMLResponse:
    """Render Panel C (Positioning & Flow) server-side from a single read-only
    pass over ``cot`` and ``curve_shape``. A fresh/empty DB renders an honest
    empty state, not a 500."""
    view = panel_c.build_view(engine)
    return templates.TemplateResponse(
        request,
        "panel_c.html",
        {"view": view, "lookback_weeks": panel_c.COT_INDEX_LOOKBACK_WEEKS},
    )


@app.get("/panel/d", response_class=HTMLResponse)
def panel_d_view(request: Request) -> HTMLResponse:
    """Render Panel D (Volatility) server-side from a single read-only pass over
    ``iv_metrics``. A fresh/empty DB renders an honest empty state, not a 500."""
    view = panel_d.build_view(engine)
    return templates.TemplateResponse(request, "panel_d.html", {"view": view})


@app.get("/panel/macro", response_class=HTMLResponse)
def panel_macro_view(request: Request) -> HTMLResponse:
    """Render the macro-context sub-panel (TLT/VTI/QQQ) server-side from a single
    read-only pass over ``prices``. Context, not commodities — subordinate to
    Panel A. A fresh/empty/pre-migration DB renders an honest empty/error state,
    not a 500."""
    view = panel_macro.build_view(engine)
    return templates.TemplateResponse(request, "panel_macro.html", {"view": view})


@app.get("/panel/sentiment", response_class=HTMLResponse)
def panel_sentiment_view(request: Request) -> HTMLResponse:
    """Render the sentiment placeholder panel server-side from a single read-only
    pass over ``sentiment_articles`` + ``sentiment_scores``. In v1 these tables
    are empty (populated later by a separate Writer-2 project), so the dominant
    path is an honest "awaiting Writer-2" empty state — distinct from the
    pre-migration/DB-down unavailable state. Never a 500."""
    view = panel_sentiment.build_view(engine)
    return templates.TemplateResponse(request, "panel_sentiment.html", {"view": view})


@app.get("/health")
def health() -> JSONResponse:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            schema_version = _read_schema_version(conn)
            etl_summary = _read_etl_summary(conn)
        return JSONResponse(
            {
                "status": "ok",
                "database": "reachable",
                "schema_version": schema_version,
                "etl": etl_summary,
            }
        )
    except Exception:
        logger.exception("Database health check failed")
        return JSONResponse(status_code=503, content={"status": "error", "database": "unreachable"})


def _read_schema_version(conn) -> str | None:
    """Return the current Alembic head revision, or None on a pre-migration DB.

    Read-only and parameter-free. A fresh database has no ``alembic_version``
    table (or it is empty); that is reported as ``None`` rather than failing the
    health check.
    """
    try:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    except ProgrammingError:
        return None
    return row[0] if row is not None else None


_ETL_SUMMARY_SQL = text(
    """
    SELECT
        latest.slot                                  AS slot,
        latest.source                                AS source,
        latest.run_date                              AS run_date,
        latest.run_finished_at                       AS run_finished_at,
        latest.status                                AS last_status,
        last_success.run_finished_at                 AS last_success_finished_at,
        last_success.run_date                        AS last_success_run_date
    FROM (
        SELECT DISTINCT ON (slot, source)
            slot, source, run_date, run_finished_at, status
        FROM etl_run_log
        ORDER BY slot, source, run_date DESC, run_finished_at DESC NULLS LAST
    ) latest
    LEFT JOIN LATERAL (
        SELECT s.run_date, s.run_finished_at
        FROM etl_run_log s
        WHERE s.slot = latest.slot
          AND s.source = latest.source
          AND s.status = 'success'
        ORDER BY s.run_date DESC, s.run_finished_at DESC NULLS LAST
        LIMIT 1
    ) last_success ON true
    ORDER BY latest.slot, latest.source
    """
)


def _read_etl_summary(conn):
    """Latest-run-per-(slot, source) heartbeat summary for ``/health`` (#24).

    Read-only ``SELECT`` on ``etl_run_log``: the most recent attempt's
    ``run_date``/``run_finished_at``/``last_status`` plus the most recent
    *successful* run (``last_success``, which may pre-date the last attempt when
    the last attempt failed/skipped). Reads via direct SQL — imports nothing from
    ``etl/`` (#17 image isolation).

    Reconciled against the **configured** slot/source set (#24 AC#8a): every
    ``(slot, source)`` declared in ``config/scheduler.yaml`` is positively present
    in the summary, so a source the scheduler *never reached* (dead process,
    uninstalled cron, misconfigured slot) surfaces as ``last_status="never_ran"``
    rather than silently vanishing. The configured set is read via
    ``common.config.load_scheduler_config`` (stdlib/yaml only) — never by
    importing ``etl/`` (#17).

    DB-error-isolated like :func:`_read_schema_version`: a pre-migration DB (no
    ``etl_run_log``) or a transient read error degrades to ``None`` rather than
    failing the health check. ``OperationalError`` is re-raised so a fully-down DB
    still trips the outer 503 handler.
    """
    try:
        rows = conn.execute(_ETL_SUMMARY_SQL).mappings().all()
    except ProgrammingError:
        return None
    except OperationalError:
        raise
    present = [_shape_etl_row(row) for row in rows]
    return _reconcile_etl_summary(present, _configured_slot_sources())


STATUS_NEVER_RAN = "never_ran"

# Deterministic display ordering for the /health etl summary: the operator's eye
# should land on problems first. Anything not listed (or NULL) sorts last.
_STATUS_ORDER = {
    "failure": 0,
    STATUS_NEVER_RAN: 1,
    "skipped": 2,
    "success": 3,
}
_STATUS_ORDER_DEFAULT = 4


def _configured_slot_sources():
    """The ``(slot, source)`` pairs declared in ``config/scheduler.yaml``.

    Pure read of the shared config (no DB, no ``etl`` import). Returns an empty
    list — and the summary degrades to "rows present only" — if the config is
    unreadable/malformed, so a bad config never 500s ``/health``.
    """
    try:
        config = load_scheduler_config()
        slots = (config or {}).get("slots", {}) or {}
    except Exception:
        logger.warning("Scheduler config unreadable for /health reconciliation; "
                       "reporting only logged runs.", exc_info=True)
        return []
    pairs = []
    for slot, spec in slots.items():
        for source in (spec or {}).get("sources", []) or []:
            pairs.append((slot, source))
    return pairs


def _reconcile_etl_summary(present_rows, configured_pairs):
    """Merge the logged ``etl_run_log`` summary rows with the configured
    ``(slot, source)`` set, filling a ``never_ran`` entry for every configured
    pair with no logged row (#24 AC#8a). Pure — no DB, no I/O — so it is
    unit-tested directly.

    Output ordering is deterministic: by status severity (failure, never_ran,
    skipped, success, then anything else) and then ``(slot, source)`` so problems
    surface first and equal-status rows stay stable.
    """
    seen = {(row["slot"], row["source"]) for row in present_rows}
    merged = list(present_rows)
    for slot, source in configured_pairs:
        if (slot, source) not in seen:
            seen.add((slot, source))
            merged.append(_never_ran_row(slot, source))
    merged.sort(
        key=lambda r: (
            _STATUS_ORDER.get(r["last_status"], _STATUS_ORDER_DEFAULT),
            r["slot"] or "",
            r["source"] or "",
        )
    )
    return merged


def _never_ran_row(slot: str, source: str) -> dict:
    """A summary entry for a configured source with no ``etl_run_log`` row — it
    was never reached by the scheduler. All timestamps NULL; ``last_status`` is
    the explicit ``never_ran`` sentinel (honest absence, not a fabricated run)."""
    return {
        "slot": slot,
        "source": source,
        "run_date": None,
        "run_finished_at": None,
        "last_status": STATUS_NEVER_RAN,
        "last_success_run_date": None,
        "last_success_finished_at": None,
    }


def _shape_etl_row(row) -> dict:
    """Shape one ``etl_run_log`` summary row into the JSON payload. Pure — no DB
    — so it is unit-tested directly. ISO-formats timestamps/dates; honest ``None``
    where a column or the last-success row is absent."""
    return {
        "slot": row["slot"],
        "source": row["source"],
        "run_date": _iso(row["run_date"]),
        "run_finished_at": _iso(row["run_finished_at"]),
        "last_status": row["last_status"],
        "last_success_run_date": _iso(row["last_success_run_date"]),
        "last_success_finished_at": _iso(row["last_success_finished_at"]),
    }


def _iso(value):
    """ISO-format a date/datetime for the JSON payload; pass ``None`` through."""
    return value.isoformat() if value is not None else None

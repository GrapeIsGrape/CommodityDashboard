"""Read-only FastAPI dashboard.

Serves a boot page (``/``), a ``/health`` check that confirms the service is up
and can reach Postgres (and reports the current Alembic ``schema_version``), and
the Phase 4 server-rendered panels. Panel C (Positioning & Flow, ``/panel/c``,
reads ``cot`` + ``curve_shape``) and Panel D (Volatility, ``/panel/d``, reads
``iv_metrics``) render via Jinja2 read-only — no SPA, no client-side fetch. The
DB is never written from a request handler.
"""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from common.config import get_database_url
from dashboard.panels import panel_c, panel_d

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
        '<a href="/panel/c">Panel C — Positioning &amp; Flow</a>, '
        '<a href="/panel/d">Panel D — Volatility</a>, '
        'or <a href="/health">/health</a> for service status.</p>'
        "</body></html>"
    )


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


@app.get("/health")
def health() -> JSONResponse:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            schema_version = _read_schema_version(conn)
        return JSONResponse(
            {"status": "ok", "database": "reachable", "schema_version": schema_version}
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

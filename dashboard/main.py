"""Read-only FastAPI dashboard.

Phase 1 scaffold: serves a boot page and a /health check that confirms the
service is up and can reach Postgres. Panels arrive in Phase 4.
"""

import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from common.config import get_database_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dashboard")

app = FastAPI(title="CommodityDashboard", description="Read-only commodity options monitor")

engine = create_engine(get_database_url(), pool_pre_ping=True)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (
        "<html><head><title>CommodityDashboard</title></head>"
        "<body><h1>CommodityDashboard is alive</h1>"
        "<p>Phase 1 scaffold. Panels arrive in Phase 4. "
        'See <a href="/health">/health</a> for service status.</p>'
        "</body></html>"
    )


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

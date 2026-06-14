"""Read-only FastAPI dashboard.

Phase 1 scaffold: serves a boot page and a /health check that confirms the
service is up and can reach Postgres. Panels arrive in Phase 4.
"""

import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import create_engine, text

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
        return JSONResponse({"status": "ok", "database": "reachable"})
    except Exception:
        logger.exception("Database health check failed")
        return JSONResponse(status_code=503, content={"status": "error", "database": "unreachable"})

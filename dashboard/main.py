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

import datetime as dt
import logging
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import get_database_url, load_release_calendar, load_scheduler_config
from common.release_calendar import upcoming_events
from dashboard.panels import panel_a, panel_b, panel_c, panel_d, panel_macro, panel_sentiment
from dashboard.panels.panel_a import (
    is_trading_session as _is_trading_session,
    last_expected_session as _last_expected_session,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("dashboard")

# --- ETL staleness helpers (pure, clock-injectable — #25) --------------------

ET = ZoneInfo("America/New_York")

# Named grace constants — one named place; asserted by boundary tests.
# Daily (release-driven) sources are idempotent polls: a single skipped day is
# tolerated. > _DAILY_GRACE_DAYS triggers STALE.
_DAILY_GRACE_DAYS = 2
# Weekday (market-data) sources: no additional grace — the weekend buffer
# is built into the session model. The constant is intentionally absent;
# the logic uses a direct < comparison (see is_etl_source_stale).


def _last_trading_session(today: dt.date) -> dt.date:
    """Most recent trading session on or before ``today`` (holiday-aware).

    Pure and clock-injectable: ``today`` is always supplied by the caller.
    Delegates to ``panel_a.is_trading_session`` + ``panel_a.last_expected_session``
    so weekend AND US market holiday awareness live in a single place.

    * If ``today`` is itself a trading session, returns ``today`` (the expected
      run target is today's session).
    * Otherwise rolls back to the prior trading session (e.g. a Saturday or a
      holiday Monday returns the preceding Friday).
    """
    if _is_trading_session(today):
        return today
    return _last_expected_session(today)


def is_etl_source_stale(
    run_date: Optional[dt.date],
    cadence: str,
    now_et: dt.datetime,
    slot_time_str: Optional[str] = None,
) -> Optional[bool]:
    """Pure, clock-injectable staleness verdict for one ETL source.

    Parameters
    ----------
    run_date:
        The latest ``run_date`` recorded in ``etl_run_log`` for this source.
        ``None`` means the source never ran or the date is unknown.
    cadence:
        ``"weekdays"`` for market-data sources (curve_shape / iv / vol_indices
        / prices — Mon–Fri only); ``"daily"`` for release-driven sources (fred
        / eia / usda / cftc — every calendar day).
    now_et:
        The injected ET "now" — never read from the wall clock inside this
        function.
    slot_time_str:
        The slot's configured fire time as ``"HH:MM"`` (e.g. ``"16:20"``),
        required for ``"weekdays"`` cadence to determine whether the slot has
        had its chance to fire today.  When ``None``, the weekday path falls
        back to the pre-slot-time model (graceful degradation — slightly eager
        STALE rather than a crash).

    Returns
    -------
    None
        ``run_date`` is ``None`` (never ran or unknown). The caller must not
        treat this as ``stale=False`` — ``never_ran`` is the more alarming
        state and takes precedence.
    True
        The source is stale: the last recorded run predates the most recent
        expected session or exceeds the daily grace window.
    False
        The source is fresh.
    """
    if run_date is None:
        return None
    if cadence == "weekdays":
        today = now_et.date()
        slot_time = None
        if slot_time_str is not None:
            try:
                h, m = map(int, str(slot_time_str).split(":"))
                slot_time = dt.time(h, m)
            except (ValueError, AttributeError, TypeError):
                pass  # fall through to graceful degradation below
        if slot_time is not None and _is_trading_session(today) and now_et.time() < slot_time:
            # The slot has not had its chance to fire today: the prior
            # session is still the freshest expected datum.  e.g. Monday
            # 08:00 before the 16:20 close-batch — Friday's run is fresh.
            expected = _last_expected_session(today)
        else:
            # Slot has fired (or today is weekend/holiday, or no valid slot
            # time available): the most recent trading session should have run.
            expected = _last_trading_session(today)
        return run_date < expected
    # "daily" (or any unrecognised cadence falls through to the grace model)
    age_days = (now_et.date() - run_date).days
    return age_days > _DAILY_GRACE_DAYS


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
def index(request: Request) -> HTMLResponse:
    now_et = dt.datetime.now(ET)
    try:
        cal_config = load_release_calendar()
        events, unconfigured_types = upcoming_events(now_et, cal_config)
    except Exception:
        logger.exception("Release calendar computation failed; rendering empty strip")
        events, unconfigured_types = [], []
    today_et = now_et.date()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "now_et": now_et,
            "today_et": today_et,
            "tomorrow_et": today_et + dt.timedelta(days=1),
            "calendar_events": events,
            "unconfigured_cal_types": unconfigured_types,
        },
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


def _read_etl_summary(conn, *, now_et: Optional[dt.datetime] = None):
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

    Adds a derived per-source ``stale`` flag (#25): ``True`` when the source's
    latest ``run_date`` is older than its configured cadence allows, ``False``
    when fresh, ``None`` when ``run_date`` is absent (the ``never_ran`` state is
    already the alarming signal; this does not double-report it).

    ``now_et`` is optional (defaults to the wall clock) so callers — especially
    tests — can inject a deterministic clock without needing to patch a global.

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
    resolved_now_et = now_et or dt.datetime.now(ET)
    cfg = _load_scheduler_config_safe()
    cadence_map = _build_cadence_map(cfg)
    present = [_shape_etl_row(row, cadence_map, resolved_now_et) for row in rows]
    return _reconcile_etl_summary(present, _configured_slot_sources(cfg))


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


def _load_scheduler_config_safe() -> Optional[dict]:
    """Load ``config/scheduler.yaml`` once, returning ``None`` on any error.

    Centralised so :func:`_read_etl_summary` can parse the YAML a single time
    and hand the result to both :func:`_build_cadence_map` and
    :func:`_configured_slot_sources` — avoiding a double parse per ``/health``
    request.
    """
    try:
        return load_scheduler_config()
    except Exception:
        logger.warning("Scheduler config unreadable for /health; "
                       "stale flags and reconciliation will degrade gracefully.",
                       exc_info=True)
        return None


def _configured_slot_sources(raw_config: Optional[dict] = None) -> list:
    """The ``(slot, source)`` pairs declared in ``config/scheduler.yaml``.

    Pure read of the shared config (no DB, no ``etl`` import). Accepts a
    pre-parsed ``raw_config`` dict so the caller can parse the YAML once and
    share it with :func:`_build_cadence_map`. Falls back to parsing itself when
    ``raw_config`` is ``None``. Returns an empty list — and the summary degrades
    to "rows present only" — if the config is unreadable/malformed, so a bad
    config never 500s ``/health``.
    """
    try:
        config = raw_config if raw_config is not None else load_scheduler_config()
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


def _build_cadence_map(raw_config: Optional[dict] = None) -> dict:
    """Map ``(slot, source)`` → ``(cadence, slot_time_str)`` tuple.

    ``cadence`` is ``"daily"`` | ``"weekdays"`` (from the slot's ``days``
    field); ``slot_time_str`` is the slot's ``at`` value (e.g. ``"16:20"``),
    which ``is_etl_source_stale`` needs to decide whether the slot has had its
    chance to fire today.

    Reads the same ``config/scheduler.yaml`` as :func:`_configured_slot_sources`.
    Accepts a pre-parsed ``raw_config`` dict so the caller can parse the YAML
    once and share it. Falls back to parsing itself when ``raw_config`` is
    ``None``. Returns an empty dict — causing ``stale=None`` for all rows — if
    the config is unreadable or malformed, consistent with the
    graceful-degradation pattern of :func:`_configured_slot_sources`.
    """
    try:
        config = raw_config if raw_config is not None else load_scheduler_config()
        slots = (config or {}).get("slots", {}) or {}
    except Exception:
        logger.warning("Scheduler config unreadable for /health cadence map; "
                       "stale flag will be None for all rows.", exc_info=True)
        return {}
    cadence_map: dict = {}
    for slot, spec in slots.items():
        cadence = (spec or {}).get("days")
        slot_time_str = (spec or {}).get("at")
        for source in (spec or {}).get("sources", []) or []:
            cadence_map[(slot, source)] = (cadence, slot_time_str)
    return cadence_map


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
    the explicit ``never_ran`` sentinel (honest absence, not a fabricated run).

    ``stale`` is ``None``: never_ran is already the alarming state and takes
    precedence; returning ``False`` here would mask the problem (#25 AC#6).
    """
    return {
        "slot": slot,
        "source": source,
        "run_date": None,
        "run_finished_at": None,
        "last_status": STATUS_NEVER_RAN,
        "last_success_run_date": None,
        "last_success_finished_at": None,
        "stale": None,
    }


def _shape_etl_row(row, cadence_map: dict, now_et: dt.datetime) -> dict:
    """Shape one ``etl_run_log`` summary row into the JSON payload. Pure — no DB
    — so it is unit-tested directly. ISO-formats timestamps/dates; honest ``None``
    where a column or the last-success row is absent.

    Adds a derived ``stale`` flag (#25): computed from the row's ``run_date``,
    the cadence for this ``(slot, source)`` pair, and the injected ET ``now_et``.
    If the cadence is unknown (not in ``cadence_map``), ``stale`` is ``None``
    (graceful degradation, never a 500).
    """
    slot = row["slot"]
    source = row["source"]
    run_date = row["run_date"]
    cadence_entry = cadence_map.get((slot, source))
    if cadence_entry is not None:
        cadence, slot_time_str = cadence_entry
        stale: Optional[bool] = is_etl_source_stale(run_date, cadence, now_et,
                                                     slot_time_str=slot_time_str)
    else:
        stale = None
    return {
        "slot": slot,
        "source": source,
        "run_date": _iso(run_date),
        "run_finished_at": _iso(row["run_finished_at"]),
        "last_status": row["last_status"],
        "last_success_run_date": _iso(row["last_success_run_date"]),
        "last_success_finished_at": _iso(row["last_success_finished_at"]),
        "stale": stale,
    }


def _iso(value):
    """ISO-format a date/datetime for the JSON payload; pass ``None`` through."""
    return value.isoformat() if value is not None else None

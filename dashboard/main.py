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
empty until a separate Writer-2 project populates them) render likewise.

The one non-read-only endpoint is ``POST /health/trigger`` (#29): an operator
button that inserts a row into ``etl_manual_trigger`` so the ETL scheduler can
pick it up and dispatch all sources immediately.  This is an ops/admin action —
not market-data write, not trade execution.
"""

import datetime as dt
import logging
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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


# --- Manual ETL trigger constants (#29) -------------------------------------

# How long (minutes) the rate-limit guard blocks a second trigger after the
# first.  Kept as a module-level constant so pure tests can assert the boundary
# without importing a live DB.
_TRIGGER_COOLDOWN_MINUTES = 10

_TRIGGER_CHECK_SQL = text(
    """
    SELECT id, requested_at
    FROM etl_manual_trigger
    WHERE processed_at IS NULL
       OR processed_at >= now() - interval '10 minutes'
    ORDER BY requested_at DESC
    LIMIT 1
    """
)

_TRIGGER_INSERT_SQL = text(
    "INSERT INTO etl_manual_trigger (slot) VALUES ('all')"
)


def _check_trigger_rate_limit(
    conn,
    now: Optional[dt.datetime] = None,
) -> tuple[bool, Optional[int]]:
    """Query ``etl_manual_trigger`` to determine whether a new trigger is allowed.

    Returns ``(rate_limited: bool, wait_minutes: Optional[int])``.

    * ``rate_limited=False`` → no blocking row; the caller may INSERT.
    * ``rate_limited=True`` → a blocking row exists; ``wait_minutes`` is the
      remaining cooldown (≥1), computed from the most recent ``requested_at``.

    Pure in the sense that the clock is injectable via ``now``.  Raises
    ``ProgrammingError`` when the table does not exist (pre-migration) and
    ``OperationalError`` when the DB is down — both must be caught by the caller.
    """
    row = conn.execute(_TRIGGER_CHECK_SQL).first()
    if row is None:
        return False, None
    requested_at = row[1]
    resolved_now = now or dt.datetime.now(dt.timezone.utc)
    if requested_at.tzinfo is None:
        requested_at = requested_at.replace(tzinfo=dt.timezone.utc)
    elapsed_minutes = (resolved_now - requested_at).total_seconds() / 60.0
    remaining = int(_TRIGGER_COOLDOWN_MINUTES - elapsed_minutes) + 1
    return True, max(1, remaining)


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
    today_et = now_et.date()

    # --- Release calendar strip ------------------------------------------
    try:
        cal_config = load_release_calendar()
        events, unconfigured_types = upcoming_events(now_et, cal_config)
    except Exception:
        logger.exception("Release calendar computation failed; rendering empty strip")
        events, unconfigured_types = [], []

    # --- Per-panel view models (each isolated: one failing never 500s the page) ---
    try:
        view_d = panel_d.build_view(engine)
    except Exception:
        logger.exception("Panel D build_view failed unexpectedly in unified index")
        view_d = panel_d.PanelDView(
            underlyings=[], indices=[],
            last_session=panel_d.last_expected_session(today_et), error=True,
        )

    try:
        view_c = panel_c.build_view(engine)
        panel_c_lookback_weeks = panel_c.COT_INDEX_LOOKBACK_WEEKS
    except Exception:
        logger.exception("Panel C build_view failed unexpectedly in unified index")
        view_c = _panel_c_error_view(today_et)
        panel_c_lookback_weeks = panel_c.COT_INDEX_LOOKBACK_WEEKS

    try:
        view_a = panel_a.build_view(engine)
    except Exception:
        logger.exception("Panel A build_view failed unexpectedly in unified index")
        view_a = _panel_a_error_view(today_et)

    try:
        view_b = panel_b.build_view(engine)
    except Exception:
        logger.exception("Panel B build_view failed unexpectedly in unified index")
        view_b = _panel_b_error_view()

    try:
        view_macro = panel_macro.build_view(engine)
    except Exception:
        logger.exception("Panel macro build_view failed unexpectedly in unified index")
        view_macro = _panel_macro_error_view(today_et)

    try:
        view_sentiment = panel_sentiment.build_view(engine)
    except Exception:
        logger.exception("Panel sentiment build_view failed unexpectedly in unified index")
        view_sentiment = _panel_sentiment_error_view()

    # --- Health data (DB status + ETL summary) ---------------------------
    health_view = _build_health_view(now_et)

    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "now_et": now_et,
            "today_et": today_et,
            "tomorrow_et": today_et + dt.timedelta(days=1),
            "calendar_events": events,
            "unconfigured_cal_types": unconfigured_types,
            # panel views (namespaced so partials can be {% include %}d)
            "view_d": view_d,
            "view_c": view_c,
            "panel_c_lookback_weeks": panel_c_lookback_weeks,
            "view_a": view_a,
            "view_b": view_b,
            "view_macro": view_macro,
            "view_sentiment": view_sentiment,
            "health_view": health_view,
        },
    )


def _panel_c_error_view(today: dt.date) -> "panel_c.PanelCView":
    """Return a minimal Panel C view in the error state (no COT/curve rows)."""
    return panel_c.PanelCView(
        cot_rows=[],
        curve_cards=[],
        expected_report_date=panel_c.expected_cot_report_date(today),
        error=True,
    )


def _panel_a_error_view(today: dt.date) -> "panel_a.PanelAView":
    """Return a minimal Panel A view in the error state."""
    return panel_a.PanelAView(
        groups=[],
        last_session=panel_a.last_expected_session(today),
        error=True,
    )


def _panel_b_error_view() -> "panel_b.PanelBView":
    """Return a minimal Panel B view in the error state."""
    return panel_b.PanelBView(
        groups=[],
        seasonality_mode=panel_b.ACTIVE_SEASONALITY_MODE,
        error=True,
    )


def _panel_macro_error_view(today: dt.date) -> "panel_macro.PanelMacroView":
    """Return a minimal macro-context view in the error state."""
    return panel_macro.PanelMacroView(
        rows=[],
        last_session=panel_macro.last_expected_session(today),
        error=True,
    )


def _panel_sentiment_error_view() -> "panel_sentiment.PanelSentimentView":
    """Return a minimal sentiment view in the UNAVAILABLE (error) state."""
    return panel_sentiment.PanelSentimentView(articles=[], error=True)


class _HealthView:
    """Minimal data container for health info surfaced in the unified index."""

    def __init__(self, db_ok, schema_version, etl_summary, trigger_available, cooldown_minutes):
        self.db_ok = db_ok
        self.schema_version = schema_version
        self.etl_summary = etl_summary
        self.trigger_available = trigger_available
        self.cooldown_minutes = cooldown_minutes


def _build_health_view(now_et: dt.datetime) -> "_HealthView":
    """Read DB status + ETL summary for the unified index health card.

    Never raises: DB errors degrade to db_ok=False, etl_summary=None.
    """
    db_ok = False
    schema_version = None
    etl_summary = None
    trigger_available = False
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            db_ok = True
            schema_version = _read_schema_version(conn)
            etl_summary = _read_etl_summary(conn, now_et=now_et)
            trigger_available = _is_trigger_table_reachable(conn)
    except Exception:
        logger.exception("Database health check failed in unified index")
    return _HealthView(
        db_ok=db_ok,
        schema_version=schema_version,
        etl_summary=etl_summary,
        trigger_available=trigger_available,
        cooldown_minutes=_TRIGGER_COOLDOWN_MINUTES,
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


@app.get("/health", response_class=HTMLResponse)
def health(
    request: Request,
    triggered: Optional[int] = None,
    rate_limited: Optional[int] = None,
    wait_minutes: Optional[int] = None,
    trigger_unavailable: Optional[int] = None,
) -> HTMLResponse:
    """Render the health page (HTML): DB status, ETL run summary, and the
    manual-trigger form (#29).  URL params ``?triggered=1`` / ``?rate_limited=1``
    drive confirmation / rate-limit banners; ``?trigger_unavailable=1`` hides
    the trigger button (pre-migration).  Never a 500."""
    db_ok = False
    schema_version = None
    etl_summary = None
    trigger_available = False

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            db_ok = True
            schema_version = _read_schema_version(conn)
            etl_summary = _read_etl_summary(conn)
            if not trigger_unavailable:
                trigger_available = _is_trigger_table_reachable(conn)
    except OperationalError:
        logger.exception("Database health check failed")

    return templates.TemplateResponse(
        request,
        "health.html",
        {
            "db_ok": db_ok,
            "schema_version": schema_version,
            "etl_summary": etl_summary,
            "trigger_available": trigger_available,
            "trigger_unavailable": bool(trigger_unavailable),
            "cooldown_minutes": _TRIGGER_COOLDOWN_MINUTES,
            "triggered": bool(triggered),
            "rate_limited": bool(rate_limited),
            "wait_minutes": wait_minutes,
        },
    )


def _is_trigger_table_reachable(conn) -> bool:
    """Return ``True`` when ``etl_manual_trigger`` exists and is queryable.

    Used by the health page to decide whether to show or hide the trigger
    button (AC#9/#10).  A ``ProgrammingError`` (pre-migration) → ``False``
    (button hidden).  Any other error propagates to the caller.
    """
    try:
        conn.execute(text("SELECT 1 FROM etl_manual_trigger LIMIT 0"))
        return True
    except ProgrammingError:
        return False


@app.post("/health/trigger")
def health_trigger(request: Request):  # noqa: ARG001
    """Insert a manual ETL trigger row then redirect to ``GET /health?triggered=1``.

    Rate-limited: if an unprocessed row or a row processed within the last
    ``_TRIGGER_COOLDOWN_MINUTES`` minutes already exists, returns a redirect to
    ``GET /health?rate_limited=1&wait_minutes=N`` instead of inserting.

    Error paths:
    * ``OperationalError`` (DB down) → JSON 503 (honest, never 500).
    * ``ProgrammingError`` (pre-migration) → redirect to
      ``GET /health?trigger_unavailable=1`` (graceful, button hidden).
    """
    try:
        with engine.begin() as conn:
            rate_limited, wait_minutes = _check_trigger_rate_limit(conn)
            if rate_limited:
                url = "/health?rate_limited=1"
                if wait_minutes is not None:
                    url += f"&wait_minutes={wait_minutes}"
                return RedirectResponse(url, status_code=303)
            conn.execute(_TRIGGER_INSERT_SQL)
        return RedirectResponse("/health?triggered=1", status_code=303)
    except ProgrammingError:
        return RedirectResponse("/health?trigger_unavailable=1", status_code=303)
    except OperationalError:
        logger.exception("DB unavailable for manual trigger insert")
        return JSONResponse({"error": "DB unavailable"}, status_code=503)


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

"""ETL run-log / heartbeat writer (#24).

The scheduler's :func:`etl.scheduler.dispatch_slot` records one row per source per
dispatch into ``etl_run_log`` (migration ``0004``): ``success`` when the source's
``run()`` returns, ``failure`` (with a short, redacted ``detail``) when it raises,
and ``skipped`` for every source in a session-guarded slot the AC#4 guard skips.

Two layers, deliberately split so the scheduler's pure clock-injectable tests
stay DB-free:

* **Pure helpers** — :func:`classify_status`, :func:`redact_detail`,
  :func:`summarize_exception` — no I/O, unit-tested without a clock or DB.
* **Best-effort DB writer** — :func:`write_run_log` upserts one row on the
  ``(slot, source, run_date)`` natural key, swallowing every error so a heartbeat
  write can never become a new failure mode (#23 AC#6 / #24 AC#5). The scheduler
  threads a ``heartbeat`` callable (defaulting to this writer) so tests inject a
  mock and never touch a real DB.

Secrets never reach ``detail``: :func:`summarize_exception` emits a bounded
``"<ExcType>: <truncated message>"`` with known secret-shaped patterns stripped
(the FRED/EIA/USDA ``_redact`` discipline, #5), never a raw traceback.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from common.config import get_database_url

logger = logging.getLogger("etl.run_log")

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"
STATUS_SKIPPED = "skipped"

_DETAIL_MAX_LEN = 500

# Secret-shaped patterns scrubbed from any exception text before it is persisted.
# Each captures a key=value/header form and redacts only the value, leaving the
# surrounding message intact for debuggability. URL query params and JSON-ish
# "token": "..." forms are both covered because source requests embed keys both
# ways (FRED/EIA query string, CFTC X-App-Token header).
_SECRET_PATTERNS = [
    re.compile(
        r"(?i)(api[_-]?key|app[_-]?token|token|key|password|passwd|pwd|secret|"
        r"authorization|auth)"
        # Require an explicit = or : separator so we only redact genuine
        # key=value / "key": "value" / header: value forms — not adjacent prose
        # like "the secret sauce" (every real secret form has a separator).
        r"(\s*[=:]\s*[\"']?\s*)"
        r"((?:bearer|basic|token)\s+)?"  # optional auth scheme keyword, kept
        r"([^\s&\"';,}]+)"
    ),
]
_REDACTED = "***"

# Defense-in-depth for the DSN/connection-string form ``scheme://user:password@host``
# (e.g. ``postgresql://commodity:change_me@db:5432/commodity``) — the key=value
# scrubber above does not match this shape, so a raw connection URL surfacing in an
# exception message would otherwise leak POSTGRES_PASSWORD into ``etl_run_log.detail``
# (#24 — the column's #1 risk). We strip the ``user:password@`` userinfo, keeping the
# scheme/host for debuggability. We do NOT rely on upstream libraries to mask it.
_DSN_CREDENTIALS_PATTERN = re.compile(
    r"(?i)\b([a-z][a-z0-9+.\-]*://)([^/\s:@]+):[^/\s@]+@"
)


def classify_status(succeeded: bool) -> str:
    """Map a source dispatch outcome to a status literal. ``skipped`` is recorded
    separately (the guard path), so this only distinguishes ran-and-returned from
    ran-and-raised."""
    return STATUS_SUCCESS if succeeded else STATUS_FAILURE


def redact_detail(message: str) -> str:
    """Strip secret-shaped ``key=value`` / ``token: value`` fragments from a free
    message so an API key, token, or password embedded in an exception string
    never reaches ``etl_run_log.detail`` (#24 AC#6)."""
    if not message:
        return message
    redacted = message
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(
            lambda m: f"{m.group(1)}{m.group(2)}{m.group(3) or ''}{_REDACTED}", redacted
        )
    # Strip ``user:password@`` userinfo from any DSN/URL, keeping ``scheme://user@``.
    redacted = _DSN_CREDENTIALS_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}:{_REDACTED}@", redacted
    )
    return redacted


def summarize_exception(exc: BaseException) -> str:
    """Build a bounded, redacted one-line summary of an exception for ``detail``:
    ``"<ExcType>: <redacted, truncated message>"`` — never a raw traceback, never
    a secret."""
    message = redact_detail(str(exc))
    summary = f"{type(exc).__name__}: {message}".strip()
    if len(summary) > _DETAIL_MAX_LEN:
        summary = summary[: _DETAIL_MAX_LEN - 1].rstrip() + "…"
    return summary


_UPSERT_SQL = text(
    """
    INSERT INTO etl_run_log (
        slot, source, run_date, run_started_at, run_finished_at, status, detail
    )
    VALUES (
        :slot, :source, :run_date, :run_started_at, :run_finished_at, :status, :detail
    )
    ON CONFLICT ON CONSTRAINT uq_etl_run_log_slot_source_date
    DO UPDATE SET
        run_started_at = EXCLUDED.run_started_at,
        run_finished_at = EXCLUDED.run_finished_at,
        status = EXCLUDED.status,
        detail = EXCLUDED.detail
    """
)


def write_run_log(
    slot: str,
    source: str,
    run_date: dt.date,
    status: str,
    *,
    run_started_at: Optional[dt.datetime] = None,
    run_finished_at: Optional[dt.datetime] = None,
    detail: Optional[str] = None,
    engine: Optional[Engine] = None,
) -> bool:
    """Best-effort upsert of one heartbeat row, keyed on ``(slot, source,
    run_date)`` so a same-day re-dispatch overwrites in place (idempotent, #24
    AC#7).

    Returns ``True`` on a successful write, ``False`` if the write failed. **Every
    error is caught and logged** — a heartbeat write must never abort the source
    batch or become a new failure mode (#24 AC#5). A missing ``etl_run_log``
    (pre-migration) therefore degrades to a logged no-op.
    """
    row = {
        "slot": slot,
        "source": source,
        "run_date": run_date,
        "run_started_at": run_started_at,
        "run_finished_at": run_finished_at,
        "status": status,
        "detail": detail,
    }
    try:
        own_engine = engine is None
        engine = engine or create_engine(get_database_url())
        try:
            with engine.begin() as conn:
                conn.execute(_UPSERT_SQL, row)
        finally:
            if own_engine:
                engine.dispose()
        return True
    except Exception:
        logger.warning(
            "Run-log heartbeat write failed for slot=%s source=%s (%s); "
            "continuing — observability only, batch unaffected.",
            slot,
            source,
            status,
            exc_info=True,
        )
        return False


def now_utc() -> dt.datetime:
    """Timezone-aware UTC wall clock for the heartbeat timestamps."""
    return dt.datetime.now(dt.timezone.utc)

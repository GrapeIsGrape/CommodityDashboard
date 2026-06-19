"""Sentiment placeholder panel — read-only view model for the dashboard.

The **last Phase-4 render**. CLAUDE.md §1 ("1 DB, 2 writers") reserves room for a
*separate, not-yet-built* project — **Writer 2**, a scheduled LLM news-sentiment
task that will write to its own ``sentiment_articles`` / ``sentiment_scores``
tables. Those tables already exist (migration ``0002``); this module is the
matching empty dashboard panel CLAUDE.md asks for ("leave room: placeholder
schema + empty dashboard panel"). **No Writer-2 ingestion is built here** — this
is render-only.

This module is **read-only** — a single SELECT (``sentiment_articles`` LEFT JOIN
``sentiment_scores``), never a write. It resolves **three distinct, all-honest
states** (none ever 500s, mirroring Panels A–D):

* **EMPTY (the v1 happy path):** both tables reachable but zero rows → an
  explicit "awaiting Writer-2 — no sentiment data yet" placeholder. This is a
  *normal expected state*, NOT an error, and NOT a fabricated row.
* **UNAVAILABLE:** the tables do not exist yet (``ProgrammingError``, a
  pre-migration DB) or Postgres is unreachable (``OperationalError``) → an honest
  data-unavailable state, *visually distinct* from EMPTY.
* **POPULATED (forward-compatible):** rows exist → each article is surfaced with
  its headline / URL / timestamp and every joined score's commodity, **score and
  ``reasoning``** (CLAUDE.md §5 — reasoning is surfaced, not just the score) plus
  the ``model``. An article with no scores yet renders cleanly (LEFT JOIN). Built
  defensively against whatever Writer 2 eventually fills — no column is assumed
  non-NULL.

NULL discipline (#22 AC5): a NULL ``score`` renders ``—`` *distinct from a real
0* (a genuine neutral-sentiment 0 must not collapse into the NULL display);
NULL ``headline`` / ``reasoning`` / timestamp render ``—``; nothing is fabricated
or carried forward.

The grouping / state-selection / NULL handling is *pure* and network-free so it
unit-tests without a live DB, mirroring ``panel_a.py`` / ``panel_macro.py``. The
dashboard ships its own image without ``etl`` (#17) — this module imports nothing
from ``etl``.
"""

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError

logger = logging.getLogger("dashboard.panel_sentiment")

# Cap the article list so a future high-volume Writer 2 can't render an unbounded
# page (pagination/search is out of scope until there is real volume — #22).
_MAX_ARTICLES = 200


# --- Formatting (CLAUDE.md §3 conventions) --------------------------------

def format_score(value: Optional[float]) -> str:
    """A sentiment score. NULL → em dash; a real ``0`` (genuine neutral) renders
    as ``0`` — the two must NOT collapse (#22 AC5). Trailing-zero noise from a
    NUMERIC column is trimmed so 0.50 reads ``0.5`` and a whole 0 reads ``0``."""
    if value is None:
        return "—"
    text_value = f"{float(value):.4f}".rstrip("0").rstrip(".")
    return text_value if text_value not in ("", "-0") else "0"


def format_text(value: Optional[str]) -> str:
    """A free-text field (headline / reasoning / model / commodity). NULL or an
    all-whitespace blank → em dash; never fabricated."""
    if value is None:
        return "—"
    stripped = value.strip()
    return stripped if stripped else "—"


def format_timestamp(value: Optional[dt.datetime]) -> str:
    """A timestamp as ``YYYY-MM-DD`` when it is midnight (date-only), else an
    honest full ``YYYY-MM-DD HH:MM`` datetime (#22 AC6). NULL → em dash."""
    if value is None:
        return "—"
    if isinstance(value, dt.datetime):
        if value.hour == 0 and value.minute == 0 and value.second == 0:
            return value.date().isoformat()
        return value.strftime("%Y-%m-%d %H:%M")
    return value.isoformat()


def safe_href(value: Optional[str]) -> Optional[str]:
    """Return the URL only when its scheme is ``http``/``https`` so it is safe to
    place in an anchor ``href``; otherwise ``None`` (the caller renders the raw
    URL as inert text). The ``url`` is future externally-sourced Writer-2/LLM
    content, so a ``javascript:`` / ``data:`` scheme must never become a clickable
    link — Jinja2 autoescaping stops attribute breakout but does NOT neutralise a
    dangerous scheme. NULL/blank → ``None``."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    scheme = stripped.split(":", 1)[0].lower() if ":" in stripped else ""
    return stripped if scheme in ("http", "https") else None


def pick_timestamp(
    published_at: Optional[dt.datetime],
    fetched_at: Optional[dt.datetime],
    created_at: Optional[dt.datetime],
) -> tuple[Optional[dt.datetime], str]:
    """Timestamp fallback ``published_at`` → ``fetched_at`` → ``created_at``
    (#22 AC4). Returns the chosen value plus a label naming which field it came
    from (so the render is honest about a fallback). All-NULL → ``(None,
    "published")``."""
    if published_at is not None:
        return published_at, "published"
    if fetched_at is not None:
        return fetched_at, "fetched"
    if created_at is not None:
        return created_at, "created"
    return None, "published"


# --- View-model rows ------------------------------------------------------

@dataclass
class ScoreRow:
    commodity: str  # pre-formatted (em dash on NULL).
    score: str  # pre-formatted: "—" for NULL, "0" for a real zero.
    reasoning: str  # pre-formatted (em dash on NULL).
    model: str  # pre-formatted (em dash on NULL).
    scored_at: str  # pre-formatted (em dash on NULL).


@dataclass
class ArticleRow:
    url: str  # NOT NULL in the schema; rendered as text (always shown).
    href: Optional[str]  # url only when its scheme is http(s); else None (no link).
    headline: str  # pre-formatted (em dash on NULL).
    timestamp: str  # pre-formatted (em dash on NULL).
    timestamp_source: str  # which field the timestamp came from.
    scores: list[ScoreRow] = field(default_factory=list)

    @property
    def has_scores(self) -> bool:
        return bool(self.scores)


@dataclass
class PanelSentimentView:
    """Exactly one of the three states is true. ``error`` (UNAVAILABLE) takes
    precedence; otherwise empty ``articles`` is the EMPTY awaiting-Writer-2
    state, and a non-empty list is POPULATED."""
    articles: list[ArticleRow]
    error: bool = False

    @property
    def is_unavailable(self) -> bool:
        return self.error

    @property
    def is_empty(self) -> bool:
        return not self.error and not self.articles

    @property
    def is_populated(self) -> bool:
        return not self.error and bool(self.articles)


# --- Pure grouping (DB-free, unit-testable) -------------------------------

def group_articles(rows: list) -> list[ArticleRow]:
    """Collapse the flat LEFT-JOIN result (one row per article×score, with a
    single row carrying NULL score columns for an article with no scores yet)
    into one :class:`ArticleRow` per article, in the order articles first appear
    (the query orders newest-first).

    Each ``row`` is a mapping-like with ``url``, ``headline``, ``published_at``,
    ``fetched_at``, ``created_at`` and the (possibly NULL) score columns
    ``commodity``, ``score``, ``reasoning``, ``model``, ``scored_at``. The score
    block is treated as present only when the score's own NOT-NULL keys
    (``commodity`` + ``model``) are present — that is how a no-score LEFT-JOIN
    row is told apart from a real score whose ``score``/``reasoning`` happen to
    be NULL. Pure: no DB, no fabrication, NULL→``—`` and a real ``0`` preserved.
    """
    by_url: dict[str, ArticleRow] = {}
    for row in rows:
        m = row._mapping if hasattr(row, "_mapping") else row
        url = m["url"]
        article = by_url.get(url)
        if article is None:
            ts_value, ts_source = pick_timestamp(
                m.get("published_at"), m.get("fetched_at"), m.get("created_at")
            )
            article = ArticleRow(
                url=url,
                href=safe_href(url),
                headline=format_text(m.get("headline")),
                timestamp=format_timestamp(ts_value),
                timestamp_source=ts_source,
            )
            by_url[url] = article

        # A score block exists only when its NOT-NULL natural-key parts are
        # present; a pure no-score LEFT-JOIN row has them all NULL.
        if m.get("commodity") is not None and m.get("model") is not None:
            article.scores.append(
                ScoreRow(
                    commodity=format_text(m.get("commodity")),
                    score=format_score(m.get("score")),
                    reasoning=format_text(m.get("reasoning")),
                    model=format_text(m.get("model")),
                    scored_at=format_timestamp(m.get("scored_at")),
                )
            )
    return list(by_url.values())


# --- Read-only query ------------------------------------------------------

# Articles newest-first (timestamp fallback handled in Python so the SQL stays
# simple), LEFT JOIN scores so a not-yet-scored article still appears once.
# Capped at the latest _MAX_ARTICLES articles via a subselect so the JOIN can't
# blow the row budget on a multi-score article near the cut.
_ARTICLES_SQL = text(
    """
    WITH recent AS (
        SELECT id, url, headline, published_at, fetched_at, created_at
        FROM sentiment_articles
        ORDER BY COALESCE(published_at, fetched_at, created_at) DESC NULLS LAST,
                 id DESC
        LIMIT :limit
    )
    SELECT
        a.url,
        a.headline,
        a.published_at,
        a.fetched_at,
        a.created_at,
        s.commodity,
        s.score,
        s.reasoning,
        s.model,
        s.scored_at
    FROM recent a
    LEFT JOIN sentiment_scores s ON s.article_id = a.id
    ORDER BY COALESCE(a.published_at, a.fetched_at, a.created_at) DESC NULLS LAST,
             a.url,
             s.commodity
    """
)


def build_view(engine: Engine) -> PanelSentimentView:
    """Assemble the sentiment placeholder view model with a single read-only
    pass over ``sentiment_articles`` LEFT JOIN ``sentiment_scores``.

    Resolves the three honest states: a pre-migration / unreachable DB →
    UNAVAILABLE (``error=True``); zero rows → EMPTY (awaiting Writer-2); rows →
    POPULATED. Never writes, never 500s."""
    try:
        with engine.connect() as conn:
            rows = list(conn.execute(_ARTICLES_SQL, {"limit": _MAX_ARTICLES}))
    except (OperationalError, ProgrammingError):
        # DB unreachable (OperationalError) or the sentiment_* tables not yet
        # created (ProgrammingError, a pre-migration DB): one failing condition
        # must not 500 the dashboard (CLAUDE.md §4). Render the honest
        # UNAVAILABLE state — distinct from the expected EMPTY state — and never
        # log the DSN/credentials.
        logger.exception("Sentiment read failed; rendering data-unavailable state")
        return PanelSentimentView(articles=[], error=True)

    return PanelSentimentView(articles=group_articles(rows))


__all__ = [
    "build_view",
    "group_articles",
    "pick_timestamp",
    "format_score",
    "format_text",
    "format_timestamp",
    "ArticleRow",
    "ScoreRow",
    "PanelSentimentView",
]

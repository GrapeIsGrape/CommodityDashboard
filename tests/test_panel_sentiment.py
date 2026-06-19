"""Tests for the sentiment placeholder panel — dashboard/panels/panel_sentiment.py + route.

The pure presentation/logic helpers (three-state selection: empty / unavailable /
populated; article→scores LEFT-JOIN grouping incl. a zero-score article; NULL vs
a real-``0`` score discipline; timestamp fallback published→fetched→created;
formatting) are network-free and unit-tested directly. The render path uses a
fake engine (no live DB). A live-Postgres-or-skip integration test migrates to
head, reads the real (empty) sentiment tables, and — where feasible — seeds an
article + a zero-score row and asserts the populated render, then cleans up.
FastAPI/httpx/jinja2 are optional in the bare test env, so the route tests
importorskip them.
"""
import datetime as dt
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from common.config import get_database_url
from dashboard.panels import panel_sentiment
from dashboard.panels.panel_sentiment import (
    ArticleRow,
    PanelSentimentView,
    ScoreRow,
    format_score,
    format_text,
    format_timestamp,
    group_articles,
    pick_timestamp,
    safe_href,
)

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


# --- NULL vs real-0 score discipline (#22 AC5) ----------------------------

def test_format_score_null_is_dash():
    assert format_score(None) == "—"


def test_format_score_real_zero_is_not_dash():
    # Neutral sentiment 0 is a real signal — it must NOT collapse into NULL's em
    # dash. This is the headline #22 AC5 invariant.
    assert format_score(0) == "0"
    assert format_score(0.0) == "0"
    assert format_score(0) != format_score(None)


def test_format_score_trims_numeric_noise():
    assert format_score(0.5) == "0.5"
    assert format_score(-0.25) == "-0.25"
    assert format_score(1.0) == "1"


# --- Free-text NULL handling ----------------------------------------------

def test_format_text_null_and_blank_are_dash():
    assert format_text(None) == "—"
    assert format_text("   ") == "—"
    assert format_text("WTI tightening") == "WTI tightening"


# --- Timestamp formatting + fallback (#22 AC4/AC6) ------------------------

def test_format_timestamp_date_only_is_yyyy_mm_dd():
    assert format_timestamp(dt.datetime(2026, 6, 18, 0, 0, 0)) == "2026-06-18"


def test_format_timestamp_with_time_is_full_datetime():
    assert format_timestamp(dt.datetime(2026, 6, 18, 14, 30)) == "2026-06-18 14:30"


def test_format_timestamp_null_is_dash():
    assert format_timestamp(None) == "—"


# --- URL scheme allow-list (no javascript:/data: clickable links) ---------

def test_safe_href_allows_http_and_https():
    assert safe_href("https://example.com/x") == "https://example.com/x"
    assert safe_href("http://example.com/x") == "http://example.com/x"
    assert safe_href("HTTPS://Example.com") == "HTTPS://Example.com"


def test_safe_href_rejects_dangerous_schemes():
    # Untrusted Writer-2/LLM URL: a dangerous scheme must never become a link.
    assert safe_href("javascript:alert(1)") is None
    assert safe_href("data:text/html,<script>alert(1)</script>") is None
    assert safe_href("  javascript:alert(1)  ") is None
    assert safe_href("ftp://example.com/x") is None
    # A relative/scheme-less string is not a safe absolute href either.
    assert safe_href("//evil.com") is None
    assert safe_href("/relative/path") is None


def test_safe_href_null_and_blank_are_none():
    assert safe_href(None) is None
    assert safe_href("   ") is None


def test_group_sets_href_only_for_safe_url():
    bad = group_articles([_row(url="javascript:alert(1)", headline="x")])
    assert bad[0].url == "javascript:alert(1)"  # raw url still shown (honest)
    assert bad[0].href is None  # but not hyperlinked
    good = group_articles([_row(url="https://example.com/y", headline="y")])
    assert good[0].href == "https://example.com/y"


def test_pick_timestamp_prefers_published_then_fetched_then_created():
    pub = dt.datetime(2026, 6, 1, 9, 0)
    fetch = dt.datetime(2026, 6, 2, 9, 0)
    created = dt.datetime(2026, 6, 3, 9, 0)
    assert pick_timestamp(pub, fetch, created) == (pub, "published")
    assert pick_timestamp(None, fetch, created) == (fetch, "fetched")
    assert pick_timestamp(None, None, created) == (created, "created")
    assert pick_timestamp(None, None, None) == (None, "published")


# --- Pure grouping: article -> scores -------------------------------------

def _row(**kw):
    base = {
        "url": None, "headline": None, "published_at": None, "fetched_at": None,
        "created_at": None, "commodity": None, "score": None, "reasoning": None,
        "model": None, "scored_at": None,
    }
    base.update(kw)
    return base


def test_group_article_with_multiple_scores():
    rows = [
        _row(url="u1", headline="Crude builds", published_at=dt.datetime(2026, 6, 18),
             commodity="CL", score=-0.4, reasoning="bearish build", model="gpt-x"),
        _row(url="u1", headline="Crude builds", published_at=dt.datetime(2026, 6, 18),
             commodity="GC", score=0.1, reasoning="mild bid", model="gpt-x"),
    ]
    articles = group_articles(rows)
    assert len(articles) == 1
    art = articles[0]
    assert art.headline == "Crude builds"
    assert len(art.scores) == 2
    assert art.has_scores is True
    assert {s.commodity for s in art.scores} == {"CL", "GC"}


def test_group_article_with_zero_scores_renders_cleanly():
    # A LEFT-JOIN no-score row: all score columns NULL. Must yield one article
    # with an empty scores list, NOT a fabricated score row.
    rows = [_row(url="u2", headline="Unscored item", fetched_at=dt.datetime(2026, 6, 18))]
    articles = group_articles(rows)
    assert len(articles) == 1
    assert articles[0].has_scores is False
    assert articles[0].scores == []
    # Fell back to fetched_at since published_at is NULL.
    assert articles[0].timestamp == "2026-06-18"
    assert articles[0].timestamp_source == "fetched"


def test_group_real_zero_score_survives_grouping():
    rows = [_row(url="u3", headline="Neutral", commodity="NG", score=0,
                 reasoning="balanced", model="m1")]
    articles = group_articles(rows)
    assert articles[0].scores[0].score == "0"  # not "—".


def test_group_null_headline_and_reasoning_are_dash():
    rows = [_row(url="u4", headline=None, commodity="ZC", score=0.2,
                 reasoning=None, model="m1")]
    articles = group_articles(rows)
    assert articles[0].headline == "—"
    assert articles[0].scores[0].reasoning == "—"


def test_group_preserves_article_order():
    rows = [
        _row(url="newer", headline="N", published_at=dt.datetime(2026, 6, 18)),
        _row(url="older", headline="O", published_at=dt.datetime(2026, 6, 1)),
    ]
    assert [a.url for a in group_articles(rows)] == ["newer", "older"]


def test_group_empty_input_is_empty_list():
    assert group_articles([]) == []


# --- Three-state view properties ------------------------------------------

def test_view_empty_state_properties():
    view = PanelSentimentView(articles=[])
    assert view.is_empty is True
    assert view.is_unavailable is False
    assert view.is_populated is False


def test_view_unavailable_state_properties():
    view = PanelSentimentView(articles=[], error=True)
    assert view.is_unavailable is True
    assert view.is_empty is False
    assert view.is_populated is False


def test_view_populated_state_properties():
    view = PanelSentimentView(articles=[ArticleRow(url="u", href=None, headline="h",
                                                   timestamp="2026-06-18",
                                                   timestamp_source="published")])
    assert view.is_populated is True
    assert view.is_empty is False
    assert view.is_unavailable is False


# --- Render path: fake engine (no live DB) --------------------------------

class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, params=None):
        return list(self._rows)


class _FakeEngine:
    def __init__(self, rows):
        self._rows = rows

    def connect(self):
        return _FakeConn(self._rows)


def test_build_view_empty_is_not_error():
    view = panel_sentiment.build_view(_FakeEngine([]))
    assert view.is_empty is True
    assert view.error is False


def test_build_view_populated_groups_rows():
    rows = [_row(url="u1", headline="H", published_at=dt.datetime(2026, 6, 18),
                 commodity="CL", score=0, reasoning="r", model="m")]
    view = panel_sentiment.build_view(_FakeEngine(rows))
    assert view.is_populated is True
    assert view.articles[0].scores[0].score == "0"


# --- DB-failure isolation: never 500, honest UNAVAILABLE state ------------

class _RaisingConn:
    def __init__(self, exc):
        self._exc = exc

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, statement, params=None):
        raise self._exc


class _RaisingEngine:
    def __init__(self, exc):
        self._exc = exc

    def connect(self):
        return _RaisingConn(self._exc)


def _operational_error():
    return OperationalError("SELECT 1", {}, Exception("connection refused"))


def _programming_error():
    return ProgrammingError("SELECT ...", {}, Exception('relation "sentiment_articles" does not exist'))


def test_build_view_db_unreachable_returns_unavailable():
    view = panel_sentiment.build_view(_RaisingEngine(_operational_error()))
    assert view.is_unavailable is True
    assert view.articles == []


def test_build_view_missing_table_returns_unavailable_not_empty():
    # A pre-migration DB (ProgrammingError) must read as UNAVAILABLE, NOT the
    # expected EMPTY state — the two must never be conflated (#22 AC3).
    view = panel_sentiment.build_view(_RaisingEngine(_programming_error()))
    assert view.is_unavailable is True
    assert view.is_empty is False


# --- Static guard: no dashboard -> etl import (the #17 pattern) ------------

def test_panel_sentiment_does_not_import_etl():
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parents[1]
           / "dashboard" / "panels" / "panel_sentiment.py").read_text(encoding="utf-8")
    assert re.search(r"^\s*(from\s+etl[\s.]|import\s+etl[\s.]?)", src, re.MULTILINE) is None


# --- Route render (no live DB; fake engine via monkeypatch) ---------------

def _reload_main(monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("jinja2")
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
    import importlib

    import dashboard.main as dashboard_main

    importlib.reload(dashboard_main)
    return dashboard_main


def test_route_empty_state_awaiting_writer2(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    empty = PanelSentimentView(articles=[])
    monkeypatch.setattr(dashboard_main.panel_sentiment, "build_view", lambda *a, **k: empty)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/sentiment")

    assert resp.status_code == 200
    body = resp.text
    assert "Awaiting Writer-2" in body
    assert "no sentiment data yet" in body.lower()
    # The expected empty state is NOT the unavailable state. (The word
    # "unavailable" also appears in the embedded CSS class palette, so target the
    # human-readable unavailable message instead of the bare substring.)
    assert "currently <strong>unavailable</strong>" not in body


def test_route_unavailable_state_distinct_from_empty(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    errored = PanelSentimentView(articles=[], error=True)
    monkeypatch.setattr(dashboard_main.panel_sentiment, "build_view", lambda *a, **k: errored)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/sentiment")

    assert resp.status_code == 200
    body = resp.text
    assert "unavailable" in body.lower()
    assert "Awaiting Writer-2" not in body


def test_route_populated_renders_headline_url_score_reasoning_model(monkeypatch):
    dashboard_main = _reload_main(monkeypatch)
    from fastapi.testclient import TestClient

    article = ArticleRow(
        url="https://example.com/crude",
        href="https://example.com/crude",
        headline="Crude inventories build",
        timestamp="2026-06-18",
        timestamp_source="published",
        scores=[
            ScoreRow(commodity="CL", score="0", reasoning="balanced build vs expectations",
                     model="gpt-test", scored_at="2026-06-18"),
        ],
    )
    unscored = ArticleRow(url="https://example.com/unscored", href="https://example.com/unscored",
                          headline="Pending",
                          timestamp="2026-06-17", timestamp_source="fetched")
    view = PanelSentimentView(articles=[article, unscored])
    monkeypatch.setattr(dashboard_main.panel_sentiment, "build_view", lambda *a, **k: view)

    with TestClient(dashboard_main.app) as client:
        resp = client.get("/panel/sentiment")

    assert resp.status_code == 200
    body = resp.text
    assert "Crude inventories build" in body
    assert "https://example.com/crude" in body
    assert "balanced build vs expectations" in body  # reasoning surfaced (§5).
    assert "gpt-test" in body
    assert ">0<" in body or "0</td>" in body  # the real-0 score, not a dash.
    assert "Not yet scored" in body  # the zero-score article renders cleanly.


# --- Live-Postgres-or-skip integration (mirrors tests/test_health.py) -----

@pytest.fixture
def migrated_engine(monkeypatch):
    alembic_config = pytest.importorskip("alembic.config")
    alembic_command = pytest.importorskip("alembic.command")
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
    try:
        engine = create_engine(get_database_url())
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("No Postgres reachable for sentiment integration test")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = alembic_config.Config(os.path.join(repo_root, "migrations", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(repo_root, "migrations"))
    alembic_command.upgrade(cfg, "head")
    yield engine
    engine.dispose()


def test_live_empty_tables_read_empty_state(migrated_engine):
    # v1 reality: the sentiment_* tables exist but are empty → EMPTY state, not
    # an error. (Don't truncate — just assert the read doesn't error; a populated
    # DB would still be a valid non-error read.)
    view = panel_sentiment.build_view(migrated_engine)
    assert view.error is False


def test_live_seeded_article_and_zero_score(migrated_engine):
    url = "https://example.test/panel-sentiment-itest"
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM sentiment_articles WHERE url = :u"), {"u": url})
        row = conn.execute(
            text(
                "INSERT INTO sentiment_articles (url, headline, published_at, source) "
                "VALUES (:u, :h, :p, 'itest') RETURNING id"
            ),
            {"u": url, "h": "ITest crude", "p": dt.datetime(2026, 6, 18)},
        ).first()
        article_id = row[0]
        conn.execute(
            text(
                "INSERT INTO sentiment_scores (article_id, commodity, score, reasoning, model) "
                "VALUES (:a, 'CL', 0, 'neutral', 'itest-model')"
            ),
            {"a": article_id},
        )
    try:
        view = panel_sentiment.build_view(migrated_engine)
        assert view.is_populated is True
        seeded = next(a for a in view.articles if a.url == url)
        assert seeded.headline == "ITest crude"
        assert seeded.timestamp == "2026-06-18"
        assert len(seeded.scores) == 1
        # A real 0 score must render as "0", not the NULL em dash.
        assert seeded.scores[0].score == "0"
        assert seeded.scores[0].reasoning == "neutral"
        assert seeded.scores[0].model == "itest-model"
    finally:
        with migrated_engine.begin() as conn:
            conn.execute(text("DELETE FROM sentiment_articles WHERE url = :u"), {"u": url})

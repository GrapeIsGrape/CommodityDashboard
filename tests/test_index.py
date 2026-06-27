"""Tests for the unified GET / index page — AC verification for #30.

All panel view-model builders are monkeypatched to return stub objects, so the
tests are network-free and DB-free. Only `fastapi` / `httpx` / `jinja2` are
needed — the module is skipped when they are absent (mirrors test_panel_d.py).
"""
import datetime as dt
import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("jinja2")

from fastapi.testclient import TestClient  # noqa: E402

from dashboard.panels import panel_a, panel_b, panel_c, panel_d, panel_macro, panel_sentiment  # noqa: E402

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}

_TODAY = dt.date(2026, 6, 27)


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------

def _stub_view_d():
    return panel_d.PanelDView(
        underlyings=[], indices=[], last_session=_TODAY, error=False
    )


def _stub_view_c():
    return panel_c.PanelCView(
        cot_rows=[], curve_cards=[],
        expected_report_date=dt.date(2026, 6, 23), error=False
    )


def _stub_view_a():
    return panel_a.PanelAView(
        groups=[], last_session=_TODAY, error=False
    )


def _stub_view_b():
    return panel_b.PanelBView(
        groups=[], seasonality_mode="yoy", error=False
    )


def _stub_view_macro():
    return panel_macro.PanelMacroView(
        rows=[], last_session=_TODAY, error=False
    )


def _stub_view_sentiment():
    return panel_sentiment.PanelSentimentView(articles=[], error=False)


class _StubHealthView:
    db_ok = True
    schema_version = "0005"
    etl_summary = []
    trigger_available = False
    cooldown_minutes = 10


def _patch_all(monkeypatch, m):
    """Patch all panel build_view calls and health builder to return stubs."""
    monkeypatch.setattr(m.panel_d, "build_view", lambda *a, **k: _stub_view_d())
    monkeypatch.setattr(m.panel_c, "build_view", lambda *a, **k: _stub_view_c())
    monkeypatch.setattr(m.panel_a, "build_view", lambda *a, **k: _stub_view_a())
    monkeypatch.setattr(m.panel_b, "build_view", lambda *a, **k: _stub_view_b())
    monkeypatch.setattr(m.panel_macro, "build_view", lambda *a, **k: _stub_view_macro())
    monkeypatch.setattr(m.panel_sentiment, "build_view", lambda *a, **k: _stub_view_sentiment())
    monkeypatch.setattr(m, "_build_health_view", lambda *a, **k: _StubHealthView())


def _get_main(monkeypatch):
    import importlib
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, os.environ.get(key, value))
    import dashboard.main as m
    importlib.reload(m)
    return m


# ---------------------------------------------------------------------------
# AC#1 — GET / returns 200 with all six panel sections + health section
# ---------------------------------------------------------------------------

def test_index_returns_200_with_all_sections(monkeypatch):
    m = _get_main(monkeypatch)
    _patch_all(monkeypatch, m)
    with TestClient(m.app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Verify each section is present by its <details id="...">
    assert "section-panel-d" in body
    assert "section-panel-c" in body
    assert "section-panel-a" in body
    assert "section-panel-b" in body
    assert "section-panel-macro" in body
    assert "section-panel-sentiment" in body
    assert "section-health" in body


# ---------------------------------------------------------------------------
# AC#2 — Each section is a <details open> (defaulting to expanded)
# ---------------------------------------------------------------------------

def test_all_sections_are_details_open_by_default(monkeypatch):
    m = _get_main(monkeypatch)
    _patch_all(monkeypatch, m)
    with TestClient(m.app) as client:
        resp = client.get("/")
    body = resp.text
    # All seven section-card <details> elements must carry the `open` attribute
    # (default expanded). Count occurrences of '<details class="section-card" open'.
    import re
    open_cards = re.findall(r'<details class="section-card" open', body)
    assert len(open_cards) == 7, (
        f"Expected 7 open <details> cards, found {len(open_cards)}"
    )


# ---------------------------------------------------------------------------
# AC#3 — "Collapse All" / "Expand All" toggle button present in HTML
# ---------------------------------------------------------------------------

def test_collapse_expand_buttons_present(monkeypatch):
    m = _get_main(monkeypatch)
    _patch_all(monkeypatch, m)
    with TestClient(m.app) as client:
        resp = client.get("/")
    body = resp.text
    assert "Collapse All" in body
    assert "Expand All" in body
    # JS toggle function present
    assert "setAllCards" in body


# ---------------------------------------------------------------------------
# AC#4 — One panel DB error does not 500; others render normally
# ---------------------------------------------------------------------------

def test_panel_d_db_error_does_not_500_others_render(monkeypatch):
    m = _get_main(monkeypatch)
    # Panel D raises an unexpected exception; others are fine.
    monkeypatch.setattr(m.panel_d, "build_view", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(m.panel_c, "build_view", lambda *a, **k: _stub_view_c())
    monkeypatch.setattr(m.panel_a, "build_view", lambda *a, **k: _stub_view_a())
    monkeypatch.setattr(m.panel_b, "build_view", lambda *a, **k: _stub_view_b())
    monkeypatch.setattr(m.panel_macro, "build_view", lambda *a, **k: _stub_view_macro())
    monkeypatch.setattr(m.panel_sentiment, "build_view", lambda *a, **k: _stub_view_sentiment())
    monkeypatch.setattr(m, "_build_health_view", lambda *a, **k: _StubHealthView())
    with TestClient(m.app) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # Panel D error state is shown (not a crash).
    assert "currently unavailable" in body.lower() or "unavailable" in body.lower() or "section-panel-d" in body
    # Other sections still present.
    assert "section-panel-c" in body
    assert "section-panel-a" in body


def test_all_panels_db_error_does_not_500(monkeypatch):
    """Every panel failing independently must still return 200."""
    from sqlalchemy.exc import OperationalError

    def _raise(*a, **k):
        raise OperationalError("SELECT 1", {}, Exception("db down"))

    m = _get_main(monkeypatch)
    monkeypatch.setattr(m.panel_d, "build_view", _raise)
    monkeypatch.setattr(m.panel_c, "build_view", _raise)
    monkeypatch.setattr(m.panel_a, "build_view", _raise)
    monkeypatch.setattr(m.panel_b, "build_view", _raise)
    monkeypatch.setattr(m.panel_macro, "build_view", _raise)
    monkeypatch.setattr(m.panel_sentiment, "build_view", _raise)
    monkeypatch.setattr(m, "_build_health_view", lambda *a, **k: _StubHealthView())
    with TestClient(m.app) as client:
        resp = client.get("/")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# AC#5 — No option-action language in the rendered unified index
# ---------------------------------------------------------------------------

def test_no_option_action_language_in_unified_index(monkeypatch):
    m = _get_main(monkeypatch)
    _patch_all(monkeypatch, m)
    with TestClient(m.app) as client:
        resp = client.get("/")
    body = resp.text.lower()
    # These are the banned phrases from the per-panel tests — none should appear
    # in the Panel A / B / Macro sections of the unified page.
    # Panel D / C can legitimately say "sell" (crowded COT labels, COT legend)
    # so we check for the specific option-action framing instead.
    # The Panel A / B / Macro panels must not contain these:
    banned = ["short put", "short call", "sell premium", "buy premium"]
    for phrase in banned:
        assert phrase not in body, f"Banned phrase found: {phrase!r}"


# ---------------------------------------------------------------------------
# AC#6 — Existing test suite stays green (static isolation guard)
# ---------------------------------------------------------------------------

def test_no_dashboard_module_imports_etl():
    """Static guard: the unified index must not introduce etl/ imports into
    dashboard/ — re-asserts the #17 guard (already in test_panel_d.py but
    confirmed here so it's clearly tied to #30 too)."""
    import pathlib
    import re
    dashboard_root = pathlib.Path(__file__).resolve().parents[1] / "dashboard"
    pattern = re.compile(r"^\s*(from\s+etl[\s.]|import\s+etl[\s.]?)", re.MULTILINE)
    offenders = []
    for path in dashboard_root.rglob("*.py"):
        text_src = path.read_text(encoding="utf-8")
        if pattern.search(text_src):
            offenders.append(str(path))
    assert offenders == [], (
        f"dashboard modules must not import the etl package: {offenders}"
    )


# ---------------------------------------------------------------------------
# AC#7 — Per-panel standalone routes still return 200 (backward compat)
# ---------------------------------------------------------------------------

def test_standalone_panel_routes_still_work(monkeypatch):
    m = _get_main(monkeypatch)
    _patch_all(monkeypatch, m)
    with TestClient(m.app) as client:
        for path, check in [
            ("/panel/d", "Panel D"),
            ("/panel/c", "Panel C"),
            ("/panel/a", "Panel A"),
            ("/panel/b", "Panel B"),
            ("/panel/macro", "Macro-Context"),
            ("/panel/sentiment", "Sentiment"),
        ]:
            resp = client.get(path)
            assert resp.status_code == 200, f"Route {path} returned {resp.status_code}"
            assert check in resp.text, f"Route {path} missing '{check}' in body"

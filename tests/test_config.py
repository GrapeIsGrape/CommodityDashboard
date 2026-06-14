import pytest

from common.config import get_database_url, load_symbols

_DB_ENV = {
    "POSTGRES_USER": "commodity",
    "POSTGRES_PASSWORD": "change_me",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "POSTGRES_DB": "commodity",
}


@pytest.fixture
def db_env(monkeypatch):
    for key, value in _DB_ENV.items():
        monkeypatch.setenv(key, value)


def test_database_url_built_from_env(db_env):
    url = get_database_url()
    assert url.drivername == "postgresql+psycopg2"
    assert url.username == "commodity"
    assert url.host == "postgres"
    assert url.port == 5432
    assert url.database == "commodity"


def test_database_url_host_and_port_default(monkeypatch):
    for key in ("POSTGRES_HOST", "POSTGRES_PORT"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("POSTGRES_USER", "u")
    monkeypatch.setenv("POSTGRES_PASSWORD", "p")
    monkeypatch.setenv("POSTGRES_DB", "d")
    url = get_database_url()
    assert url.host == "postgres"
    assert url.port == 5432


def test_database_url_escapes_special_chars(db_env, monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss:w/rd?")
    url = get_database_url()
    # The URL object keeps the raw password; rendering escapes it.
    assert url.password == "p@ss:w/rd?"
    rendered = url.render_as_string(hide_password=False)
    assert "p%40ss%3Aw%2Frd%3F" in rendered


def test_database_url_missing_required_var(monkeypatch):
    for key in _DB_ENV:
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(KeyError):
        get_database_url()


def test_load_symbols_has_expected_structure():
    symbols = load_symbols()
    assert "commodities" in symbols
    assert "macro_context" in symbols
    macro = {m["symbol"] for m in symbols["macro_context"]}
    assert {"TLT", "VTI", "QQQ"} <= macro


def test_load_symbols_proxy_mapping():
    symbols = load_symbols()
    proxies = {
        entry["future"]: entry["iv_proxy"]
        for group in symbols["commodities"].values()
        for entry in group
    }
    assert proxies["GC"] == "GLD"
    assert proxies["SI"] == "SLV"
    assert proxies["CL"] == "USO"
    assert proxies["NG"] == "UNG"

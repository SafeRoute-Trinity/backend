"""PostGIS URL resolution in DatabaseFactory."""

import pytest

from libs.db import DatabaseFactory


@pytest.fixture
def clear_postgis_urls(monkeypatch):
    for key in ("POSTGIS_DATABASE_URL", "GEO_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)


def test_geo_database_url_fallback_psycopg2_to_asyncpg(monkeypatch, clear_postgis_urls):
    monkeypatch.setenv(
        "GEO_DATABASE_URL",
        "postgresql+psycopg2://u:pw@postgis-svc:5432/saferoute_geo",
    )
    factory = DatabaseFactory()
    cfg = factory._create_postgis_config()
    assert cfg.database_url.startswith("postgresql+asyncpg://")
    assert "postgis-svc" in cfg.database_url
    assert "/saferoute_geo" in cfg.database_url.replace("***", "")


def test_postgis_database_url_wins_over_geo(monkeypatch):
    monkeypatch.setenv(
        "POSTGIS_DATABASE_URL",
        "postgresql://a:b@primary-db:5432/winner",
    )
    monkeypatch.setenv(
        "GEO_DATABASE_URL",
        "postgresql://a:b@geo-db:5432/loser",
    )
    factory = DatabaseFactory()
    cfg = factory._create_postgis_config()
    assert "primary-db" in cfg.database_url
    assert "winner" in cfg.database_url


def test_postgres_fallback_uses_postgis_database_name(monkeypatch, clear_postgis_urls):
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "shared-pg.internal")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGIS_DATABASE", "my_geo_db")
    factory = DatabaseFactory()
    cfg = factory._create_postgis_config()
    assert cfg.database == "my_geo_db"
    assert cfg.host == "shared-pg.internal"

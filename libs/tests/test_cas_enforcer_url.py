"""Unit tests for CAS enforcer DB URL resolution (no live DB)."""

from __future__ import annotations

import pytest

from libs.cas_enforcer import resolve_cas_database_url


@pytest.fixture
def clear_db_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "CAS_DATABASE_URL",
        "DATABASE_URL",
        "POSTGIS_DATABASE_URL",
        "POSTGIS_HOST",
        "POSTGIS_PORT",
        "POSTGIS_USER",
        "POSTGIS_PASSWORD",
        "POSTGIS_DATABASE",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DATABASE",
    ):
        monkeypatch.delenv(key, raising=False)


def test_postgis_host_wins_over_database_url(
    monkeypatch: pytest.MonkeyPatch, clear_db_env: None
) -> None:
    """Safety-scoring: ways on PostGIS; CAS must not follow DATABASE_URL alone."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:secret@db.internal:5432/appdb")
    monkeypatch.setenv("POSTGIS_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGIS_PORT", "5433")
    monkeypatch.setenv("POSTGIS_USER", "saferoute")
    monkeypatch.setenv("POSTGIS_PASSWORD", "pw")
    monkeypatch.setenv("POSTGIS_DATABASE", "saferoute_geo")

    url = resolve_cas_database_url("")
    assert "127.0.0.1:5433" in url
    assert "saferoute_geo" in url
    assert "db.internal" not in url


def test_cas_database_url_wins_over_everything(
    monkeypatch: pytest.MonkeyPatch, clear_db_env: None
) -> None:
    monkeypatch.setenv("CAS_DATABASE_URL", "postgresql://cas:cas@casdb:5432/casdb")
    monkeypatch.setenv("POSTGIS_HOST", "127.0.0.1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:app@other:5432/other")

    url = resolve_cas_database_url("")
    assert "postgresql+asyncpg://" in url
    assert "casdb" in url


def test_database_url_when_no_postgis(monkeypatch: pytest.MonkeyPatch, clear_db_env: None) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost:5432/saferoute")

    url = resolve_cas_database_url("")
    assert "postgresql+asyncpg://" in url
    assert "localhost:5432" in url


def test_explicit_arg_wins(monkeypatch: pytest.MonkeyPatch, clear_db_env: None) -> None:
    monkeypatch.setenv("CAS_DATABASE_URL", "postgresql://wrong:wrong@wrong:5432/wrong")

    url = resolve_cas_database_url("postgresql://explicit:explicit@explicit:5432/explicit")
    assert "explicit" in url
    assert "wrong" not in url

"""Unit tests for server lifespan assembly (all external effects mocked).

These tests verify that the MCP server wires up multi-database pools,
per-database executors, the configured SQL validator and the rate limiter.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

import pg_mcp.server as server_module
from pg_mcp.config.settings import reset_settings
from pg_mcp.models.schema import DatabaseSchema
from pg_mcp.server import lifespan


@pytest.fixture(autouse=True)
def server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configure a two-database environment and reset settings after the test."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DATABASE_NAME", "maindb")
    monkeypatch.setenv(
        "DATABASES",
        '[{"name": "secondary", "host": "other-host"}]',
    )
    monkeypatch.setenv("SECURITY_BLOCKED_TABLES", "salaries")
    monkeypatch.setenv("SECURITY_BLOCKED_COLUMNS", "password_hash")
    monkeypatch.setenv("SECURITY_ALLOW_EXPLAIN", "true")
    yield
    reset_settings()


@pytest.fixture
def created_pools(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Mock pool creation, returning the pools created per database."""
    pools: dict[str, MagicMock] = {}

    async def fake_create_pool(config: Any) -> MagicMock:
        pool = MagicMock()
        pool._db_name = config.name
        pools[config.name] = pool
        return pool

    monkeypatch.setattr(server_module, "create_pool", fake_create_pool)
    return pools


@pytest.fixture
def closed_pools(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Mock pool shutdown, recording which databases were closed."""
    closed: list[str] = []

    async def fake_close(
        pools: dict[str, Any],
        timeout: float = 10.0,  # noqa: ASYNC109
    ) -> None:
        closed.extend(pools.keys())

    monkeypatch.setattr(server_module, "close_pools", fake_close)
    return closed


@pytest.fixture
def mock_schema_load(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Mock schema cache loading for all databases."""
    loaded: list[str] = []

    def load(self: Any, db_name: str, pool: Any) -> Any:
        loaded.append(db_name)

        async def _load() -> DatabaseSchema:
            return DatabaseSchema(database_name=db_name, tables=[], version="15.0")

        return _load()

    monkeypatch.setattr(server_module.SchemaCache, "load", load)
    return loaded


class TestLifespan:
    """Tests for server startup and shutdown wiring."""

    @pytest.mark.asyncio
    async def test_multi_database_assembly(
        self,
        created_pools: dict[str, MagicMock],
        closed_pools: list[str],
        mock_schema_load: list[str],
    ) -> None:
        """Test pools, executors, validator and rate limiter are built per config."""
        async with lifespan(MagicMock()):
            assert set(created_pools.keys()) == {"maindb", "secondary"}
            assert sorted(mock_schema_load) == ["maindb", "secondary"]

            orchestrator = server_module._orchestrator
            assert orchestrator is not None
            # One executor per database
            assert set(orchestrator.sql_executors.keys()) == {"maindb", "secondary"}
            # Pools are wired through to the orchestrator
            assert set(orchestrator.pools.keys()) == {"maindb", "secondary"}
            # Rate limiter is injected and sized from configuration
            assert orchestrator.rate_limiter is not None
            assert orchestrator.rate_limiter.query_limiter.max_concurrent == 10
            assert orchestrator.rate_limiter.llm_limiter.max_concurrent == 5

            # Validator is configured from security settings
            validator = orchestrator.sql_validator
            assert validator.blocked_tables == {"salaries"}
            assert validator.blocked_columns == {"password_hash"}
            assert validator.allow_explain is True

        # Shutdown closes all pools
        assert sorted(closed_pools) == ["maindb", "secondary"]

    @pytest.mark.asyncio
    async def test_single_database_backward_compatible(
        self,
        monkeypatch: pytest.MonkeyPatch,
        created_pools: dict[str, MagicMock],
        closed_pools: list[str],
        mock_schema_load: list[str],
    ) -> None:
        """Test that without DATABASES only the primary database is created."""
        monkeypatch.delenv("DATABASES")

        async with lifespan(MagicMock()):
            assert set(created_pools.keys()) == {"maindb"}
            assert set(server_module._orchestrator.sql_executors.keys()) == {"maindb"}

        assert closed_pools == ["maindb"]

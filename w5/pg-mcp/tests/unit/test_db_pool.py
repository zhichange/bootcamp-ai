"""Unit tests for database connection pool management (mocked asyncpg)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pg_mcp.config.settings import DatabaseConfig
from pg_mcp.db.pool import close_pools, create_pool, create_pools


def _db_config(name: str) -> DatabaseConfig:
    """Build a database config for testing."""
    return DatabaseConfig(name=name, host="localhost", user="postgres", password="pw")


class TestCreatePool:
    """Tests for pool creation."""

    @pytest.mark.asyncio
    async def test_create_pool_passes_config(self) -> None:
        """Test create_pool forwards configuration to asyncpg."""
        mock_pool = MagicMock()
        mock_create = AsyncMock(return_value=mock_pool)
        with patch("pg_mcp.db.pool.asyncpg.create_pool", new=mock_create):
            config = _db_config("mydb")
            pool = await create_pool(config)

        assert pool is mock_pool
        mock_create.assert_awaited_once_with(
            host="localhost",
            port=5432,
            database="mydb",
            user="postgres",
            password="pw",
            min_size=5,
            max_size=20,
            timeout=30.0,
            command_timeout=30.0,
        )


class TestCreatePools:
    """Tests for multi-database pool creation."""

    @pytest.mark.asyncio
    async def test_create_pools_maps_names(self) -> None:
        """Test pools are keyed by database name."""
        pools_by_name = {"db1": MagicMock(), "db2": MagicMock()}

        async def fake_create_pool(config: DatabaseConfig) -> MagicMock:
            return pools_by_name[config.name]

        with patch("pg_mcp.db.pool.create_pool", new=fake_create_pool):
            pools = await create_pools([_db_config("db1"), _db_config("db2")])

        assert set(pools.keys()) == {"db1", "db2"}


class TestClosePools:
    """Tests for graceful pool shutdown."""

    @pytest.mark.asyncio
    async def test_graceful_close(self) -> None:
        """Test pools are closed gracefully when fast."""
        pool = MagicMock()
        pool.close = AsyncMock()
        await close_pools({"db1": pool}, timeout=1.0)
        pool.close.assert_awaited_once()
        pool.terminate.assert_not_called()

    @pytest.mark.asyncio
    async def test_terminate_on_timeout(self) -> None:
        """Test pools are terminated when graceful close times out."""

        async def slow_close() -> None:
            import asyncio

            await asyncio.sleep(5)

        pool = MagicMock()
        pool.close = slow_close
        await close_pools({"db1": pool}, timeout=0.05)
        pool.terminate.assert_called_once()

    @pytest.mark.asyncio
    async def test_terminate_on_error_and_continue(self) -> None:
        """Test an error closing one pool does not stop the others."""
        bad_pool = MagicMock()
        bad_pool.close = AsyncMock(side_effect=RuntimeError("boom"))
        good_pool = MagicMock()
        good_pool.close = AsyncMock()

        await close_pools({"bad": bad_pool, "good": good_pool}, timeout=1.0)

        bad_pool.terminate.assert_called_once()
        good_pool.close.assert_awaited_once()
        good_pool.terminate.assert_not_called()

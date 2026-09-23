"""Feature demonstration script for pg-mcp.

Runs the full query pipeline with mocked LLM/executors to demonstrate,
without any external dependency (no PostgreSQL / no OpenAI key):

1. Multi-database configuration via DATABASES (JSON) + per-database routing
2. Config-driven security: blocked tables / blocked columns / EXPLAIN policy
3. Concurrency rate limiting (RATE_LIMITED responses)
4. Exponential backoff between SQL generation retries
5. Prometheus metrics instrumentation

Usage:
    uv run python scripts/demo_features.py            # run demo, print evidence
    uv run python scripts/demo_features.py --metrics  # also serve /metrics on :9090
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Demo does not call the real OpenAI API; the key only satisfies validation.
os.environ.setdefault("OPENAI_API_KEY", "sk-demo-not-used")

from prometheus_client import REGISTRY, start_http_server

from pg_mcp.config.settings import Settings, ValidationConfig
from pg_mcp.models.query import QueryRequest, ReturnType
from pg_mcp.observability.tracing import get_request_id
from pg_mcp.resilience.rate_limiter import MultiRateLimiter
from pg_mcp.services.orchestrator import QueryOrchestrator
from pg_mcp.services.sql_executor import SQLExecutor
from pg_mcp.services.sql_generator import SQLGenerator
from pg_mcp.services.sql_validator import SQLValidator

BANNER = "=" * 72


def section(title: str) -> None:
    """Print a section banner."""
    print()
    print(BANNER)
    print(f"  {title}")
    print(BANNER)


def build_orchestrator(settings: Settings) -> tuple[QueryOrchestrator, dict[str, str]]:
    """Build a fully-wired orchestrator with fake DB pools and executors.

    Returns:
        tuple: (orchestrator, executor_log) where executor_log records which
        executor served each request (simulating per-database routing).
    """
    # Fake per-database connection pools (no real PostgreSQL needed)
    pools: dict[str, MagicMock] = {}
    for db in settings.all_databases:
        pool = MagicMock()
        pool._db_name = db.name
        pools[db.name] = pool

    # Schema cache: in-memory, pre-loaded for every database
    schema_cache = MagicMock()
    schema_cache.get.side_effect = lambda db: MagicMock(
        database_name=db, tables=[MagicMock() for _ in range(3)]
    )

    # LLM generator: fake, returns valid SQL (configurable per demo)
    generator = MagicMock(spec=SQLGenerator)

    async def fake_generate(**_kwargs: object) -> str:
        return "SELECT id, name FROM users"

    generator.generate.side_effect = fake_generate

    # Per-database executors backed by their own pool
    executor_log: dict[str, str] = {}
    executors: dict[str, SQLExecutor] = {}
    for db in settings.all_databases:
        executor = MagicMock(spec=SQLExecutor)

        async def fake_execute(sql: str, _db: str = db.name) -> tuple[list[dict], int]:
            executor_log[_db] = executor_log.get(_db, 0) + 1  # type: ignore[operator]
            return ([{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}], 2)

        executor.execute.side_effect = fake_execute
        executors[db.name] = executor

    # Config-driven security validator + concurrency rate limiter
    validator = SQLValidator(config=settings.security)
    rate_limiter = MultiRateLimiter(
        query_limit=settings.resilience.max_concurrent_queries,
        llm_limit=settings.resilience.max_concurrent_llm_calls,
    )

    orchestrator = QueryOrchestrator(
        sql_generator=generator,
        sql_validator=validator,
        sql_executors=executors,
        result_validator=MagicMock(),
        schema_cache=schema_cache,
        pools=pools,
        resilience_config=settings.resilience,
        validation_config=ValidationConfig(enabled=False),
        rate_limiter=rate_limiter,
        rate_limit_timeout=0.2,
    )
    return orchestrator, executor_log


def demo_multi_database() -> QueryOrchestrator:
    """Demo 1: DATABASES JSON config + per-database executor routing."""
    section("1. 多数据库支持：DATABASES JSON 配置 + 按库路由执行器")

    os.environ["DATABASE_NAME"] = "maindb"
    os.environ["DATABASES"] = (
        '[{"name": "sales", "host": "localhost"}, {"name": "archive", "host": "db-arch"}]'
    )
    settings = Settings()
    print(f"  配置的主库:      {settings.database.name}")
    print(f"  DATABASES 附加库: {[db.name for db in settings.additional_databases]}")
    print(f"  全部可用数据库:   {[db.name for db in settings.all_databases]}")

    orchestrator, _log = build_orchestrator(settings)

    # a) 请求指定 sales 库 → 路由到 sales 的执行器
    resp = asyncio.run(orchestrator.execute_query(QueryRequest(question="q", database="sales")))
    print(f"\n  [a] 指定 database='sales'      -> success={resp.success}")
    print(f"      响应中 generated_sql={resp.generated_sql!r}  (由 sales 执行器执行)")

    # b) 请求不存在的库 → 明确报错并列出可用库
    resp = asyncio.run(orchestrator.execute_query(QueryRequest(question="q", database="wrongdb")))
    print(f"\n  [b] 指定 database='wrongdb'    -> success={resp.success}")
    err = resp.error
    print(f"      error.code={err.code if err else None}")
    print(f"      error.message={err.message if err else None}")
    available = err.details.get("available_databases") if err else None
    print(f"      available_databases={available}")

    # c) 多库但未指定 → 要求显式指定
    resp = asyncio.run(orchestrator.execute_query(QueryRequest(question="q", database=None)))
    print(f"\n  [c] 未指定 database(多库环境)  -> success={resp.success}")
    print(f"      error.message={resp.error.message if resp.error else None}")
    return orchestrator


def demo_security() -> None:
    """Demo 2: config-driven blocked tables / columns / EXPLAIN policy."""
    section("2. 安全控制：配置化封禁表 / 封禁列 / EXPLAIN 策略")

    os.environ["SECURITY_BLOCKED_TABLES"] = "salaries, audit_log"
    os.environ["SECURITY_BLOCKED_COLUMNS"] = "password_hash"
    os.environ["SECURITY_ALLOW_EXPLAIN"] = "false"
    settings = Settings()
    validator = SQLValidator(config=settings.security)
    print(f"  SECURITY_BLOCKED_TABLES={settings.security.blocked_tables}")
    print(f"  SECURITY_BLOCKED_COLUMNS={settings.security.blocked_columns}")
    print(f"  SECURITY_ALLOW_EXPLAIN={settings.security.allow_explain}")

    cases = [
        ("SELECT * FROM users", "正常查询"),
        ("SELECT * FROM salaries", "访问封禁表 salaries"),
        (
            "SELECT u.name FROM users u JOIN audit_log a ON u.id = a.user_id",
            "JOIN 封禁表 audit_log",
        ),
        ("SELECT id, password_hash FROM users", "访问封禁列 password_hash"),
        ("EXPLAIN SELECT * FROM users", "EXPLAIN(默认禁止)"),
    ]
    for sql, desc in cases:
        is_valid, error = validator.validate(sql)
        status = "PASS" if is_valid else "REJECTED"
        reason = "" if is_valid else f"  原因: {error}"
        print(f"\n  [{status}] {desc}\n        SQL: {sql}{reason}")

    print("\n  -- 打开 SECURITY_ALLOW_EXPLAIN=true 后 --")
    os.environ["SECURITY_ALLOW_EXPLAIN"] = "true"
    validator = SQLValidator(config=Settings().security)
    is_valid, error = validator.validate("EXPLAIN SELECT * FROM users")
    print(
        f"  [{'PASS' if is_valid else 'REJECTED'}] EXPLAIN(已允许)"
        f"{'' if is_valid else f'  原因: {error}'}"
    )


def demo_rate_limit() -> None:
    """Demo 3: concurrency rate limiting produces RATE_LIMITED errors."""
    section("3. 弹性-限流：并发打满后请求被拒绝 (rate_limit_exceeded)")

    settings = Settings()
    settings.resilience.max_concurrent_queries = 1
    settings.resilience.max_concurrent_llm_calls = 1
    orchestrator, _log = build_orchestrator(settings)

    async def run() -> None:
        limiter = orchestrator.rate_limiter
        assert limiter is not None
        # Simulate one request already holding the only slot
        await limiter.query_limiter.acquire()
        print("  当前并发查询槽位: 1/1 已占用")
        resp = await orchestrator.execute_query(QueryRequest(question="q", database="maindb"))
        print(f"  新请求 -> success={resp.success}")
        print(f"  error.code={resp.error.code if resp.error else None}")
        print(f"  error.message={resp.error.message if resp.error else None}")
        limiter.query_limiter.release()
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(run())


def demo_backoff() -> None:
    """Demo 4: exponential backoff between generation retries."""
    section("4. 弹性-重试退避：验证失败后按指数退避重试")

    settings = Settings()
    settings.resilience.max_retries = 3
    settings.resilience.retry_delay = 0.2
    settings.resilience.backoff_factor = 2.0
    orchestrator, _log = build_orchestrator(settings)

    # LLM 先两次生成不安全 SQL，触发重试
    attempts: list[str] = ["DELETE FROM users", "DROP TABLE users", "SELECT 1", "SELECT 1"]
    gen = orchestrator.sql_generator
    gen.generate.side_effect = None

    async def flaky_generate(**_kwargs: object) -> str:
        return attempts.pop(0)

    gen.generate.side_effect = flaky_generate

    validator = orchestrator.sql_validator
    original_validate = validator.validate_or_raise

    def strict_validate(sql: str) -> None:
        if not sql.upper().startswith("SELECT"):
            from pg_mcp.models.errors import SecurityViolationError

            raise SecurityViolationError(f"{sql.split()[0]} not allowed")
        original_validate(sql)

    validator.validate_or_raise = strict_validate  # type: ignore[method-assign]

    async def run() -> tuple[str, list[float]]:
        delays: list[float] = []
        real_sleep = asyncio.sleep

        async def timed_sleep(delay: float) -> None:
            delays.append(delay)
            await real_sleep(delay)

        import pg_mcp.services.orchestrator as orch_mod

        orch_mod.asyncio.sleep = timed_sleep  # type: ignore[assignment]
        sql, _validation, _tokens = await orchestrator._generate_sql_with_retry(
            question="q", schema=MagicMock(), request_id="demo"
        )
        orch_mod.asyncio.sleep = real_sleep  # type: ignore[assignment]
        return sql, delays

    sql, delays = asyncio.run(run())
    print("  max_retries=3, retry_delay=0.2s, backoff_factor=2.0")
    print(f"  第1次生成 DELETE(被拒) -> 退避 {delays[0]:.2f}s")
    print(f"  第2次生成 DROP(被拒)   -> 退避 {delays[1]:.2f}s")
    print(f"  第3次生成 SELECT(通过)，最终 SQL: {sql}")
    print(f"  实测退避序列: {[f'{d:.2f}' for d in delays]} (期望 [0.20, 0.40])")


def demo_metrics(orchestrator: QueryOrchestrator) -> None:
    """Demo 5: Prometheus metrics captured by the pipeline."""
    section("5. 可观测性-指标：Prometheus pg_mcp_* 指标")

    async def fire() -> None:
        await orchestrator.execute_query(
            QueryRequest(question="q", database="maindb", return_type=ReturnType.SQL)
        )
        await orchestrator.execute_query(QueryRequest(question="q", database="sales"))

    asyncio.run(fire())

    wanted = {
        "pg_mcp_query_requests_total",
        "pg_mcp_sql_rejected_total",
        "pg_mcp_llm_calls_total",
        "pg_mcp_query_duration_seconds_count",
    }
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name in wanted:
                labels = ",".join(f"{k}={v}" for k, v in sample.labels.items())
                label_str = f"{{{labels}}}" if labels else ""
                print(f"  {sample.name}{label_str} = {sample.value}")

    request_id = get_request_id()
    print(f"\n  (链路追踪 request_id 已贯穿请求生命周期，本次上下文: {request_id})")


def main() -> None:
    """Run all demonstrations."""
    import logging

    logging.getLogger().setLevel(logging.CRITICAL)

    print(BANNER)
    print("  pg-mcp 功能演示: 多数据库 / 安全控制 / 限流 / 退避 / 指标")
    print(BANNER)

    orchestrator = demo_multi_database()
    demo_security()
    demo_rate_limit()
    demo_backoff()
    demo_metrics(orchestrator)

    if "--metrics" in sys.argv:
        import contextlib

        section("Prometheus 指标端点已启动: http://localhost:9090/metrics (Ctrl+C 退出)")
        start_http_server(9090)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.get_event_loop().run_forever()

    print()
    print(BANNER)
    print("  演示结束")
    print(BANNER)


if __name__ == "__main__":
    main()

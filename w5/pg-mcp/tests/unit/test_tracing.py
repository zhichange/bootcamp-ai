"""Unit tests for request tracing and context propagation."""

import logging

import pytest

from pg_mcp.observability.tracing import (
    TracingLogger,
    clear_request_id,
    generate_request_id,
    get_request_id,
    get_tracing_logger,
    request_context,
    set_request_id,
    trace_async,
    trace_sync,
)


class TestRequestIdContext:
    """Tests for request ID context management."""

    def test_generate_request_id_returns_uuid(self) -> None:
        """Test generated request IDs are unique non-empty strings."""
        id1 = generate_request_id()
        id2 = generate_request_id()
        assert id1 != id2
        assert len(id1) == 36

    def test_set_and_get_request_id(self) -> None:
        """Test setting and reading the request ID."""
        set_request_id("req-1")
        assert get_request_id() == "req-1"
        clear_request_id()
        assert get_request_id() is None

    @pytest.mark.asyncio
    async def test_request_context_generates_id(self) -> None:
        """Test request_context generates an ID and resets it afterwards."""
        async with request_context() as request_id:
            assert request_id
            assert get_request_id() == request_id
        assert get_request_id() is None

    @pytest.mark.asyncio
    async def test_request_context_with_provided_id(self) -> None:
        """Test request_context honors a provided request ID."""
        async with request_context("fixed-id") as request_id:
            assert request_id == "fixed-id"
            assert get_request_id() == "fixed-id"

    @pytest.mark.asyncio
    async def test_request_context_propagates_through_await(self) -> None:
        """Test the request ID is visible inside awaited coroutines."""

        async def inner() -> str | None:
            return get_request_id()

        async with request_context("outer-id"):
            assert await inner() == "outer-id"


class _CaptureHandler(logging.Handler):
    """Handler that captures log records into a list."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class TestTraceDecorators:
    """Tests for the trace_async and trace_sync decorators."""

    @pytest.mark.asyncio
    async def test_trace_async_injects_request_id(self) -> None:
        """Test trace_async adds request_id to log records."""

        @trace_async(operation="op-test")
        async def work(logger: logging.Logger) -> None:
            logger.info("inside work")

        handler = _CaptureHandler()
        test_logger = logging.getLogger("trace-async-test")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)

        async with request_context("trace-123"):
            await work(test_logger)

        assert len(handler.records) == 1
        assert handler.records[0].request_id == "trace-123"
        assert handler.records[0].operation == "op-test"

    @pytest.mark.asyncio
    async def test_trace_async_without_context(self) -> None:
        """Test trace_async works without an active request context."""

        @trace_async()
        async def work() -> str:
            return "ok"

        assert await work() == "ok"

    def test_trace_sync_injects_request_id(self) -> None:
        """Test trace_sync adds request_id to log records."""

        @trace_sync(operation="sync-op")
        def work(logger: logging.Logger) -> None:
            logger.info("inside sync work")

        handler = _CaptureHandler()
        test_logger = logging.getLogger("trace-sync-test")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)

        set_request_id("sync-456")
        try:
            work(test_logger)
        finally:
            clear_request_id()

        assert len(handler.records) == 1
        assert handler.records[0].request_id == "sync-456"


class TestTracingLogger:
    """Tests for the TracingLogger wrapper."""

    def test_adds_request_id_to_extra(self) -> None:
        """Test TracingLogger injects the current request_id into extra."""
        handler = _CaptureHandler()
        test_logger = logging.getLogger("tracing-logger-test")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)

        tracing_logger = get_tracing_logger("tracing-logger-test")
        set_request_id("ctx-789")
        try:
            tracing_logger.info("hello", extra={"database": "db1"})
        finally:
            clear_request_id()

        assert len(handler.records) == 1
        record = handler.records[0]
        assert record.request_id == "ctx-789"
        assert record.database == "db1"

    def test_does_not_overwrite_explicit_request_id(self) -> None:
        """Test an explicitly provided request_id wins over the context."""
        handler = _CaptureHandler()
        test_logger = logging.getLogger("tracing-logger-explicit")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)

        tracing_logger = TracingLogger("tracing-logger-explicit")
        set_request_id("ctx-auto")
        try:
            tracing_logger.warning("hello", extra={"request_id": "ctx-manual"})
        finally:
            clear_request_id()

        assert handler.records[0].request_id == "ctx-manual"

    def test_exception_logging(self) -> None:
        """Test exception() records exc_info."""
        handler = _CaptureHandler()
        test_logger = logging.getLogger("tracing-logger-exc")
        test_logger.addHandler(handler)
        test_logger.setLevel(logging.INFO)

        tracing_logger = get_tracing_logger("tracing-logger-exc")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            tracing_logger.exception("failed")

        assert handler.records[0].exc_info is not None
        assert handler.records[0].levelno == logging.ERROR

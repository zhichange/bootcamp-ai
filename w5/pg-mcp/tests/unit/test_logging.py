"""Unit tests for structured logging and sensitive data filtering."""

import json
import logging

import pytest

from pg_mcp.observability.logging import (
    JSONFormatter,
    SensitiveDataFilter,
    TextFormatter,
    configure_logging,
    get_logger,
)


class TestSensitiveDataFilter:
    """Tests for sensitive data sanitization."""

    @pytest.fixture
    def log_filter(self) -> SensitiveDataFilter:
        """Create the filter under test."""
        return SensitiveDataFilter()

    def _make_record(self, msg: str, **extra: object) -> logging.LogRecord:
        """Create a log record with extra attributes."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_filters_extra_sensitive_keys(self, log_filter: SensitiveDataFilter) -> None:
        """Test sensitive extra fields are redacted."""
        record = self._make_record("msg", password="hunter2", api_key="sk-123")
        assert log_filter.filter(record) is True
        assert record.password == "***REDACTED***"
        assert record.api_key == "***REDACTED***"

    def test_sanitizes_nested_dicts(self, log_filter: SensitiveDataFilter) -> None:
        """Test nested dictionaries are sanitized recursively."""
        record = self._make_record("msg", details={"user": "bob", "credentials": {"token": "abc"}})
        log_filter.filter(record)
        assert record.details == {"user": "bob", "credentials": {"token": "***REDACTED***"}}

    def test_sanitizes_args(self, log_filter: SensitiveDataFilter) -> None:
        """Test message args are sanitized."""
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="connect %s",
            args=({"password": "secret"},),
            exc_info=None,
        )
        log_filter.filter(record)
        args_str = str(record.args)
        assert "secret" not in args_str
        assert "***REDACTED***" in args_str

    def test_non_sensitive_keys_untouched(self, log_filter: SensitiveDataFilter) -> None:
        """Test non-sensitive fields pass through unchanged."""
        record = self._make_record("msg", database="db1", row_count=5)
        log_filter.filter(record)
        assert record.database == "db1"
        assert record.row_count == 5


class TestFormatters:
    """Tests for JSON and text log formatters."""

    def _make_record(self, msg: str, **extra: object) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test.logger",
            level=logging.WARNING,
            pathname="test.py",
            lineno=42,
            msg=msg,
            args=(),
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_json_formatter_output(self) -> None:
        """Test JSON formatter produces parseable JSON with core fields."""
        record = self._make_record("hello world", request_id="req-1")
        output = JSONFormatter().format(record)
        data = json.loads(output)
        assert data["message"] == "hello world"
        assert data["level"] == "WARNING"
        assert data["logger"] == "test.logger"
        assert data["request_id"] == "req-1"

    def test_json_formatter_includes_extra(self) -> None:
        """Test JSON formatter includes custom extra fields."""
        record = self._make_record("msg", database="db1")
        data = json.loads(JSONFormatter().format(record))
        assert data["extra"]["database"] == "db1"

    def test_text_formatter_output(self) -> None:
        """Test text formatter includes level, logger and message."""
        record = self._make_record("simple message", request_id="req-2")
        output = TextFormatter().format(record)
        assert "WARNING" in output
        assert "test.logger" in output
        assert "simple message" in output
        assert "request_id=req-2" in output


class TestConfigureLogging:
    """Tests for logging configuration."""

    @pytest.fixture(autouse=True)
    def restore_logging(self) -> None:
        """Restore root logger handlers after each test."""
        root = logging.getLogger()
        handlers_before = root.handlers[:]
        level_before = root.level
        yield  # type: ignore[misc]
        root.handlers = handlers_before
        root.setLevel(level_before)

    def test_configure_json_with_filter(self) -> None:
        """Test JSON configuration attaches a JSON formatter and filter."""
        configure_logging(level="DEBUG", log_format="json", enable_sensitive_filter=True)
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JSONFormatter)
        assert any(isinstance(f, SensitiveDataFilter) for f in root.handlers[0].filters)

    def test_configure_text_without_filter(self) -> None:
        """Test text configuration without sensitive filter."""
        configure_logging(level="INFO", log_format="text", enable_sensitive_filter=False)
        root = logging.getLogger()
        assert isinstance(root.handlers[0].formatter, TextFormatter)
        assert not any(isinstance(f, SensitiveDataFilter) for f in root.handlers[0].filters)

    def test_configure_replaces_existing_handlers(self) -> None:
        """Test reconfiguration removes old handlers."""
        root = logging.getLogger()
        root.addHandler(logging.NullHandler())
        configure_logging(level="INFO", log_format="json", enable_sensitive_filter=False)
        assert len(root.handlers) == 1

    def test_get_logger(self) -> None:
        """Test get_logger returns a named logger."""
        assert get_logger("some.module").name == "some.module"

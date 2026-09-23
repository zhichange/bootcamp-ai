"""Unit tests for the LLM-based result validator (mocked OpenAI client)."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from pg_mcp.config.settings import OpenAIConfig, ValidationConfig
from pg_mcp.models.errors import LLMError
from pg_mcp.services.result_validator import ResultValidator


def _make_validator(enabled: bool = True) -> ResultValidator:
    """Create a validator with a mocked OpenAI client."""
    openai_config = OpenAIConfig(api_key="sk-test", model="gpt-4o-mini")
    validation_config = ValidationConfig(enabled=enabled)
    return ResultValidator(openai_config=openai_config, validation_config=validation_config)


def _mock_response(content: str | None, choices: int = 1) -> SimpleNamespace:
    """Build a fake ChatCompletion response."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content)) for _ in range(choices)],
        model_dump=lambda: {"mock": True},
    )


class TestResultValidator:
    """Tests for ResultValidator."""

    @pytest.mark.asyncio
    async def test_disabled_returns_full_confidence(self) -> None:
        """Test disabled validation short-circuits with confidence 100."""
        validator = _make_validator(enabled=False)
        result = await validator.validate(
            question="q", sql="SELECT 1", results=[{"a": 1}], row_count=1
        )
        assert result.confidence == 100
        assert result.is_acceptable is True

    @pytest.mark.asyncio
    async def test_successful_validation(self) -> None:
        """Test parsing a valid LLM JSON response."""
        validator = _make_validator()
        validator.client = AsyncMock()
        validator.client.chat.completions.create = AsyncMock(
            return_value=_mock_response(
                '{"confidence": 85, "explanation": "matches", "suggestion": null}'
            )
        )

        result = await validator.validate(
            question="q", sql="SELECT 1", results=[{"a": 1}], row_count=1
        )

        assert result.confidence == 85
        assert result.explanation == "matches"
        assert result.is_acceptable is True

    @pytest.mark.asyncio
    async def test_low_confidence_not_acceptable(self) -> None:
        """Test results below the confidence threshold are not acceptable."""
        validator = _make_validator()
        validator.client = AsyncMock()
        validator.client.chat.completions.create = AsyncMock(
            return_value=_mock_response('{"confidence": 20, "explanation": "wrong"}')
        )

        result = await validator.validate(question="q", sql="SELECT 1", results=[], row_count=0)
        assert result.confidence == 20
        assert result.is_acceptable is False

    @pytest.mark.asyncio
    async def test_invalid_json_returns_moderate_confidence(self) -> None:
        """Test unparseable LLM responses yield confidence 60 and a warning."""
        validator = _make_validator()
        validator.client = AsyncMock()
        validator.client.chat.completions.create = AsyncMock(
            return_value=_mock_response("not json at all")
        )

        result = await validator.validate(question="q", sql="SELECT 1", results=[], row_count=0)
        assert result.confidence == 60
        assert result.is_acceptable is False
        assert "parsing failed" in result.explanation.lower()

    @pytest.mark.asyncio
    async def test_out_of_range_confidence_is_clamped(self) -> None:
        """Test numeric confidence outside 0-100 is clamped."""
        validator = _make_validator()
        validator.client = AsyncMock()
        validator.client.chat.completions.create = AsyncMock(
            return_value=_mock_response('{"confidence": 250, "explanation": "x"}')
        )

        result = await validator.validate(question="q", sql="SELECT 1", results=[], row_count=0)
        assert result.confidence == 100

    @pytest.mark.asyncio
    async def test_empty_choices_raises(self) -> None:
        """Test an empty LLM response raises LLMError."""
        validator = _make_validator()
        validator.client = AsyncMock()
        validator.client.chat.completions.create = AsyncMock(
            return_value=_mock_response(None, choices=0)
        )

        with pytest.raises(LLMError):
            await validator.validate(question="q", sql="SELECT 1", results=[], row_count=0)

    @pytest.mark.asyncio
    async def test_samples_results_before_calling_llm(self) -> None:
        """Test only sample_rows rows are sent to the LLM."""
        validator = _make_validator()
        validator.validation_config.sample_rows = 2
        validator.client = AsyncMock()
        validator.client.chat.completions.create = AsyncMock(
            return_value=_mock_response('{"confidence": 90, "explanation": "ok"}')
        )

        rows: list[dict[str, Any]] = [{"id": i} for i in range(100)]
        await validator.validate(question="q", sql="SELECT 1", results=rows, row_count=100)

        prompt = validator.client.chat.completions.create.call_args.kwargs["messages"][1]["content"]
        assert '"id": 99' not in prompt

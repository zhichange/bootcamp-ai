"""Unit tests for query result export (service and API endpoint)."""

import json
import re
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.main import app
from app.database import get_session
from app.models.database import DatabaseConnection, ConnectionStatus
from app.models.query import QuerySource
from app.models.schemas import QueryResult, QueryColumn
from app.services.export_service import (
    ExportFormat,
    format_result,
    build_filename,
)
from app.services.sql_validator import SqlValidationError


@pytest.fixture
def test_session():
    """Create an in-memory SQLite session for testing."""
    engine = create_engine(
        "sqlite:///file:test_export_db?mode=memory&cache=shared&uri=true",
        connect_args={"check_same_thread": False, "uri": True},
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def client(test_session):
    """Create TestClient with test database session."""

    def get_test_session():
        return test_session

    app.dependency_overrides[get_session] = get_test_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def sample_connection(test_session):
    """Create a sample database connection."""
    conn = DatabaseConnection(
        name="test_db",
        url="postgresql://user:pass@localhost/testdb",
        description="Test database",
        status=ConnectionStatus.ACTIVE,
    )
    test_session.add(conn)
    test_session.commit()
    test_session.refresh(conn)
    return conn


def make_result() -> QueryResult:
    """Build a sample QueryResult with edge-case values."""
    return QueryResult(
        columns=[
            QueryColumn(name="id", dataType="integer"),
            QueryColumn(name="name", dataType="character varying"),
            QueryColumn(name="note", dataType="text"),
        ],
        rows=[
            {"id": 1, "name": "Alice", "note": 'has "quotes", commas\nand newlines'},
            {"id": 2, "name": "张三", "note": None},
        ],
        rowCount=2,
        executionTimeMs=10,
        sql="SELECT * FROM users",
    )


class TestExportFormat:
    """Test ExportFormat enum properties."""

    def test_content_types(self):
        assert ExportFormat.CSV.content_type == "text/csv; charset=utf-8"
        assert ExportFormat.JSON.content_type == "application/json; charset=utf-8"
        assert ExportFormat.TSV.content_type == "text/tab-separated-values; charset=utf-8"

    def test_extensions(self):
        assert ExportFormat.CSV.extension == "csv"
        assert ExportFormat.JSON.extension == "json"
        assert ExportFormat.TSV.extension == "tsv"


class TestFormatResult:
    """Test result serialization for each format."""

    def test_csv_basic(self):
        content = format_result(make_result(), ExportFormat.CSV)
        assert content.split("\n")[0] == "id,name,note"
        # Special characters must be quoted and quotes doubled (RFC 4180)
        assert 'has ""quotes"", commas' in content
        # NULL becomes empty
        assert "张三," in content

    def test_csv_escapes_correctly(self):
        content = format_result(make_result(), ExportFormat.CSV)
        # The quoted field spans two lines
        assert 'has ""quotes"", commas' in content

    def test_json_is_valid_array(self):
        content = format_result(make_result(), ExportFormat.JSON)
        data = json.loads(content)
        assert len(data) == 2
        assert data[0]["name"] == "Alice"
        assert data[1]["note"] is None

    def test_json_serializes_datetime(self):
        result = QueryResult(
            columns=[QueryColumn(name="ts", dataType="timestamp")],
            rows=[{"ts": datetime(2026, 8, 19, 10, 15, 0)}],
            rowCount=1,
            executionTimeMs=1,
            sql="SELECT now()",
        )
        data = json.loads(format_result(result, ExportFormat.JSON))
        assert data[0]["ts"] == "2026-08-19T10:15:00"

    def test_tsv_no_embedded_tabs_or_newlines(self):
        content = format_result(make_result(), ExportFormat.TSV)
        lines = content.strip().split("\n")
        assert lines[0] == "id\tname\tnote"
        for line in lines:
            assert line.count("\t") == 2
        assert "张三" in content

    def test_empty_result(self):
        result = QueryResult(
            columns=[QueryColumn(name="id", dataType="integer")],
            rows=[],
            rowCount=0,
            executionTimeMs=1,
            sql="SELECT 1 WHERE false",
        )
        assert format_result(result, ExportFormat.CSV).strip() == "id"
        assert json.loads(format_result(result, ExportFormat.JSON)) == []
        assert format_result(result, ExportFormat.TSV).strip() == "id"


class TestBuildFilename:
    """Test download filename generation."""

    def test_filename_format(self):
        filename = build_filename("todo", ExportFormat.CSV)
        assert re.fullmatch(r"todo_\d{8}_\d{6}\.csv", filename)


class TestExportEndpoint:
    """Test the export API endpoint."""

    def _mock_result(self):
        return QueryResult(
            columns=[QueryColumn(name="id", dataType="integer")],
            rows=[{"id": 1}, {"id": 2}],
            rowCount=2,
            executionTimeMs=5,
            sql="SELECT id FROM users",
        )

    @patch("app.api.v1.queries.execute_query_with_service")
    def test_export_csv(self, mock_execute, client, sample_connection):
        mock_execute.return_value = self._mock_result()
        response = client.post(
            "/api/v1/dbs/test_db/query/export",
            json={"sql": "SELECT id FROM users", "format": "csv"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        assert ".csv" in response.headers["content-disposition"]
        assert response.text.strip().split("\n")[0] == "id"

    @patch("app.api.v1.queries.execute_query_with_service")
    def test_export_json(self, mock_execute, client, sample_connection):
        mock_execute.return_value = self._mock_result()
        response = client.post(
            "/api/v1/dbs/test_db/query/export",
            json={"sql": "SELECT id FROM users", "format": "json"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert ".json" in response.headers["content-disposition"]
        assert response.json() == [{"id": 1}, {"id": 2}]

    @patch("app.api.v1.queries.execute_query_with_service")
    def test_export_tsv(self, mock_execute, client, sample_connection):
        mock_execute.return_value = self._mock_result()
        response = client.post(
            "/api/v1/dbs/test_db/query/export",
            json={"sql": "SELECT id FROM users", "format": "tsv"},
        )
        assert response.status_code == 200
        assert "tab-separated-values" in response.headers["content-type"]
        assert response.text.startswith("id\n")

    @patch("app.api.v1.queries.execute_query_with_service")
    def test_export_default_format_is_csv(self, mock_execute, client, sample_connection):
        mock_execute.return_value = self._mock_result()
        response = client.post(
            "/api/v1/dbs/test_db/query/export",
            json={"sql": "SELECT id FROM users"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")

    def test_export_unsupported_format(self, client, sample_connection):
        response = client.post(
            "/api/v1/dbs/test_db/query/export",
            json={"sql": "SELECT id FROM users", "format": "pdf"},
        )
        assert response.status_code == 422
        assert "Unsupported export format" in response.json()["detail"]

    def test_export_database_not_found(self, client):
        response = client.post(
            "/api/v1/dbs/nonexistent/query/export",
            json={"sql": "SELECT 1", "format": "csv"},
        )
        assert response.status_code == 404

    @patch("app.api.v1.queries.execute_query_with_service")
    def test_export_validation_error(self, mock_execute, client, sample_connection):
        mock_execute.side_effect = SqlValidationError("Only SELECT queries are allowed")
        response = client.post(
            "/api/v1/dbs/test_db/query/export",
            json={"sql": "DELETE FROM users", "format": "csv"},
        )
        assert response.status_code == 400

    @patch("app.api.v1.queries.execute_query_with_service")
    def test_export_execution_error(self, mock_execute, client, sample_connection):
        mock_execute.side_effect = Exception("Table does not exist")
        response = client.post(
            "/api/v1/dbs/test_db/query/export",
            json={"sql": "SELECT * FROM invalid_table", "format": "csv"},
        )
        assert response.status_code == 500

    @patch("app.api.v1.queries.execute_query_with_service")
    def test_export_uses_manual_source(self, mock_execute, client, sample_connection):
        mock_execute.return_value = self._mock_result()
        client.post(
            "/api/v1/dbs/test_db/query/export",
            json={"sql": "SELECT id FROM users", "format": "csv"},
        )
        mock_execute.assert_called_once()
        assert mock_execute.call_args[0][4] == QuerySource.MANUAL

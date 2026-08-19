"""Query result export service (Strategy pattern).

Serializes a QueryResult into a downloadable file format (CSV, JSON, TSV).
"""

import csv
import io
import json
from datetime import datetime
from enum import Enum
from typing import List

from app.models.schemas import QueryResult


class ExportFormat(str, Enum):
    """Supported export formats with their media types."""

    CSV = "csv"
    JSON = "json"
    TSV = "tsv"

    @property
    def content_type(self) -> str:
        """Full Content-Type header value (with charset)."""
        media_types = {
            ExportFormat.CSV: "text/csv",
            ExportFormat.JSON: "application/json",
            ExportFormat.TSV: "text/tab-separated-values",
        }
        return f"{media_types[self]}; charset=utf-8"

    @property
    def extension(self) -> str:
        """File extension for this format."""
        return self.value


def _column_names(result: QueryResult) -> List[str]:
    """Get ordered column names from a query result."""
    return [col.name for col in result.columns]


def _to_csv(result: QueryResult) -> str:
    """Serialize result as RFC 4180 compliant CSV."""
    headers = _column_names(result)
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    for row in result.rows:
        writer.writerow(
            ["" if row.get(h) is None else row.get(h) for h in headers]
        )
    return output.getvalue()


def _to_tsv(result: QueryResult) -> str:
    """Serialize result as tab-separated values.

    Tabs/newlines inside values are replaced with spaces to keep one row per line.
    """
    headers = _column_names(result)
    lines = ["\t".join(headers)]
    for row in result.rows:
        values = []
        for h in headers:
            value = row.get(h)
            if value is None:
                values.append("")
            else:
                values.append(str(value).replace("\t", " ").replace("\r", " ").replace("\n", " "))
        lines.append("\t".join(values))
    return "\n".join(lines) + "\n"


def _to_json(result: QueryResult) -> str:
    """Serialize result as a pretty-printed JSON array of row objects."""
    return json.dumps(result.rows, indent=2, ensure_ascii=False, default=str)


def format_result(result: QueryResult, fmt: ExportFormat) -> str:
    """Format a QueryResult into the target export format.

    Args:
        result: Query result with columns and rows
        fmt: Target export format

    Returns:
        Serialized file content as a string
    """
    formatters = {
        ExportFormat.CSV: _to_csv,
        ExportFormat.JSON: _to_json,
        ExportFormat.TSV: _to_tsv,
    }
    return formatters[fmt](result)


def build_filename(database_name: str, fmt: ExportFormat) -> str:
    """Build a download filename like 'todo_20260819_101500.csv'."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{database_name}_{timestamp}.{fmt.extension}"

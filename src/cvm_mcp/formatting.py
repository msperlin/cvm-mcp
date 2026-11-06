"""Token-efficient rendering for tool output.

Tool results land verbatim in a language model's context, so tables are
rendered as compact CSV with short headers instead of repetitive JSON records.
"""

from __future__ import annotations

import io
import math
from collections.abc import Iterable, Sequence

import pandas as pd


def format_value(value: object) -> str:
    """Render a single cell: blank for null, integers bare, floats with 2 dp."""
    if value is None or value is pd.NA:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        return f"{value:.2f}"
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    return str(value)


def to_csv(
    frame: pd.DataFrame,
    *,
    columns: Sequence[str] | None = None,
    rename: dict[str, str] | None = None,
) -> str:
    """Render a DataFrame as compact CSV without a trailing newline."""
    if frame.empty:
        return ""

    selected = frame
    if columns is not None:
        available = [column for column in columns if column in frame.columns]
        if available:
            selected = frame.loc[:, available]

    output = io.StringIO()
    header = [str(column) for column in selected.columns]
    output.write(",".join(_escape(name) for name in header))
    output.write("\n")

    for row in selected.itertuples(index=False, name=None):
        output.write(",".join(_escape(format_value(value)) for value in row))
        output.write("\n")

    text = output.getvalue().rstrip("\n")
    if rename:
        first, _, rest = text.partition("\n")
        names = [rename.get(name, name) for name in first.split(",")]
        text = "\n".join([",".join(names), rest]) if rest else ",".join(names)
    return text


def _escape(text: str) -> str:
    if any(character in text for character in (",", '"', "\n", "\r")):
        return '"' + text.replace('"', '""') + '"'
    return text


def with_warnings(body: str, warnings: Iterable[str] | None) -> str:
    """Prefix CSV with ``#`` comment lines, which keep the body valid CSV."""
    messages = [message for message in (warnings or []) if message]
    if not messages:
        return body
    prefix = "\n".join(f"# WARNING: {message}" for message in messages)
    return f"{prefix}\n{body}" if body else prefix


def human_bytes(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"

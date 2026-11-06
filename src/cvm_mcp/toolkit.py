"""Shared parsing and rendering helpers for the tool modules."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

import pandas as pd
from mcp.types import ToolAnnotations
from pydantic import Field

from . import schema_dfp
from .catalog_dfp import fold
from .formatting import to_csv, with_warnings
from .normalize import clean_names

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=True)

MIN_YEAR = 2010
MAX_TABLE_ROWS = 50_000
DEFAULT_TABLE_ROWS = 200

VERSIONS = ("first", "last", "all")


def json_out(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def error(message: str, *, hint: str | None = None, **extra: Any) -> str:
    payload: dict[str, Any] = {"error": message}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return json_out(payload)


def current_year() -> int:
    return datetime.now().year


def parse_years(first_year: int | None, last_year: int | None, *, span: int = 3) -> tuple[int, int]:
    current = current_year()
    last = current if last_year is None else int(last_year)
    first = (last - span + 1) if first_year is None else int(first_year)
    if first < MIN_YEAR:
        raise ValueError(f"first_year must be >= {MIN_YEAR} (CVM data starts there)")
    if last > current + 1:
        raise ValueError(f"last_year must be <= {current + 1}")
    if first > last:
        raise ValueError("first_year must be on or before last_year")
    return first, last


def parse_codes(codes: list[int] | int | None) -> list[int] | None:
    if codes is None:
        return None
    if isinstance(codes, (int, str)) and not isinstance(codes, bool):
        codes = [codes]
    cleaned: list[int] = []
    for code in codes:
        try:
            value = int(code)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        raise ValueError("cd_cvm must contain positive integers")
    return cleaned


def parse_docs(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        tokens = [token for token in re.split(r"[,;|+\s]+", value) if token]
    else:
        tokens = [str(token) for token in value]
    upper = [token.strip().upper() for token in tokens if token.strip()]
    if not upper:
        raise ValueError("doc_type is empty")
    if any(token in {"*", "ALL"} for token in upper):
        return list(schema_dfp.DOC_TYPES)
    unknown = [token for token in upper if token not in schema_dfp.DOC_TYPES]
    if unknown:
        raise ValueError(
            f"unknown doc_type {', '.join(unknown)}. "
            f"Valid: {', '.join(schema_dfp.DOC_TYPES)} or '*'"
        )
    return list(dict.fromkeys(upper))


def parse_formats(value: str | list[str]) -> list[str]:
    if isinstance(value, str):
        tokens = [token for token in re.split(r"[,;|+\s]+", value) if token]
    else:
        tokens = [str(token) for token in value]
    lower = [token.strip().lower() for token in tokens if token.strip()]
    if not lower:
        raise ValueError("format is empty")
    if any(token in {"*", "all"} for token in lower):
        return list(schema_dfp.DOC_FORMATS)
    unknown = [token for token in lower if token not in schema_dfp.DOC_FORMATS]
    if unknown:
        raise ValueError(
            f"unknown format {', '.join(unknown)}. Valid: con (consolidated), ind (individual)"
        )
    return list(dict.fromkeys(lower))


def parse_data_type(value: str) -> str:
    kind = str(value).strip().lower()
    if kind not in ("dfp", "itr"):
        raise ValueError("data_type must be 'dfp' (annual) or 'itr' (quarterly)")
    return kind


def parse_version(value: str) -> str:
    version = str(value).strip().lower()
    aliases = {"latest": "last", "newest": "last", "earliest": "first"}
    version = aliases.get(version, version)
    if version not in VERSIONS:
        raise ValueError(f"version must be one of {', '.join(VERSIONS)}")
    return version


def apply_version(frame: pd.DataFrame, version: str) -> pd.DataFrame:
    """Keep the first/last FRE version per company and reference date."""
    if version == "all" or frame.empty or "versao" not in frame.columns:
        return frame
    group = [column for column in ("cnpj", "data_referencia") if column in frame.columns]
    if not group:
        return frame

    valid = frame["versao"].notna()
    work = frame.loc[valid].copy()
    if work.empty:
        return frame.reset_index(drop=True)

    chooser = "max" if version == "last" else "min"
    chosen = work.groupby(group, dropna=False)["versao"].transform(chooser)
    out = work.loc[work["versao"].eq(chosen)]
    rest = frame.loc[~valid]
    if not rest.empty:
        out = pd.concat([out, rest], ignore_index=True)
    return out.reset_index(drop=True)


def render(
    frame: pd.DataFrame,
    *,
    columns: list[str] | None = None,
    max_rows: int,
    warnings: list[str] | None = None,
) -> str:
    messages = [message for message in (warnings or []) if message]
    if frame.empty:
        prefix = "\n".join(f"# NOTE: {message}" for message in messages)
        body = "(no rows match the requested filters)"
        return f"{prefix}\n{body}" if prefix else body

    selected = frame
    if columns:
        wanted = clean_names(columns)
        available = set(frame.columns)
        picked = [name for name in wanted if name in available]
        if picked:
            selected = frame.loc[:, picked]

    if len(selected) > max_rows:
        messages.append(
            f"output truncated from {len(selected)} to {max_rows} rows; "
            "narrow the years/companies, select columns, or raise max_rows"
        )
        selected = selected.iloc[:max_rows]

    return with_warnings(to_csv(selected), messages)


def int_field(description: str, **kwargs: Any):
    return Annotated[int, Field(description=description, **kwargs)]


__all__ = [
    "DEFAULT_TABLE_ROWS",
    "MAX_TABLE_ROWS",
    "MIN_YEAR",
    "READ_ONLY",
    "VERSIONS",
    "Literal",
    "apply_version",
    "current_year",
    "error",
    "fold",
    "json_out",
    "parse_codes",
    "parse_data_type",
    "parse_docs",
    "parse_formats",
    "parse_version",
    "parse_years",
    "render",
]

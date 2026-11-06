"""Generic normalization for the CVM open-data files.

Every dataset on ``dados.cvm.gov.br`` is a semicolon-separated Latin-1 CSV, but
three details bite in practice and are handled here once:

1. **Encoding is Latin-1, not UTF-8.** Reading it as UTF-8 mangles every accent
   in company names.
2. **Quoting cannot be trusted.** Some free-text fields carry unbalanced double
   quotes, so a quoting-aware parser silently shifts columns. We try minimal
   quoting first and fall back to ``QUOTE_NONE``.
3. **CNPJ is formatted two ways.** Company files use ``00.000.000/0001-91``;
   fund files use ``00000000000191``. Everything is normalized to 14 digits so
   joins behave.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterable

import pandas as pd


def fold(text: object) -> str:
    """Lowercase, accent-free form used for matching."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def clean_names(columns: Iterable[object]) -> list[str]:
    """``Camel_Case`` headers -> ``snake_case``."""
    cleaned: list[str] = []
    for column in columns:
        text = re.sub(r"[^0-9A-Za-z]+", "_", str(column)).strip("_").lower()
        cleaned.append(re.sub(r"_+", "_", text))
    return cleaned


def normalize_cnpj(value: object) -> str | None:
    """Return the 14 CNPJ digits, or ``None`` when the value is not a CNPJ."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    return digits if len(digits) == 14 else None


def cnpj_series(values: pd.Series) -> pd.Series:
    """Vectorized :func:`normalize_cnpj` returning a string series."""
    return (
        values.astype("string")
        .str.replace(r"\D", "", regex=True)
        .where(lambda series: series.str.len() == 14)
    )


def read_csv_bytes(
    payload: bytes,
    *,
    sep: str = ";",
    encoding: str = "latin1",
) -> pd.DataFrame:
    """Read one CVM CSV, tolerating unbalanced quotes and bad lines."""
    common: dict[str, object] = {
        "sep": sep,
        "encoding": encoding,
        "dtype": "string",
        "keep_default_na": False,
        "na_values": [],
        "on_bad_lines": "warn",
    }
    try:
        return pd.read_csv(io.BytesIO(payload), quoting=csv.QUOTE_MINIMAL, engine="c", **common)
    except (pd.errors.ParserError, UnicodeDecodeError, csv.Error):
        return pd.read_csv(io.BytesIO(payload), quoting=csv.QUOTE_NONE, engine="python", **common)


def read_csv_file(path, *, sep: str = ";", encoding: str = "latin1") -> pd.DataFrame:
    return read_csv_bytes(path.read_bytes(), sep=sep, encoding=encoding)


#: A date as CVM writes it (ISO or dd/mm/yyyy).
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})([ T]\d{2}:\d{2}(:\d{2})?)?$")
#: A number as CVM writes it: optional sign, dot decimals, optional comma.
_NUMBER_RE = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$")
_NUMBER_COMMA_RE = re.compile(r"^[+-]?\d+,\d+$")


def _looks_empty(text: str) -> bool:
    return text.strip().lower() in {"", "nan", "none", "null", "na", "n/a", "-"}


def _sample(values: pd.Series, size: int = 2000) -> list[str]:
    non_null = values.dropna()
    if non_null.empty:
        return []
    sample = non_null.head(size)
    return [str(value).strip() for value in sample]


#: Columns whose values are identifiers, never numbers, whatever they look like.
_IDENTIFIER_RE = re.compile(r"(^|_)(cnpj|cpf|cep|nis|id|codigo|cod)(_|$)")
#: A run of digits this long is an identifier (CNPJ, process number), not a quantity.
_LONG_DIGITS_RE = re.compile(r"^\d{12,}$")


def _coerce_column(values: pd.Series) -> pd.Series:
    """Best-effort typing: ISO dates -> string dates, numbers -> float64."""
    sample = _sample(values)
    if not sample:
        return values.astype("string")

    meaningful = [text for text in sample if not _looks_empty(text)]
    if not meaningful:
        return values.astype("string")

    if all(_DATE_RE.match(text) for text in meaningful):
        parsed = pd.to_datetime(values, errors="coerce", format="mixed", dayfirst=False)
        return parsed.dt.strftime("%Y-%m-%d").astype("string")

    if all(_LONG_DIGITS_RE.match(text) for text in meaningful):
        return values.astype("string").str.strip().replace("", pd.NA)

    if all(_NUMBER_RE.match(text) for text in meaningful):
        return pd.to_numeric(values, errors="coerce").astype("float64")

    if all(_NUMBER_COMMA_RE.match(text) for text in meaningful):
        return pd.to_numeric(values.str.replace(",", ".", regex=False), errors="coerce").astype(
            "float64"
        )

    cleaned = values.astype("string").str.strip().replace("", pd.NA)
    return cleaned


def canonicalize(
    frame: pd.DataFrame,
    *,
    key_columns: Iterable[str] = (),
) -> pd.DataFrame:
    """Clean headers, coerce types and put the key columns first."""
    if frame.empty:
        return frame

    frame = frame.copy()
    frame.columns = clean_names(frame.columns)
    frame = frame.loc[:, ~frame.columns.duplicated()]

    converted: dict[str, pd.Series] = {}
    for column in frame.columns:
        values = frame[column]
        if _IDENTIFIER_RE.search(column):
            converted[column] = values.astype("string").str.strip().replace("", pd.NA)
        else:
            converted[column] = _coerce_column(values)
    # Build once instead of assigning column by column: the wide FIDC tables
    # (~100 columns) fragment badly and pandas emits a PerformanceWarning.
    frame = pd.DataFrame(converted, index=frame.index)

    ordered = [column for column in key_columns if column in frame.columns]
    ordered += [column for column in frame.columns if column not in ordered]
    return frame.loc[:, ordered].reset_index(drop=True)


def attach_cd_cvm(
    detail: pd.DataFrame,
    registry: pd.DataFrame,
    *,
    cnpj_column: str = "cnpj",
) -> pd.DataFrame:
    """Add ``cd_cvm`` to a detail table using the company registry."""
    if "cd_cvm" in detail.columns:
        detail = detail.drop(columns=["cd_cvm"])
    if registry.empty or cnpj_column not in detail.columns:
        detail["cd_cvm"] = pd.Series(pd.NA, index=detail.index, dtype="Int64")
        return detail

    mapping = (
        registry.assign(cnpj=registry["CNPJ_CIA"].map(normalize_cnpj))
        .loc[:, ["cnpj", "CD_CVM"]]
        .dropna(subset=["cnpj"])
        .drop_duplicates("cnpj")
        .rename(columns={"CD_CVM": "cd_cvm"})
    )
    detail = detail.assign(cnpj=detail[cnpj_column].map(normalize_cnpj))
    merged = detail.merge(mapping, on="cnpj", how="left")
    merged["cd_cvm"] = pd.to_numeric(merged["cd_cvm"], errors="coerce").astype("Int64")
    return merged

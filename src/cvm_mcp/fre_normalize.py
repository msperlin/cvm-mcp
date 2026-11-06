"""FRE normalization, kept byte-for-byte compatible with ``cvm-fre-mcp``.

The FRE archives are re-read with the exact rules of the standalone server so
the unified MCP returns the same frames: same header cleaning, same shared key
block, same ``cd_cvm`` attachment (by document id, falling back to CNPJ) and the
same type coercion.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable

import pandas as pd

KEY_COLUMNS = (
    "cd_cvm",
    "cnpj",
    "nome_companhia",
    "data_referencia",
    "versao",
    "id_documento",
)

RAW_RENAMES = {
    "cnpj_cia": "cnpj",
    "cnpj_companhia": "cnpj",
    "denom_cia": "nome_companhia",
    "dt_refer": "data_referencia",
    "id_doc": "id_documento",
    "dt_receb": "data_recebimento",
    "link_doc": "link_documento",
}

NUMERIC_PREFIXES = (
    "valor_",
    "quantidade_",
    "percentual_",
    "preco_",
    "numero_",
    "total_",
    "salario",
    "bonus",
    "participacao_",
    "outros_valores",
    "comissoes",
    "pos_emprego",
    "cessacao_cargo",
    "baseada_acoes",
    "remuneracao_",
)

DATE_PREFIXES = ("data_", "dt_")


def clean_names(columns: Iterable[object]) -> list[str]:
    """CVM's ``Camel_Case`` headers -> GetFREData-compatible snake_case."""
    cleaned: list[str] = []
    for column in columns:
        text = re.sub(r"[^0-9A-Za-z]+", "_", str(column)).strip("_").lower()
        cleaned.append(re.sub(r"_+", "_", text))
    return cleaned


def canonicalize(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize headers, the shared key block and value types."""
    frame = frame.copy()
    frame.columns = clean_names(frame.columns)
    frame = frame.rename(columns=RAW_RENAMES)
    frame = frame.loc[:, ~frame.columns.duplicated()]

    for column in frame.columns:
        if column in {"versao", "id_documento", "cd_cvm"}:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("Int64")
        elif column.startswith(DATE_PREFIXES):
            frame[column] = pd.to_datetime(frame[column], errors="coerce")
        elif column.startswith(NUMERIC_PREFIXES):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        else:
            frame[column] = frame[column].astype("string").str.strip()
            frame[column] = frame[column].replace("", pd.NA)

    ordered = [column for column in KEY_COLUMNS if column in frame.columns]
    ordered += [column for column in frame.columns if column not in ordered]
    return frame.loc[:, ordered].reset_index(drop=True)


def read_csv_bytes(payload: bytes) -> pd.DataFrame:
    """Read a CVM FRE CSV (Latin-1, ``;``)."""
    common: dict[str, object] = {
        "sep": ";",
        "encoding": "latin1",
        "dtype": "string",
        "keep_default_na": False,
        "na_values": [],
        "on_bad_lines": "warn",
    }
    try:
        return pd.read_csv(io.BytesIO(payload), quoting=csv.QUOTE_MINIMAL, engine="c", **common)
    except (pd.errors.ParserError, UnicodeDecodeError, csv.Error):
        return pd.read_csv(io.BytesIO(payload), quoting=csv.QUOTE_NONE, engine="python", **common)


def attach_cd_cvm(detail: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    """Add ``cd_cvm`` to a detail table using the document header."""
    if "cd_cvm" in detail.columns:
        detail = detail.drop(columns=["cd_cvm"])
    if master.empty:
        detail["cd_cvm"] = pd.Series(pd.NA, index=detail.index, dtype="Int64")
        return detail

    if "id_documento" in detail.columns and "id_documento" in master.columns:
        mapping = (
            master.loc[:, ["id_documento", "cd_cvm"]]
            .dropna(subset=["id_documento"])
            .drop_duplicates("id_documento")
        )
        merged = detail.merge(mapping, on="id_documento", how="left")
        if merged["cd_cvm"].notna().any():
            return merged

    mapping = (
        master.loc[:, ["cnpj", "cd_cvm"]]
        .dropna(subset=["cnpj"])
        .drop_duplicates("cnpj")
    )
    return detail.merge(mapping, on="cnpj", how="left")

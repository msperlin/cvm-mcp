"""Canonical schema for CVM DFP/ITR files.

The raw files are Latin-1, semicolon separated, decimal-comma CSVs published at
``dados.cvm.gov.br``.  Two details matter far more than they look:

1. **Never let a CSV parser honour quotes.**  ``DS_CONTA`` occasionally contains
   an unbalanced double quote (e.g. ``Titulos ... ("VJ")``), and some rows begin
   with a stray apostrophe.  A quoting parser silently shifts columns for those
   rows, so we parse with ``quoting=QUOTE_NONE`` and strip the artefacts.
2. **The column set drifts across years and documents.**  ``BPA``/``BPP`` ship
   14 columns (no ``DT_INI_EXERC``, no ``COLUNA_DF``, plus ``ST_CONTA_FIXA``),
   ``DRE`` ships 15, ``DMPL`` 16.  Everything is projected onto a canonical
   schema, with missing columns filled with nulls.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pandas as pd
import pyarrow as pa

DOC_TYPES: Final[tuple[str, ...]] = (
    "BPA",
    "BPP",
    "DRE",
    "DFC_MD",
    "DFC_MI",
    "DMPL",
    "DRA",
    "DVA",
)
DOC_FORMATS: Final[tuple[str, ...]] = ("con", "ind")

#: Canonical column order.  Mirrors ``GetDFPData2::clean_dfp_itr_data()``.
CANONICAL_COLUMNS: Final[tuple[str, ...]] = (
    "CNPJ_CIA",
    "CD_CVM",
    "DT_REFER",
    "DT_INI_EXERC",
    "DT_FIM_EXERC",
    "DENOM_CIA",
    "VERSAO",
    "GRUPO_DFP",
    "MOEDA",
    "ESCALA_MOEDA",
    "ORDEM_EXERC",
    "CD_CONTA",
    "DS_CONTA",
    "VL_CONTA",
    "COLUNA_DF",
)

TEXT_COLUMNS: Final[tuple[str, ...]] = (
    "CNPJ_CIA",
    "DENOM_CIA",
    "GRUPO_DFP",
    "MOEDA",
    "ESCALA_MOEDA",
    "CD_CONTA",
    "DS_CONTA",
    "COLUNA_DF",
)
DATE_COLUMNS: Final[tuple[str, ...]] = ("DT_REFER", "DT_INI_EXERC", "DT_FIM_EXERC")

#: Explicit Arrow schema for the Parquet cache.  Without it, a column that is
#: entirely null in one document (``DT_INI_EXERC`` is absent from BPA/BPP, for
#: instance) is written as Arrow ``null`` and the dataset then refuses to unify
#: it with the ``string`` type of another document.
ARROW_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        ("CNPJ_CIA", pa.string()),
        ("CD_CVM", pa.int64()),
        ("DT_REFER", pa.string()),
        ("DT_INI_EXERC", pa.string()),
        ("DT_FIM_EXERC", pa.string()),
        ("DENOM_CIA", pa.string()),
        ("VERSAO", pa.int64()),
        ("GRUPO_DFP", pa.string()),
        ("MOEDA", pa.string()),
        ("ESCALA_MOEDA", pa.string()),
        ("ORDEM_EXERC", pa.string()),
        ("CD_CONTA", pa.string()),
        ("DS_CONTA", pa.string()),
        ("VL_CONTA", pa.float64()),
        ("COLUNA_DF", pa.string()),
        ("VL_CONTA_BRL", pa.float64()),
        ("ACCOUNT_DEPTH", pa.int16()),
        ("FISCAL_YEAR", pa.int64()),
        ("PERIOD", pa.string()),
    ]
)

#: Parsing kwargs that work for every DFP/ITR member inside the CVM zips.
READ_CSV_KWARGS: Final[dict[str, object]] = {
    "sep": ";",
    "encoding": "latin1",
    "dtype": "string",
    "quoting": csv.QUOTE_NONE,
    "keep_default_na": False,
    "na_values": [],
    "engine": "c",
    "on_bad_lines": "warn",
}


class SchemaError(ValueError):
    """A CVM file does not look like a DFP/ITR statement file."""


def _strip_artifacts(values: pd.Series) -> pd.Series:
    """Remove the leading apostrophe and surrounding blanks CVM sometimes emits."""
    return (
        values.astype("string")
        .str.replace("\ufeff", "", regex=False)
        .str.replace("\u00a0", " ", regex=False)
        .str.strip()
        .str.removeprefix("'")
        .str.strip()
    )


def parse_number_column(values: pd.Series) -> pd.Series:
    """Reproduce ``readr::parse_number()`` for the ``VL_CONTA`` column.

    Handles decimal commas, dot thousands separators, ``R$`` prefixes and
    accounting negatives (``(1.234,56)``).
    """
    text = _strip_artifacts(values)
    text = text.mask(text.str.lower().isin(["", "nan", "none", "null", "na"]))

    negative = text.str.match(r"^\(.*\)$", na=False)
    text = text.str.replace(r"^\((.*)\)$", r"\1", regex=True)
    text = text.str.replace("R$", "", regex=False)
    text = text.str.replace(r"\s+", "", regex=True)

    has_comma = text.str.contains(",", na=False, regex=False)
    # "1.234,56" -> drop the dot thousands separator before turning "," into "."
    text = text.mask(has_comma, text.str.replace(".", "", regex=False))
    text = text.str.replace(",", ".", regex=False)
    text = text.str.replace(r"[^0-9eE+\-.]", "", regex=True)

    numbers = pd.to_numeric(text, errors="coerce").astype("float64")
    return numbers.mask(negative, -numbers)


def normalize_dates(values: pd.Series) -> pd.Series:
    """Return ISO ``YYYY-MM-DD`` strings (or null) for a date column."""
    text = _strip_artifacts(values)
    text = text.mask(text.str.lower().isin(["", "nan", "none", "null", "na"]))
    parsed = pd.to_datetime(text, errors="coerce", format="mixed", dayfirst=False)
    return parsed.dt.strftime("%Y-%m-%d")


#: Per-share accounts (``Lucro por Acao``) are already quoted in R$/share, so
#: the thousands multiplier of ``ESCALA_MOEDA`` must not be applied to them.
PER_SHARE_PREFIX: Final[str] = "3.99"


def brl_series(frame: pd.DataFrame) -> pd.Series:
    """Convert ``VL_CONTA`` to reais, honouring ``ESCALA_MOEDA``.

    ``MIL`` means the filing is expressed in thousands of reais.  Per-share
    accounts are the documented exception: CVM publishes them in R$/share even
    when the statement as a whole is in thousands.
    """
    scale = (
        frame["ESCALA_MOEDA"]
        .astype("string")
        .str.upper()
        .map({"MIL": 1000.0, "UNIDADE": 1.0})
        .fillna(1.0)
        .astype("float64")
    )
    scaled = frame["VL_CONTA"].astype("float64") * scale
    if "CD_CONTA" not in frame.columns:
        return scaled

    per_share = frame["CD_CONTA"].astype("string").str.startswith(PER_SHARE_PREFIX).fillna(False)
    return scaled.where(~per_share, frame["VL_CONTA"].astype("float64"))


def period_series(frame: pd.DataFrame) -> pd.Series:
    """Classify a row as ``annual``, ``ytd``, ``quarter`` or ``snapshot``.

    ITR filings publish *both* a year-to-date column and a quarter-only column
    for the same account and date, so they must stay distinguishable.  The
    fiscal-year-to-date row is the one that starts earliest within the same
    filing, which also handles non-calendar fiscal years and the first quarter
    (where the two columns are identical).  Balance-sheet lines have no
    ``DT_INI_EXERC`` at all: they are snapshots of a stock.
    """
    start = pd.to_datetime(frame.get("DT_INI_EXERC"), errors="coerce", format="mixed")
    end = pd.to_datetime(frame.get("DT_FIM_EXERC"), errors="coerce", format="mixed")
    days = (end - start).dt.days

    period = pd.Series("snapshot", index=frame.index, dtype="object")
    period = period.mask(start.notna(), "ytd")

    if {"CNPJ_CIA", "DT_REFER"}.issubset(frame.columns):
        keys: list[pd.Series] = [frame["CNPJ_CIA"], frame["DT_REFER"]]
        if "ORDEM_EXERC" in frame.columns:
            keys.append(frame["ORDEM_EXERC"])
        earliest = start.groupby(keys, dropna=False).transform("min")
        period = period.mask(start.notna() & (start != earliest), "quarter")

    period = period.mask(days > 300, "annual")
    return period


def normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Project a raw CVM statement file onto the canonical schema."""
    if df.empty:
        return pd.DataFrame({column: pd.Series(dtype="object") for column in CANONICAL_COLUMNS})

    df = df.rename(columns=lambda column: str(column).strip())
    missing = [column for column in CANONICAL_COLUMNS if column not in df.columns]
    for column in missing:
        df[column] = pd.NA
    df = df.loc[:, list(CANONICAL_COLUMNS)].copy()

    for column in TEXT_COLUMNS:
        df[column] = _strip_artifacts(df[column]).astype("object")

    for column in DATE_COLUMNS:
        df[column] = normalize_dates(df[column]).astype("object")

    df["CD_CVM"] = pd.to_numeric(df["CD_CVM"], errors="coerce").astype("Int64")
    df["VERSAO"] = pd.to_numeric(df["VERSAO"], errors="coerce").astype("Int64")
    df["VL_CONTA"] = parse_number_column(df["VL_CONTA"]).astype("float64")

    df["ESCALA_MOEDA"] = df["ESCALA_MOEDA"].str.upper()
    df["VL_CONTA_BRL"] = brl_series(df)

    df["ORDEM_EXERC"] = df["ORDEM_EXERC"].str.upper()
    df["ACCOUNT_DEPTH"] = (
        df["CD_CONTA"].str.count(r"\.").fillna(0).astype("int16")
    )
    df["FISCAL_YEAR"] = (
        pd.to_numeric(
            df["DT_REFER"].str.slice(0, 4),
            errors="coerce",
        )
        .astype("Int64")
    )
    df["PERIOD"] = period_series(df)

    df = df.dropna(subset=["CNPJ_CIA", "DT_REFER", "CD_CONTA"])
    return df.reset_index(drop=True)


def keep_latest_exercise(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only the current period (``ORDEM_EXERC == 'ULTIMO'``).

    Equivalent to ``possible_cases[2]`` in ``GetDFPData2`` but tolerant of
    accent variants and files that only carry a single exercise order.
    """
    if df.empty or "ORDEM_EXERC" not in df.columns:
        return df

    order = (
        df["ORDEM_EXERC"]
        .astype("string")
        .str.upper()
        .str.normalize("NFKD")
        .str.encode("ascii", "ignore")
        .str.decode("ascii")
    )
    current = order.eq("ULTIMO")
    if current.any():
        return df.loc[current].reset_index(drop=True)
    return df


def deduplicate_versions(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the highest ``VERSAO`` for each reported figure.

    Amended filings can list the same account more than once.
    """
    if df.empty or "VERSAO" not in df.columns:
        return df
    subset = [
        column
        for column in (
            "CNPJ_CIA",
            "DT_REFER",
            "GRUPO_DFP",
            "CD_CONTA",
            "COLUNA_DF",
            "ORDEM_EXERC",
            # ITR filings carry a year-to-date row and a quarter-only row for
            # the same account and date; they are different figures, not
            # duplicate versions.
            "DT_INI_EXERC",
            "DT_FIM_EXERC",
        )
        if column in df.columns
    ]
    ordered = df.sort_values("VERSAO", na_position="first")
    return ordered.drop_duplicates(subset=subset, keep="last").reset_index(drop=True)


def read_statement_csv(path_or_buffer, *, clean: bool = True) -> pd.DataFrame:
    """Read one DFP/ITR CSV member and return a normalized frame."""
    df = pd.read_csv(path_or_buffer, **READ_CSV_KWARGS)
    if df.shape[1] < 10:
        raise SchemaError(
            f"expected at least 10 columns, got {df.shape[1]}: {list(df.columns)[:6]}"
        )
    df = normalize_frame(df)
    if clean:
        df = keep_latest_exercise(df)
        df = deduplicate_versions(df)
    return df


def member_name(kind: str, doc: str, fmt: str, year: int) -> str:
    """Name of the CSV member inside ``{kind}_cia_aberta_{year}.zip``."""
    return f"{kind.lower()}_cia_aberta_{doc}_{fmt}_{year}.csv"


def find_member(names: Sequence[str], doc: str, fmt: str, year: int) -> str | None:
    """Locate a CSV member, tolerating case and the odd historical name."""
    wanted = f"_{doc}_{fmt}_{year}.csv".lower()
    for name in names:
        if name.lower().endswith(wanted):
            return name
    return None


def guess_year(path: Path | str) -> int | None:
    """Extract a four-digit year from a file name."""
    import re

    match = re.search(r"(19|20)\d{2}", Path(str(path)).name)
    return int(match.group(0)) if match else None


def doc_label(doc: str, fmt: str) -> str:
    """Human label for a document/format pair."""
    labels = {
        "BPA": "Balanco Patrimonial Ativo",
        "BPP": "Balanco Patrimonial Passivo",
        "DRE": "Demonstracao do Resultado",
        "DFC_MD": "Fluxo de Caixa (metodo direto)",
        "DFC_MI": "Fluxo de Caixa (metodo indireto)",
        "DMPL": "Mutacoes do Patrimonio Liquido",
        "DRA": "Demonstracao do Resultado Abrangente",
        "DVA": "Demonstracao do Valor Adicionado",
    }
    base = labels.get(doc, doc)
    suffix = "consolidado" if fmt == "con" else "individual"
    return f"{base} - {suffix}"

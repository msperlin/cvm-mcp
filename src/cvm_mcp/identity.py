"""The identity layer every other domain depends on.

CVM keys everything on CNPJ, users think in tickers, and the FRE/DFP tools key
on ``cd_cvm``. This module is the single place that translates between the
three, and ranks company-name matches so the obvious answer comes first (a
plain alphabetical sort puts "PETROBRAS QUIMICA" before "PETROLEO BRASILEIRO").
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from .normalize import fold, normalize_cnpj


def registry_mask(registry: pd.DataFrame, status: str | None) -> pd.Series:
    if not status:
        return pd.Series(True, index=registry.index)
    wanted = fold(status)
    situation = registry["SIT"].fillna("").map(fold)
    return situation.str.contains(wanted, regex=False)


def search_matches(registry: pd.DataFrame, text: str) -> pd.DataFrame:
    """Rank companies by (active status, exact name, prefix, name length)."""
    if text.isdigit():
        return registry.loc[registry["CD_CVM"] == int(text)]

    needle = fold(text)
    social = registry["DENOM_SOCIAL"].fillna("").map(fold)
    commercial = registry["DENOM_COMERC"].fillna("").map(fold)
    cnpj = registry["CNPJ_CIA"].fillna("")

    in_social = social.str.contains(needle, regex=False)
    in_commercial = commercial.str.contains(needle, regex=False)
    mask = in_social | in_commercial | cnpj.str.contains(text, regex=False)
    if not mask.any():
        return registry.iloc[0:0]

    tier = pd.Series(5, index=registry.index, dtype="int64")
    tier = tier.mask(in_social | in_commercial, 4)
    tier = tier.mask(social.str.startswith(needle, na=False), 3)
    tier = tier.mask(commercial.str.startswith(needle, na=False), 2)
    tier = tier.mask(social.eq(needle) | commercial.eq(needle), 1)
    tier = tier.mask(social.eq(needle) & commercial.eq(needle), 0)

    situation = registry["SIT"].fillna("").map(fold)
    ranked = registry.loc[mask].assign(
        _inactive=(~situation.eq("ativo")).astype("int64").loc[mask],
        _tier=tier.loc[mask],
        _length=social.loc[mask].str.len(),
    )
    return ranked.sort_values(
        ["_inactive", "_tier", "_length", "DENOM_SOCIAL"]
    ).drop(columns=["_inactive", "_tier", "_length"])


def find_by_cnpj(registry: pd.DataFrame, cnpj: str) -> pd.DataFrame:
    digits = normalize_cnpj(cnpj) or str(cnpj)
    return registry.loc[registry["CNPJ_CIA"].fillna("").map(normalize_cnpj) == digits]


def clean_json_value(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date().isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            return value
    return value


def company_record(row: Any, *, extended: bool = False) -> dict[str, Any]:
    payload = {
        "cd_cvm": int(row.CD_CVM),
        "company": clean_json_value(row.DENOM_SOCIAL),
        "commercial_name": clean_json_value(row.DENOM_COMERC),
        "cnpj": clean_json_value(row.CNPJ_CIA),
        "sector": clean_json_value(row.SETOR_ATIV),
        "status": clean_json_value(row.SIT),
        "market": clean_json_value(row.TP_MERC),
    }
    if extended:
        payload["city"] = clean_json_value(row.MUN)
        payload["state"] = clean_json_value(row.UF)
    return payload

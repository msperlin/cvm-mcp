"""Public offering tools (CVM/DISTRIB): search, issuance volume, league table.

CVM split the flow in two files and the split is chronological:

* ``oferta_distribuicao.csv``  — the ICVM 400 flow (1988 onwards, and the
  ordinary-rite 160 cases).
* ``oferta_resolucao_160.csv`` — the RCVM 160 flow (2023 onwards).

Both are read and harmonized into one frame, tagged with ``source``, otherwise a
2024 query would return only a handful of rows while the real market is in the
160 file.
"""

from __future__ import annotations

from typing import Annotated, Literal

import pandas as pd
from pydantic import Field

from .app import mcp
from .fetch import describe_error
from .formatting import to_csv
from .normalize import fold
from .runtime import store
from .toolkit import READ_ONLY, error, json_out

COMMON_COLUMNS = (
    "source",
    "numero_processo",
    "nome_emissor",
    "cnpj_emissor",
    "tipo_ativo",
    "rito",
    "data_registro",
    "data_encerramento",
    "quantidade",
    "preco_unitario",
    "valor_total",
    "nome_lider",
    "serie",
)


def _harmonize(raw: pd.DataFrame, source: str) -> pd.DataFrame:
    """Map either raw table onto the common offering schema."""
    if source == "distribuicao":
        mapping = {
            "tipo_ativo": "tipo_ativo",
            "rito_oferta": "rito",
            "data_registro_oferta": "data_registro",
            "data_encerramento_oferta": "data_encerramento",
            "quantidade_total": "quantidade",
            "preco_unitario": "preco_unitario",
            "valor_total": "valor_total",
            "serie": "serie",
        }
    else:
        mapping = {
            "valor_mobiliario": "tipo_ativo",
            "data_registro": "data_registro",
            "data_encerramento": "data_encerramento",
            "qtde_total_registrada": "quantidade",
            "valor_total_registrado": "valor_total",
        }

    frame = pd.DataFrame(index=raw.index)
    frame["source"] = source
    for target in ("numero_processo", "nome_emissor", "cnpj_emissor", "nome_lider"):
        frame[target] = raw[target] if target in raw.columns else pd.NA
    for column, target in mapping.items():
        frame[target] = raw[column] if column in raw.columns else pd.NA

    if source != "distribuicao":
        frame["rito"] = "RCVM 160"
        frame["preco_unitario"] = pd.NA
        frame["serie"] = pd.NA
    return frame.loc[:, COMMON_COLUMNS]


async def _offerings(
    *,
    asset_type: str | None = None,
    rite: str | None = None,
    issuer: str | None = None,
    first_date: str | None = None,
    last_date: str | None = None,
    sources: tuple[str, ...] = ("distribuicao", "resolucao_160"),
) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for source in sources:
        try:
            await store.ensure("oferta", source)
            raw = store.read("oferta", source)
        except Exception:  # noqa: BLE001, S112 - one file missing must not kill the query
            continue
        frames.append(_harmonize(raw, source))
    if not frames:
        return pd.DataFrame(columns=COMMON_COLUMNS)

    frame = pd.concat(frames, ignore_index=True)

    if asset_type:
        wanted = fold(asset_type)
        frame = frame.loc[frame["tipo_ativo"].fillna("").map(fold).str.contains(wanted, regex=False)]
    if rite:
        wanted = fold(rite)
        frame = frame.loc[frame["rito"].fillna("").map(fold).str.contains(wanted, regex=False)]
    if issuer:
        wanted = fold(issuer)
        frame = frame.loc[frame["nome_emissor"].fillna("").map(fold).str.contains(wanted, regex=False)]

    dates = frame["data_registro"].astype("string")
    if first_date:
        frame = frame.loc[dates >= str(first_date)]
    if last_date:
        frame = frame.loc[dates <= str(last_date)]
    return frame.reset_index(drop=True)


@mcp.tool(annotations=READ_ONLY)
async def search_offerings(
    issuer: Annotated[str | None, Field(description="Issuer name (partial).")] = None,
    asset_type: Annotated[
        str | None, Field(description="Asset type, e.g. 'ACOES', 'DEBENTURES', 'COTAS', 'CRI'.")
    ] = None,
    rite: Annotated[str | None, Field(description="Rite, e.g. 'ICVM 400' or 'RCVM 160'.")] = None,
    first_date: Annotated[str | None, Field(description="First registration date (YYYY-MM-DD).")] = None,
    last_date: Annotated[str | None, Field(description="Last registration date (YYYY-MM-DD).")] = None,
    limit: Annotated[int, Field(description="Maximum rows.", ge=1, le=5000)] = 100,
) -> str:
    """Search registered, exempt and restricted public offerings since 1988."""
    try:
        frame = await _offerings(
            asset_type=asset_type,
            rite=rite,
            issuer=issuer,
            first_date=first_date,
            last_date=last_date,
        )
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    if frame.empty:
        return json_out({"count": 0, "hint": "Loosen the filters; asset types vary across decades."})
    total = len(frame)
    frame = frame.sort_values("data_registro", ascending=False, na_position="last").head(int(limit))

    columns = [
        "source",
        "numero_processo",
        "nome_emissor",
        "tipo_ativo",
        "rito",
        "data_registro",
        "data_encerramento",
        "quantidade",
        "preco_unitario",
        "valor_total",
        "nome_lider",
    ]
    header = f"# source: CVM/Ofertas Públicas | matches={total} shown={len(frame)}\n"
    return header + to_csv(frame, columns=columns)


@mcp.tool(annotations=READ_ONLY)
async def issuance_volume(
    group_by: Annotated[
        Literal["year", "asset_type", "issuer", "lead", "rite"],
        Field(description="Dimension to aggregate by."),
    ] = "year",
    asset_type: Annotated[str | None, Field(description="Optional asset type filter.")] = None,
    rite: Annotated[str | None, Field(description="Optional rite filter.")] = None,
    first_date: Annotated[str | None, Field(description="First registration date (YYYY-MM-DD).")] = None,
    last_date: Annotated[str | None, Field(description="Last registration date (YYYY-MM-DD).")] = None,
    limit: Annotated[int, Field(description="Rows returned.", ge=1, le=500)] = 50,
) -> str:
    """Total offering volume, grouped by year, asset type, issuer, lead or rite.

    `valor_total` is as reported by CVM; amounts from different decades are not
    inflation-adjusted.
    """
    try:
        frame = await _offerings(
            asset_type=asset_type, rite=rite, first_date=first_date, last_date=last_date
        )
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))
    if frame.empty:
        return json_out({"count": 0})

    if group_by == "year":
        keys = frame["data_registro"].astype("string").str.slice(0, 4)
    elif group_by == "asset_type":
        keys = frame["tipo_ativo"]
    elif group_by == "issuer":
        keys = frame["nome_emissor"]
    elif group_by == "lead":
        keys = frame["nome_lider"]
    else:
        keys = frame["rito"]

    frame = frame.assign(key=keys).dropna(subset=["key"])
    grouped = (
        frame.groupby("key", dropna=False)
        .agg(offerings=("numero_processo", "nunique"), volume=("valor_total", "sum"))
        .reset_index()
        .rename(columns={"key": group_by})
        .sort_values("volume", ascending=False)
        .head(int(limit))
    )
    header = f"# source: CVM/Ofertas Públicas | grouped by {group_by} | volume as reported\n"
    return header + to_csv(grouped)


@mcp.tool(annotations=READ_ONLY)
async def league_table(
    first_date: Annotated[str | None, Field(description="First registration date (YYYY-MM-DD).")] = None,
    last_date: Annotated[str | None, Field(description="Last registration date (YYYY-MM-DD).")] = None,
    asset_type: Annotated[str | None, Field(description="Optional asset type filter.")] = None,
    rite: Annotated[str | None, Field(description="Optional rite filter.")] = None,
    limit: Annotated[int, Field(description="Underwriters returned.", ge=1, le=100)] = 25,
) -> str:
    """Rank lead underwriters by total offering volume (the ECM/DCM league table)."""
    try:
        frame = await _offerings(
            asset_type=asset_type, rite=rite, first_date=first_date, last_date=last_date
        )
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))
    if frame.empty:
        return json_out({"count": 0})

    grouped = (
        frame.dropna(subset=["nome_lider"])
        .assign(lead=frame["nome_lider"].astype("string").str.strip())
        .groupby("lead")
        .agg(
            offerings=("numero_processo", "nunique"),
            volume=("valor_total", "sum"),
            issuers=("nome_emissor", "nunique"),
        )
        .reset_index()
        .sort_values("volume", ascending=False)
        .head(int(limit))
    )
    if not grouped.empty:
        total = grouped["volume"].sum()
        grouped["share_pct"] = (grouped["volume"] / total * 100).round(2) if total else None

    header = "# source: CVM/Ofertas Públicas | lead underwriter league table\n"
    return header + to_csv(grouped, columns=["lead", "offerings", "volume", "issuers", "share_pct"])

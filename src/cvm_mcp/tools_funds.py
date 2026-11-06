"""Fund tools.

The CVM splits funds across datasets, and the split is by *instrument*, not by
manager: FIF/FAPI report daily, FII/FIDC/FIAGRO report monthly. The tools here
hide that: `get_fund_nav` and `get_fund_portfolio` resolve the class CNPJ once,
look at its type and read the right dataset (or tell you plainly that FIP is
quarterly and lives elsewhere).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import Field

from . import registry
from .app import mcp
from .fetch import describe_error
from .formatting import to_csv
from .normalize import fold, normalize_cnpj
from .runtime import store
from .store import NoData
from .toolkit import READ_ONLY, error, json_out

# ---------------------------------------------------------------------------
# periods and fund families


def _month_key(value: str | None) -> str:
    """Accept ``2025-10`` or ``202510`` and return ``202510``."""
    if value is None:
        return ""
    text = str(value).strip().replace("-", "").replace("/", "")
    if not re.fullmatch(r"\d{6}", text):
        raise ValueError(f"period must be YYYY-MM or YYYYMM, got {value!r}")
    return text


def _month_iso(period: str) -> str:
    return f"{period[:4]}-{period[4:]}"


def _shift_month(period: str, delta: int) -> str:
    year, month = int(period[:4]), int(period[4:])
    index = year * 12 + (month - 1) + delta
    return f"{index // 12:04d}{index % 12 + 1:02d}"


def _month_range(first: str, last: str) -> list[str]:
    if first > last:
        raise ValueError("first_period must be on or before last_period")
    periods: list[str] = []
    current = first
    while current <= last:
        periods.append(current)
        current = _shift_month(current, 1)
        if len(periods) > 240:
            raise ValueError("period range is too wide (240 months max)")
    return periods


def _years_of(months: Sequence[str]) -> list[str]:
    return sorted({month[:4] for month in months})


def _fund_family(type_text: Any) -> str:
    """Map the CVM class type to the dataset family that carries its data."""
    text = fold(str(type_text or ""))
    if "fidc" in text:
        return "fidc"
    if "fiagro" in text:
        return "fiagro"
    if "fii" in text or "fiim" in text:
        return "fii"
    if "fip" in text:
        return "fip"
    return "fi"


async def _latest_month_for(family: str) -> str:
    if family == "fii":
        years = await store.client.periods(registry.get("fii_inf_mensal"))
        latest = max(years)
        await store.ensure("fii_inf_mensal", "geral", latest)
        frame = store.read(
            "fii_inf_mensal", "geral", periods=[latest], columns=["data_referencia"]
        )
        value = frame["data_referencia"].dropna().max()
        return str(value)[:7].replace("-", "")

    dataset_id = {
        "fi": "fi_inf_diario",
        "fidc": "fidc_inf_mensal",
        "fiagro": "fiagro_inf_mensal",
    }.get(family)
    if dataset_id is None:
        raise ValueError(f"dataset family {family!r} has no monthly series")
    available = await store.client.periods(registry.get(dataset_id))
    return str(max(available))


async def _latest_month() -> str:
    return await _latest_month_for("fi")


async def _resolve_fund(query: str) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Return ``(cnpj, error, record)`` for a fund name, class CNPJ or code."""
    text = str(query).strip()
    if not text:
        return None, "fund is empty", None

    digits = normalize_cnpj(text)
    try:
        await store.ensure("fi_cad", "classe")
        frame = store.read("fi_cad", "classe")
    except Exception as exc:  # noqa: BLE001
        return None, describe_error(exc), None

    if digits:
        hits = frame.loc[frame["cnpj"].fillna("") == digits]
        if hits.empty:
            hits = frame.loc[frame["cnpj_classe"].fillna("").map(normalize_cnpj) == digits]
        if hits.empty:
            return (
                None,
                (
                    f"CNPJ {digits} is not a fund/class in the CVM registry. "
                    "FIIs and securitisation vehicles have separate datasets."
                ),
                None,
            )
    else:
        needle = fold(text)
        names = frame["denominacao_social"].fillna("").map(fold)
        hits = frame.loc[names.str.contains(needle, regex=False)]
        if hits.empty and str(text).isdigit():
            hits = frame.loc[frame["codigo_cvm"].astype(str) == str(text)]

    if hits.empty:
        return None, f"no fund/class matches {text!r}", None
    row = hits.iloc[0]
    cnpj = normalize_cnpj(row.get("cnpj")) or normalize_cnpj(row.get("cnpj_classe"))
    if not cnpj:
        return None, "the matched class has no CNPJ", None
    record = {
        "cnpj": cnpj,
        "fund": row.get("denominacao_social"),
        "cvm_code": row.get("codigo_cvm"),
        "type": row.get("tipo_classe"),
        "status": row.get("situacao"),
        "classification": row.get("classificacao_anbima"),
        "family": _fund_family(row.get("tipo_classe")),
    }
    return cnpj, None, {key: _clean(value) for key, value in record.items()}


def _clean(value: Any) -> Any:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):
            return value
    return value


def _require(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """Ensure the requested columns exist, filling missing ones with nulls."""
    frame = frame.copy()
    for column in columns:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame


def _melt(frame: pd.DataFrame, id_columns: Sequence[str], value_name: str = "value") -> pd.DataFrame:
    """Turn a one-row-per-fund wide report into long ``(class, value)`` form."""
    ids = [column for column in id_columns if column in frame.columns]
    numeric = [
        column
        for column in frame.columns
        if column not in ids and pd.api.types.is_numeric_dtype(frame[column])
    ]
    long = frame.melt(id_vars=ids, value_vars=numeric, var_name="component", value_name=value_name)
    long[value_name] = pd.to_numeric(long[value_name], errors="coerce")
    long = long.loc[long[value_name].fillna(0) != 0]
    return long.sort_values(value_name, ascending=False)


# ---------------------------------------------------------------------------
# search


@mcp.tool(annotations=READ_ONLY)
async def search_fund(
    query: Annotated[
        str,
        Field(description="Fund/class name (partial), CNPJ or CVM fund code. E.g. 'itau', '42737077000199'."),
    ],
    limit: Annotated[int, Field(description="Maximum matches.", ge=1, le=50)] = 10,
) -> str:
    """Find a fund or class and get the CNPJ used by every fund tool.

    The CVM keys fund data on the *class* CNPJ. The result carries a `family`
    (`fi`, `fii`, `fidc`, `fiagro`, `fip`) that tells which monthly report
    covers it — `get_fund_nav` routes automatically.
    """
    try:
        await store.ensure("fi_cad", "classe")
        frame = store.read("fi_cad", "classe")
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    text = str(query).strip()
    if not text:
        return error("query is empty")

    digits = normalize_cnpj(text)
    if digits:
        hits = frame.loc[frame["cnpj"].fillna("") == digits]
    else:
        needle = fold(text)
        names = frame["denominacao_social"].fillna("").map(fold)
        hits = frame.loc[names.str.contains(needle, regex=False)]
        if hits.empty and text.isdigit():
            hits = frame.loc[frame["codigo_cvm"].astype(str).str.startswith(text)]

    if hits.empty:
        return json_out(
            {
                "query": text,
                "count": 0,
                "hint": "Try a shorter name fragment, or the class CNPJ (14 digits).",
            }
        )

    results = [
        {
            "cnpj": _clean(row.cnpj) or _clean(getattr(row, "cnpj_classe", None)),
            "fund": _clean(row.denominacao_social),
            "cvm_code": _clean(row.codigo_cvm),
            "type": _clean(row.tipo_classe),
            "family": _fund_family(row.tipo_classe),
            "status": _clean(row.situacao),
            "classification": _clean(row.classificacao_anbima),
        }
        for row in hits.head(int(limit)).itertuples(index=False)
    ]
    return json_out({"query": text, "count": len(results), "results": results})


# ---------------------------------------------------------------------------
# NAV / monthly series


@mcp.tool(annotations=READ_ONLY)
async def get_fund_nav(
    fund: Annotated[str, Field(description="Fund name (partial) or class CNPJ.")],
    first_period: Annotated[str | None, Field(description="First month (YYYY-MM or YYYYMM).")] = None,
    last_period: Annotated[str | None, Field(description="Last month. Default: latest published.")] = None,
    max_rows: Annotated[int, Field(description="Maximum rows.", ge=1, le=20_000)] = 5000,
) -> str:
    """NAV time series for any fund, routed to the right CVM dataset.

    * FIF/FAPI: daily quota, net asset value, subscriptions, redemptions, unitholders.
    * FII: monthly net asset value, book value per share, unitholders, dividend yield.
    * FIDC: monthly net asset value and average NAV.
    * FIAGRO: monthly net asset value, unitholders, returns and fees.
    """
    cnpj, problem, record = await _resolve_fund(fund)
    if cnpj is None:
        return error(problem or "fund not found")
    family = record.get("family") if record else "fi"

    if family == "fip":
        return error(
            "FIP funds report quarterly, not monthly",
            hint="Use get_fre_table or run_query over the FIP quarterly dataset (not yet in the registry).",
        )

    try:
        last = _month_key(last_period) if last_period else await _latest_month_for(family)
        first = _month_key(first_period) if first_period else _shift_month(last, -11)
        months = _month_range(first, last)
    except ValueError as exc:
        return error(str(exc))

    try:
        if family == "fii":
            return await _fii_nav(cnpj, first, last, months, max_rows)
        if family == "fidc":
            return await _fidc_nav(cnpj, first, last, months, max_rows)
        if family == "fiagro":
            return await _fiagro_nav(cnpj, first, last, months, max_rows)
        return await _fi_nav(cnpj, first, last, months, max_rows)
    except NoData:
        return error(
            f"no report for cnpj={cnpj} in {first}-{last}",
            hint="The fund may have no filing in that window, or the month is not published yet.",
        )
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))


async def _fi_nav(cnpj: str, first: str, last: str, months: list[str], max_rows: int) -> str:
    await store.ensure_many("fi_inf_diario", months, ["informe"])
    frame = store.read("fi_inf_diario", "informe", periods=months, equals={"cnpj": cnpj})
    frame = frame.sort_values("dt_comptc").copy()
    frame["daily_return_pct"] = frame["vl_quota"].pct_change() * 100.0
    if len(frame) > max_rows:
        return error(f"query would return {len(frame)} rows, above {max_rows}")
    columns = [
        "cnpj_fundo_classe",
        "dt_comptc",
        "vl_quota",
        "vl_patrim_liq",
        "captc_dia",
        "resg_dia",
        "nr_cotst",
        "daily_return_pct",
    ]
    header = f"# source: CVM/FI Informe Diário | cnpj={cnpj} | {first}-{last}\n"
    return header + to_csv(frame, columns=columns)


async def _fii_nav(cnpj: str, first: str, last: str, months: list[str], max_rows: int) -> str:
    years = _years_of(months)
    await store.ensure_many("fii_inf_mensal", years, ["geral", "complemento"])
    geral = store.read(
        "fii_inf_mensal",
        "geral",
        periods=years,
        equals={"cnpj": cnpj},
        columns=[
            "cnpj",
            "data_referencia",
            "nome_fundo_classe",
            "segmento_atuacao",
            "mandato",
            "tipo_gestao",
            "quantidade_cotas_emitidas",
        ],
    )
    complement = store.read(
        "fii_inf_mensal",
        "complemento",
        periods=years,
        equals={"cnpj": cnpj},
        columns=[
            "cnpj",
            "data_referencia",
            "total_numero_cotistas",
            "patrimonio_liquido",
            "valor_patrimonial_cotas",
            "percentual_rentabilidade_efetiva_mes",
            "percentual_dividend_yield_mes",
            "percentual_despesas_taxa_administracao",
        ],
    )
    frame = geral.merge(complement, on=["cnpj", "data_referencia"], how="left")
    frame["month"] = frame["data_referencia"].astype("string").str.slice(0, 7)
    frame = frame.loc[
        (frame["month"] >= _month_iso(first)) & (frame["month"] <= _month_iso(last))
    ].sort_values("data_referencia")
    if frame.empty:
        raise NoData("no FII report in the requested window")
    if len(frame) > max_rows:
        return error(f"query would return {len(frame)} rows, above {max_rows}")

    columns = [
        "month",
        "nome_fundo_classe",
        "segmento_atuacao",
        "mandato",
        "quantidade_cotas_emitidas",
        "total_numero_cotistas",
        "patrimonio_liquido",
        "valor_patrimonial_cotas",
        "percentual_rentabilidade_efetiva_mes",
        "percentual_dividend_yield_mes",
        "percentual_despesas_taxa_administracao",
    ]
    header = f"# source: CVM/FII Informe Mensal | cnpj={cnpj} | {first}-{last} | values in BRL\n"
    return header + to_csv(frame, columns=columns)


async def _fidc_nav(cnpj: str, first: str, last: str, months: list[str], max_rows: int) -> str:
    await store.ensure_many("fidc_inf_mensal", months, ["tab_iv"])
    frame = store.read("fidc_inf_mensal", "tab_iv", periods=months, equals={"cnpj": cnpj})
    frame = frame.sort_values("dt_comptc")
    if len(frame) > max_rows:
        return error(f"query would return {len(frame)} rows, above {max_rows}")
    columns = ["dt_comptc", "denom_social", "tab_iv_a_vl_pl", "tab_iv_b_vl_pl_medio"]
    header = f"# source: CVM/FIDC Informe Mensal | cnpj={cnpj} | {first}-{last} | values in BRL\n"
    return header + to_csv(frame, columns=columns)


async def _fiagro_nav(cnpj: str, first: str, last: str, months: list[str], max_rows: int) -> str:
    await store.ensure_many("fiagro_inf_mensal", months, ["informe"])
    frame = store.read("fiagro_inf_mensal", "informe", periods=months, equals={"cnpj": cnpj})
    frame["month"] = frame["data_referencia"].astype("string").str.slice(0, 7)
    frame = frame.loc[
        (frame["month"] >= _month_iso(first)) & (frame["month"] <= _month_iso(last))
    ].sort_values("data_referencia")
    if frame.empty:
        raise NoData("no FIAGRO report in the requested window")
    if len(frame) > max_rows:
        return error(f"query would return {len(frame)} rows, above {max_rows}")
    columns = [
        "month",
        "nome_classe",
        "numero_cotistas",
        "patrimonio_liquido",
        "cotas_emitidas",
        "valor_patrimonial_cotas",
        "rentabilidade_efetiva_mes",
        "dividend_yield_mes",
        "total_investido",
    ]
    header = f"# source: CVM/FIAGRO Informe Mensal | cnpj={cnpj} | {first}-{last} | values in BRL\n"
    return header + to_csv(frame, columns=columns)


# ---------------------------------------------------------------------------
# flows


@mcp.tool(annotations=READ_ONLY)
async def get_fund_flows(
    fund: Annotated[str, Field(description="Fund name (partial) or class CNPJ.")],
    first_period: Annotated[str | None, Field(description="First month.")] = None,
    last_period: Annotated[str | None, Field(description="Last month. Default: latest.")] = None,
) -> str:
    """Monthly subscriptions, redemptions and net flow for one FI fund.

    Only FIF/FAPI report daily subscriptions and redemptions (Informe Diário).
    FII, FIDC and FIAGRO do not publish a flow series in the monthly report.
    """
    cnpj, problem, record = await _resolve_fund(fund)
    if cnpj is None:
        return error(problem or "fund not found")
    if record and record.get("family") != "fi":
        return error(
            f"no flow series for a {record.get('family')} fund",
            hint="Only FIF/FAPI report subscriptions and redemptions (Informe Diário).",
        )

    try:
        last = _month_key(last_period) if last_period else await _latest_month()
        first = _month_key(first_period) if first_period else _shift_month(last, -11)
        periods = _month_range(first, last)
        await store.ensure_many("fi_inf_diario", periods, ["informe"])
        frame = store.read("fi_inf_diario", "informe", periods=periods, equals={"cnpj": cnpj})
    except NoData:
        return error(
            f"no daily report for cnpj={cnpj} in {first}-{last}",
            hint="The Informe Diário covers FI (ICVM 555). FIIs, FIDCs and CRI/CRA "
            "vehicles report in separate CVM datasets.",
        )
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    frame = frame.copy()
    frame["month"] = frame["_period"].astype(str)
    grouped = (
        frame.groupby("month")
        .agg(
            subscriptions=("captc_dia", "sum"),
            redemptions=("resg_dia", "sum"),
            net_asset_value=("vl_patrim_liq", "last"),
            unitholders=("nr_cotst", "last"),
        )
        .reset_index()
    )
    grouped["net_flow"] = grouped["subscriptions"] - grouped["redemptions"]
    header = f"# source: CVM/FI | cnpj={cnpj} | values in BRL\n"
    return header + to_csv(grouped)


# ---------------------------------------------------------------------------
# portfolio


@mcp.tool(annotations=READ_ONLY)
async def get_fund_portfolio(
    fund: Annotated[str, Field(description="Fund name (partial) or class CNPJ.")],
    period: Annotated[str | None, Field(description="Month (YYYY-MM). Default: latest available.")] = None,
    block: Annotated[
        str, Field(description="FI only: '1'..'8' for one CDA block, 'all' for every block.")
    ] = "all",
    max_rows: Annotated[int, Field(description="Maximum rows.", ge=1, le=50_000)] = 5000,
) -> str:
    """Holdings of any fund, routed to the right dataset.

    * FIF/FIAGRO: CDA portfolio (asset, issuer, quantity, market value, cost).
    * FII: balance-sheet composition (properties, CRI, FII quotas, cash...).
    * FIDC: credit portfolio by segment (industrial, real estate, payroll...).

    The output is long-form: one row per `component`, with its value in BRL.
    """
    cnpj, problem, record = await _resolve_fund(fund)
    if cnpj is None:
        return error(problem or "fund not found")
    family = record.get("family") if record else "fi"

    try:
        if family == "fii":
            return await _fii_portfolio(cnpj, period, max_rows)
        if family == "fidc":
            return await _fidc_portfolio(cnpj, period, max_rows)
        if family == "fip":
            return error(
                "FIP portfolios are reported quarterly",
                hint="Use run_query over the FIP quarterly dataset (not yet in the registry).",
            )
        if family == "fiagro":
            # Structured funds live in the cda_fie member of the CDA archive.
            return await _cda_portfolio(cnpj, period, ["fie"], max_rows)
        return await _fi_portfolio(cnpj, period, block, max_rows)
    except NoData:
        return error(
            f"no portfolio for cnpj={cnpj} in {period or 'the latest period'}",
            hint="The fund may not report this document, or the month is not published yet.",
        )
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))


async def _fi_portfolio(cnpj: str, period: str | None, block: str, max_rows: int) -> str:
    blocks = list(range(1, 9)) if str(block).lower() == "all" else [int(block)]
    return await _cda_portfolio(cnpj, period, [f"blc_{index}" for index in blocks], max_rows)


async def _cda_portfolio(
    cnpj: str, period: str | None, tables: list[str], max_rows: int
) -> str:
    if period:
        target = _month_key(period)
    else:
        available = await store.client.periods(registry.get("fi_cda"))
        target = str(max(available))

    await store.ensure_many("fi_cda", [target], tables)

    frames: list[pd.DataFrame] = []
    for table in tables:
        try:
            part = store.read("fi_cda", table, periods=[target], equals={"cnpj": cnpj})
        except NoData:
            continue
        part["block"] = table
        frames.append(part)

    if not frames:
        raise NoData("no CDA rows")
    frame = pd.concat(frames, ignore_index=True)
    if len(frame) > max_rows:
        return error(
            f"portfolio would return {len(frame)} rows, above {max_rows}",
            hint="Use a single block or raise max_rows.",
        )
    columns = [
        "block",
        "tp_aplic",
        "tp_ativo",
        "ds_ativo",
        "emissor",
        "qt_pos_final",
        "vl_merc_pos_final",
        "vl_custo_pos_final",
        "dt_venc",
    ]
    header = f"# source: CVM/FI CDA | cnpj={cnpj} | {target} | values in BRL\n"
    return header + to_csv(frame, columns=columns)


async def _fii_portfolio(cnpj: str, period: str | None, max_rows: int) -> str:
    target = _month_key(period) if period else await _latest_month_for("fii")
    year = target[:4]
    await store.ensure("fii_inf_mensal", "ativo_passivo", year)
    frame = store.read("fii_inf_mensal", "ativo_passivo", periods=[year], equals={"cnpj": cnpj})
    frame = frame.loc[
        frame["data_referencia"].astype("string").str.slice(0, 7) == _month_iso(target)
    ]
    if frame.empty:
        raise NoData("no FII balance sheet in that month")

    long = _melt(frame, ["cnpj", "cnpj_fundo_classe", "data_referencia", "versao"])
    if len(long) > max_rows:
        return error(f"portfolio would return {len(long)} rows, above {max_rows}")
    header = f"# source: CVM/FII Informe Mensal | cnpj={cnpj} | {target} | values in BRL\n"
    return header + to_csv(long, columns=["component", "value"])


async def _fidc_portfolio(cnpj: str, period: str | None, max_rows: int) -> str:
    target = _month_key(period) if period else await _latest_month_for("fidc")
    await store.ensure("fidc_inf_mensal", "tab_ii", target)
    frame = store.read("fidc_inf_mensal", "tab_ii", periods=[target], equals={"cnpj": cnpj})
    if frame.empty:
        raise NoData("no FIDC credit portfolio in that month")

    long = _melt(frame, ["cnpj", "cnpj_fundo_classe", "denom_social", "dt_comptc"])
    long = long.loc[long["component"] != "tab_ii_vl_carteira"]
    # Keep only the top-level segments (A..K). Sub-codes such as C1/F2 are
    # components of the parent, so including both would double count.
    long = long.loc[long["component"].str.match(r"^tab_ii_[a-k]_vl_", na=False)]
    if len(long) > max_rows:
        return error(f"portfolio would return {len(long)} rows, above {max_rows}")
    header = f"# source: CVM/FIDC Informe Mensal | cnpj={cnpj} | {target} | values in BRL\n"
    return header + to_csv(long, columns=["component", "value"])


# ---------------------------------------------------------------------------
# FIDC credit quality


@mcp.tool(annotations=READ_ONLY)
async def get_fidc_report(
    fund: Annotated[str, Field(description="FIDC name (partial) or class CNPJ.")],
    first_period: Annotated[str | None, Field(description="First month (YYYY-MM).")] = None,
    last_period: Annotated[str | None, Field(description="Last month. Default: latest.")] = None,
) -> str:
    """Credit-fund dashboard: assets, portfolio, net asset value and delinquency.

    `inadimplencia_pct` is the overdue credit over the total credit portfolio
    (tables I.2A.2 and I.2 of the FIDC monthly report).
    """
    cnpj, problem, record = await _resolve_fund(fund)
    if cnpj is None:
        return error(problem or "fund not found")
    if record and record.get("family") != "fidc":
        return error(
            f"{record.get('family')} is not a FIDC",
            hint="Use get_fund_nav / get_fund_portfolio for other fund families.",
        )

    try:
        last = _month_key(last_period) if last_period else await _latest_month_for("fidc")
        first = _month_key(first_period) if first_period else _shift_month(last, -5)
        months = _month_range(first, last)
        await store.ensure_many("fidc_inf_mensal", months, ["tab_i", "tab_ii", "tab_iv"])
    except ValueError as exc:
        return error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    columns_i = [
        "cnpj",
        "dt_comptc",
        "denom_social",
        "tab_i_vl_ativo",
        "tab_i2_vl_carteira",
        "tab_i2a_vl_dircred_risco",
        "tab_i2a1_vl_cred_venc_ad",
        "tab_i2a2_vl_cred_venc_inad",
        "tab_i2a3_vl_cred_inad",
    ]
    columns_ii = ["cnpj", "dt_comptc", "tab_ii_vl_carteira"]
    columns_iv = ["cnpj", "dt_comptc", "tab_iv_a_vl_pl", "tab_iv_b_vl_pl_medio"]

    try:
        table_i = _require(
            store.read("fidc_inf_mensal", "tab_i", periods=months, equals={"cnpj": cnpj}), columns_i
        )
        table_iv = _require(
            store.read("fidc_inf_mensal", "tab_iv", periods=months, equals={"cnpj": cnpj}), columns_iv
        )
    except NoData as exc:
        return error(str(exc))
    table_i = table_i.loc[:, columns_i]
    table_iv = table_iv.loc[:, columns_iv]

    frame = table_i.merge(table_iv, on=["cnpj", "dt_comptc"], how="left")
    try:
        table_ii = _require(
            store.read("fidc_inf_mensal", "tab_ii", periods=months, equals={"cnpj": cnpj}), columns_ii
        )
        frame = frame.merge(table_ii.loc[:, columns_ii], on=["cnpj", "dt_comptc"], how="left")
    except NoData:
        frame["tab_ii_vl_carteira"] = pd.NA

    portfolio = pd.to_numeric(frame["tab_i2_vl_carteira"], errors="coerce")
    overdue = pd.to_numeric(frame["tab_i2a2_vl_cred_venc_inad"], errors="coerce")
    frame["inadimplencia_pct"] = (overdue / portfolio * 100.0).where(portfolio > 0)

    frame = frame.sort_values("dt_comptc")
    columns = [
        "dt_comptc",
        "denom_social",
        "tab_iv_a_vl_pl",
        "tab_i_vl_ativo",
        "tab_i2_vl_carteira",
        "tab_ii_vl_carteira",
        "tab_i2a2_vl_cred_venc_inad",
        "tab_i2a3_vl_cred_inad",
        "inadimplencia_pct",
    ]
    header = f"# source: CVM/FIDC | cnpj={cnpj} | {first}-{last} | values in BRL\n"
    return header + to_csv(frame, columns=columns)


# ---------------------------------------------------------------------------
# ranking


@mcp.tool(annotations=READ_ONLY)
async def rank_funds(
    period: Annotated[str | None, Field(description="Month (YYYY-MM). Default: latest.")] = None,
    metric: Annotated[
        Literal["net_asset_value", "net_flow", "return_pct", "unitholders"],
        Field(description="Ranking metric."),
    ] = "net_asset_value",
    fund_type: Annotated[str | None, Field(description="Optional fund type filter, e.g. 'CLASSES - FIF'.")] = None,
    limit: Annotated[int, Field(description="Rows returned.", ge=1, le=500)] = 25,
) -> str:
    """Rank FI funds in one month by net asset value, net flow, return or unitholders."""
    try:
        target = _month_key(period) if period else await _latest_month()
        await store.ensure("fi_inf_diario", "informe", target)
        frame = store.read("fi_inf_diario", "informe", periods=[target])
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    if fund_type:
        wanted = fold(fund_type)
        frame = frame.loc[frame["tp_fundo_classe"].fillna("").map(fold).str.contains(wanted, regex=False)]
    if frame.empty:
        return error(f"no funds for type {fund_type!r} in {target}")

    frame = frame.sort_values(["cnpj", "dt_comptc"])
    grouped = (
        frame.groupby(["cnpj", "tp_fundo_classe"], dropna=False)
        .agg(
            cnpj_fundo_classe=("cnpj_fundo_classe", "first"),
            net_asset_value=("vl_patrim_liq", "last"),
            net_flow=("captc_dia", "sum"),
            redemptions=("resg_dia", "sum"),
            unitholders=("nr_cotst", "last"),
            quota_first=("vl_quota", "first"),
            quota_last=("vl_quota", "last"),
        )
        .reset_index()
    )
    grouped["net_flow"] = grouped["net_flow"] - grouped["redemptions"]
    grouped["return_pct"] = (grouped["quota_last"] / grouped["quota_first"] - 1.0) * 100.0

    try:
        await store.ensure("fi_cad", "classe")
        classes = store.read("fi_cad", "classe")
        names = classes.loc[:, ["cnpj", "denominacao_social"]].dropna(subset=["cnpj"]).drop_duplicates("cnpj")
        grouped = grouped.merge(names, on="cnpj", how="left")
    except Exception:  # noqa: BLE001 - names are cosmetic
        grouped["denominacao_social"] = pd.NA

    grouped = grouped.sort_values(metric, ascending=False).head(int(limit))
    columns = [
        "cnpj_fundo_classe",
        "denominacao_social",
        "tp_fundo_classe",
        "net_asset_value",
        "net_flow",
        "return_pct",
        "unitholders",
        "quota_last",
    ]
    header = f"# source: CVM/FI | month={target} | ranked by {metric}\n"
    return header + to_csv(grouped, columns=columns)


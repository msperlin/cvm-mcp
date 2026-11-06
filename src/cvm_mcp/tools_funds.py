"""Fund tools: daily NAV, flows and portfolio (FI / CDA), and the fund registry."""

from __future__ import annotations

import re
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
# periods


def _month_key(value: str | None) -> str:
    """Accept ``2025-10`` or ``202510`` and return ``202510``."""
    if value is None:
        return ""
    text = str(value).strip().replace("-", "").replace("/", "")
    if not re.fullmatch(r"\d{6}", text):
        raise ValueError(f"period must be YYYY-MM or YYYYMM, got {value!r}")
    return text


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


async def _latest_month() -> str:
    available = await store.client.periods(registry.get("fi_inf_diario"))
    return str(max(available))


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
        "manager": row.get("gestor") if "gestor" in frame.columns else None,
        "classification": row.get("classificacao_anbima"),
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


# ---------------------------------------------------------------------------
# tools


@mcp.tool(annotations=READ_ONLY)
async def search_fund(
    query: Annotated[
        str,
        Field(description="Fund/class name (partial), CNPJ or CVM fund code. E.g. 'itaú', '00.017.024/0001-53'."),
    ],
    limit: Annotated[int, Field(description="Maximum matches.", ge=1, le=50)] = 10,
) -> str:
    """Find a fund or class and get the CNPJ used by every fund tool.

    The CVM keys fund data on the *class* CNPJ; this tool is how you get it.
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

    hits = hits.head(int(limit))
    results = [
        {
            "cnpj": _clean(row.cnpj) or _clean(getattr(row, "cnpj_classe", None)),
            "fund": _clean(row.denominacao_social),
            "cvm_code": _clean(row.codigo_cvm),
            "type": _clean(row.tipo_classe),
            "status": _clean(row.situacao),
            "classification": _clean(row.classificacao_anbima),
        }
        for row in hits.itertuples(index=False)
    ]
    return json_out({"query": text, "count": len(results), "results": results})


@mcp.tool(annotations=READ_ONLY)
async def get_fund_nav(
    fund: Annotated[str, Field(description="Fund name (partial) or class CNPJ.")],
    first_period: Annotated[str | None, Field(description="First month (YYYY-MM or YYYYMM).")] = None,
    last_period: Annotated[str | None, Field(description="Last month. Default: latest published.")] = None,
    max_rows: Annotated[int, Field(description="Maximum rows.", ge=1, le=20_000)] = 5000,
) -> str:
    """Daily quota, net asset value, subscriptions, redemptions and unitholders.

    This is the fund time series: `vl_quota` is the quota price, `vl_patrim_liq`
    the net asset value and `daily_return_pct` the quota return in percent.
    """
    cnpj, problem, _ = await _resolve_fund(fund)
    if cnpj is None:
        return error(problem or "fund not found")

    try:
        last = _month_key(last_period) if last_period else await _latest_month()
        first = _month_key(first_period) if first_period else _shift_month(last, -11)
        periods = _month_range(first, last)
        await store.ensure_many("fi_inf_diario", periods, ["informe"])
        frame = store.read("fi_inf_diario", "informe", periods=periods, equals={"cnpj": cnpj})
    except NoData:
        return error(
            f"no daily report for cnpj={cnpj} in {first}-{last}",
            hint=(
                "The Informe Diário covers FI (ICVM 555). FIIs, FIDCs and CRI/CRA "
                "vehicles report in separate CVM datasets."
            ),
        )
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))
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


@mcp.tool(annotations=READ_ONLY)
async def get_fund_flows(
    fund: Annotated[str, Field(description="Fund name (partial) or class CNPJ.")],
    first_period: Annotated[str | None, Field(description="First month.")] = None,
    last_period: Annotated[str | None, Field(description="Last month. Default: latest.")] = None,
) -> str:
    """Monthly subscriptions, redemptions and net flow for one fund."""
    cnpj, problem, _ = await _resolve_fund(fund)
    if cnpj is None:
        return error(problem or "fund not found")

    try:
        last = _month_key(last_period) if last_period else await _latest_month()
        first = _month_key(first_period) if first_period else _shift_month(last, -11)
        periods = _month_range(first, last)
        await store.ensure_many("fi_inf_diario", periods, ["informe"])
        frame = store.read("fi_inf_diario", "informe", periods=periods, equals={"cnpj": cnpj})
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    frame = frame.copy()
    frame["month"] = frame["_period"].astype(str)
    grouped = (
        frame.groupby("month")
        .agg(
            subscriptions=("captc_dia", "sum"),
            redemptions=("resg_dia", "sum"),
            net_flow=("captc_dia", lambda values: values.sum()),
            net_asset_value=("vl_patrim_liq", "last"),
            unitholders=("nr_cotst", "last"),
        )
        .reset_index()
    )
    grouped["net_flow"] = grouped["subscriptions"] - grouped["redemptions"]
    header = f"# source: CVM/FI | cnpj={cnpj} | values in BRL\n"
    return header + to_csv(grouped)


@mcp.tool(annotations=READ_ONLY)
async def get_fund_portfolio(
    fund: Annotated[str, Field(description="Fund name (partial) or class CNPJ.")],
    period: Annotated[str | None, Field(description="Month (YYYY-MM). Default: latest CDA available.")] = None,
    block: Annotated[
        str, Field(description="'1'..'8' for one block, 'all' for every block.")
    ] = "all",
    max_rows: Annotated[int, Field(description="Maximum rows.", ge=1, le=50_000)] = 5000,
) -> str:
    """The fund portfolio (CDA): assets, issuers, quantity, market value and cost.

    Blocks are arbitrary groups created by CVM; 'all' merges them and tags each
    row with `block`.
    """
    cnpj, problem, _ = await _resolve_fund(fund)
    if cnpj is None:
        return error(problem or "fund not found")

    try:
        if period:
            target = _month_key(period)
        else:
            available = await store.client.periods(registry.get("fi_cda"))
            target = str(max(available))
        blocks = list(range(1, 9)) if str(block).lower() == "all" else [int(block)]
        tables = [f"blc_{index}" for index in blocks]
        await store.ensure_many("fi_cda", [target], tables)
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    frames: list[pd.DataFrame] = []
    problems: list[str] = []
    for index in blocks:
        try:
            part = store.read("fi_cda", f"blc_{index}", periods=[target], equals={"cnpj": cnpj})
        except NoData:
            continue
        except Exception as exc:  # noqa: BLE001
            problems.append(f"block {index}: {describe_error(exc)}")
            continue
        part["block"] = index
        frames.append(part)

    if not frames:
        return error(
            f"no portfolio rows for cnpj={cnpj} in {target}",
            hint="The fund may not report a CDA, or the month is not published yet.",
            problems=problems,
        )

    frame = pd.concat(frames, ignore_index=True)
    if len(frame) > max_rows:
        return error(f"portfolio would return {len(frame)} rows, above {max_rows}", hint="Use a single block or raise max_rows.")
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
    """Rank funds in one month by net asset value, net flow, return or unitholders."""
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

"""Company tools: statements (DFP/ITR), corporate filings (FRE), insider
trading (VLMO) and governance (ICBGC)."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import Field

from . import catalog_dfp, registry
from .app import mcp
from .catalog_fre import get_table as get_fre_table_spec
from .fetch import describe_error
from .formatting import to_csv, with_warnings
from .identity import (
    clean_json_value,
    company_record,
    find_by_cnpj,
    registry_mask,
    search_matches,
)
from .normalize import fold
from .runtime import store
from .store import NoData
from .toolkit import (
    DEFAULT_TABLE_ROWS,
    MAX_TABLE_ROWS,
    MIN_YEAR,
    READ_ONLY,
    apply_version,
    current_year,
    error,
    json_out,
    parse_codes,
    parse_data_type,
    parse_docs,
    parse_formats,
    parse_version,
    parse_years,
    render,
)

STATEMENT_COLUMNS = (
    "doc",
    "fmt",
    "CD_CVM",
    "DENOM_CIA",
    "FISCAL_YEAR",
    "DT_REFER",
    "PERIOD",
    "CD_CONTA",
    "DS_CONTA",
    "VL_CONTA",
    "VL_CONTA_BRL",
    "ESCALA_MOEDA",
    "MOEDA",
    "GRUPO_DFP",
    "QUARTER_LABEL",
)


# ---------------------------------------------------------------------------
# helpers


async def _get_registry() -> pd.DataFrame:
    return await store.registry()


async def _company_names(codes: list[int]) -> dict[int, str]:
    try:
        registry_frame = await store.registry()
    except Exception:  # noqa: BLE001 - names are cosmetic
        return {}
    subset = registry_frame.loc[registry_frame["CD_CVM"].isin(codes)]
    return {
        int(code): str(name)
        for code, name in zip(subset["CD_CVM"], subset["DENOM_SOCIAL"])
    }


async def _statement_frame(
    kind: str,
    codes: list[int],
    first_year: int,
    last_year: int,
    docs: list[str],
    formats: list[str],
) -> tuple[pd.DataFrame, list[str]]:
    """Read DFP/ITR partitions, tagging each row with its document and format."""
    years = [str(year) for year in range(first_year, last_year + 1)]
    tables = [f"{doc.lower()}_{fmt}" for doc in docs for fmt in formats]
    await store.ensure_many(kind, years, tables)
    warnings = [
        f"{dataset}/{table}/{period}: {message}"
        for (dataset, table, period), message in store.last_errors.items()
        if dataset == kind
    ]

    frames: list[pd.DataFrame] = []
    for doc in docs:
        for fmt in formats:
            try:
                frame = store.read(
                    kind,
                    f"{doc.lower()}_{fmt}",
                    periods=years,
                    isin={"CD_CVM": codes},
                )
            except NoData:
                continue
            frame["doc"] = doc
            frame["fmt"] = fmt
            frames.append(frame)

    if not frames:
        raise NoData(f"no {kind.upper()} rows for cd_cvm={codes} in {first_year}-{last_year}")
    return pd.concat(frames, ignore_index=True), warnings


async def _collect_fre(
    slug: str,
    codes: list[int] | None,
    first_year: int,
    last_year: int,
    version: str,
) -> tuple[pd.DataFrame, list[str], list[int]]:
    frames: list[pd.DataFrame] = []
    warnings: list[str] = []
    years_found: list[int] = []

    for year in range(first_year, last_year + 1):
        try:
            await store.ensure("fre", slug, str(year))
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{year}: {describe_error(exc)}")
            continue

        try:
            frame = store.read(
                "fre",
                slug,
                periods=[str(year)],
                isin={"cd_cvm": codes} if codes else None,
            )
        except NoData:
            continue
        frame = apply_version(frame, version)
        if frame.empty:
            continue
        frame.insert(0, "ano", year)
        frames.append(frame)
        years_found.append(year)

    if not frames:
        return pd.DataFrame(), warnings, years_found
    return pd.concat(frames, ignore_index=True), warnings, years_found


async def _resolve_company(value: str | int) -> tuple[int | None, str | None]:
    """Accept a cd_cvm, a CNPJ or a name and return ``(cd_cvm, error)``."""
    text = str(value).strip()
    try:
        frame = await store.registry()
    except Exception as exc:  # noqa: BLE001
        return None, describe_error(exc)

    if text.isdigit() and len(text) <= 6:
        matches = frame.loc[frame["CD_CVM"] == int(text)]
        if matches.empty:
            return None, f"cd_cvm {text} not found"
        return int(matches.iloc[0].CD_CVM), None

    if len(re.sub(r"\D", "", text)) == 14:
        matches = find_by_cnpj(frame, text)
        if matches.empty:
            return None, f"CNPJ {text} not found in the CVM registry"
        return int(matches.iloc[0].CD_CVM), None

    matches = search_matches(frame, text)
    if matches.empty:
        return None, f"no company matches {text!r}"
    return int(matches.iloc[0].CD_CVM), None


# ---------------------------------------------------------------------------
# identity


@mcp.tool(annotations=READ_ONLY)
async def search_company(
    query: Annotated[
        str,
        Field(description="Company name, partial name, CNPJ, ticker or CVM code. E.g. 'petrobras', 'PETR4'."),
    ],
    limit: Annotated[int, Field(description="Maximum companies to return.", ge=1, le=50)] = 10,
    status: Annotated[
        str | None, Field(description="Registry situation filter, e.g. 'ATIVO' or 'CANCELADO'.")
    ] = None,
) -> str:
    """Find the CVM code (`cd_cvm`) of a company by name, CNPJ, ticker or code.

    Accent- and case-insensitive. This is the entry point for every corporate
    tool: they key on `cd_cvm`, never on the ticker.
    """
    text = str(query).strip()
    if not text:
        return error("query is empty", hint="Try 'petrobras', 'vale' or a CVM code.")

    try:
        frame = await store.registry()
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc), hint="The CVM registry could not be downloaded.")

    frame = frame.loc[registry_mask(frame, status)]
    if frame.empty:
        return error("no companies match the status filter", hint="Try status='ATIVO'.")

    if len(re.sub(r"\D", "", text)) == 14:
        matches = find_by_cnpj(frame, text)
    elif re.fullmatch(r"[A-Za-z]{3,6}\d{1,2}", text):
        return await _search_by_ticker(text)
    else:
        matches = search_matches(frame, text)

    if matches.empty:
        return json_out(
            {
                "query": text,
                "count": 0,
                "hint": "No match. Try a shorter fragment ('brades', 'energ') or list_companies.",
            }
        )

    matches = matches.head(int(limit))
    results = [company_record(row) for row in matches.itertuples(index=False)]
    payload: dict[str, Any] = {"query": text, "count": len(results), "results": results}
    if len(results) > 1:
        payload["note"] = (
            "Several companies match. Large groups file through many entities; "
            "pick by sector and status, or ask the user which one they mean."
        )
    return json_out(payload)


async def _search_by_ticker(ticker: str) -> str:
    wanted = str(ticker).strip().upper()
    try:
        available = await store.client.periods(registry.get("fca"))
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))
    for year in sorted(available, reverse=True)[:3]:
        try:
            frame = await store.tickers(int(year))
        except Exception:  # noqa: BLE001, S112 - try the next year
            continue
        hits = frame.loc[frame["CODIGO_NEGOCIACAO"].fillna("").str.upper() == wanted]
        if not hits.empty:
            row = hits.iloc[0]
            return json_out(
                {
                    "query": ticker,
                    "count": len(hits),
                    "cd_cvm": clean_json_value(row.CD_CVM),
                    "company": clean_json_value(row.DENOM_SOCIAL),
                    "cnpj": clean_json_value(row.CNPJ_CIA),
                    "ticker": wanted,
                    "year": int(year),
                }
            )
    return json_out({"query": ticker, "count": 0, "hint": "Ticker not found in the last FCA years."})


@mcp.tool(annotations=READ_ONLY)
async def resolve(
    query: Annotated[
        str, Field(description="A ticker (PETR4), a CNPJ, a cd_cvm or a company name.")
    ],
) -> str:
    """Resolve any identifier to the CVM keys (cd_cvm, CNPJ, ticker, name)."""
    text = str(query).strip()
    if not text:
        return error("query is empty")
    if re.fullmatch(r"[A-Za-z]{3,6}\d{1,2}", text):
        return await _search_by_ticker(text)
    code, problem = await _resolve_company(text)
    if code is None:
        return error(problem or "not found")
    frame = await store.registry()
    row = frame.loc[frame["CD_CVM"] == code].iloc[0]
    return json_out(company_record(row, extended=True))


@mcp.tool(annotations=READ_ONLY)
async def list_companies(
    sector: Annotated[str | None, Field(description="Partial sector name, e.g. 'Energia'.")] = None,
    status: Annotated[str | None, Field(description="Registry situation, e.g. 'ATIVO'.")] = "ATIVO",
    limit: Annotated[int, Field(description="Page size.", ge=1, le=200)] = 50,
    offset: Annotated[int, Field(description="Rows to skip (paging).", ge=0)] = 0,
) -> str:
    """Browse the CVM company registry, optionally filtered by sector."""
    try:
        frame = await store.registry()
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    frame = frame.loc[registry_mask(frame, status)]
    if sector:
        wanted = fold(sector)
        sectors = frame["SETOR_ATIV"].fillna("").map(fold)
        frame = frame.loc[sectors.str.contains(wanted, regex=False)]

    total = len(frame)
    page = frame.sort_values("DENOM_SOCIAL").iloc[offset : offset + limit]
    results = [
        {
            "cd_cvm": int(row.CD_CVM),
            "company": clean_json_value(row.DENOM_SOCIAL),
            "sector": clean_json_value(row.SETOR_ATIV),
            "status": clean_json_value(row.SIT),
        }
        for row in page.itertuples(index=False)
    ]
    return json_out(
        {
            "total_matches": total,
            "offset": offset,
            "count": len(results),
            "results": results,
            "has_more": offset + len(results) < total,
        }
    )


@mcp.tool(annotations=READ_ONLY)
async def get_tickers(
    year: Annotated[
        int | None, Field(description="FCA reference year (2018+). Default: latest available.")
    ] = None,
    ticker: Annotated[str | None, Field(description="Filter by trading code, e.g. 'PETR4'.")] = None,
    cvm_codes: Annotated[list[int] | None, Field(description="Optional CVM codes.")] = None,
    limit: Annotated[int, Field(description="Maximum rows.", ge=1, le=5000)] = 500,
) -> str:
    """Map tickers (PETR4, VALE3) to CVM codes and company names."""
    try:
        available = await store.client.periods(registry.get("fca"))
        target = max(available) if year is None else str(int(year))
        if target not in available:
            return error(
                f"no FCA data for {target}",
                hint=f"Available years: {min(available)}-{max(available)}",
            )
        frame = await store.tickers(int(target))
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    if ticker:
        wanted = str(ticker).strip().upper()
        frame = frame.loc[frame["CODIGO_NEGOCIACAO"].fillna("").str.upper() == wanted]
    if cvm_codes:
        frame = frame.loc[frame["CD_CVM"].isin(parse_codes(cvm_codes) or [])]

    if frame.empty:
        return json_out({"year": target, "count": 0, "hint": "No securities match the filter."})
    if len(frame) > limit:
        return error(
            f"ticker list would return {len(frame)} rows, above {limit}",
            hint="Filter by ticker or cvm_codes, or raise the limit.",
        )

    frame = frame.sort_values(["DENOM_SOCIAL", "CODIGO_NEGOCIACAO"])
    body = to_csv(
        frame,
        columns=(
            "CD_CVM",
            "CODIGO_NEGOCIACAO",
            "DENOM_SOCIAL",
            "VALOR_MOBILIARIO",
            "MERCADO",
            "SEGMENTO",
            "DT_INI_NEGOCIACAO",
            "DT_FIM_NEGOCIACAO",
        ),
    )
    return f"# source: CVM/FCA {target}\n{body}"


# ---------------------------------------------------------------------------
# DFP / ITR


@mcp.tool(annotations=READ_ONLY)
async def list_account_codes(
    doc_type: Annotated[
        str | None, Field(description="Optional filter: BPA, BPP, DRE, DFC_MI, DVA.")
    ] = None,
) -> str:
    """List the curated account codes and role names understood by this server."""
    wanted = parse_docs(doc_type)[0] if doc_type else None
    accounts = catalog_dfp.accounts_for(wanted)
    if not accounts:
        return error(f"no catalog entries for {doc_type!r}")
    return json_out(
        {
            "doc_type": wanted,
            "count": len(accounts),
            "accounts": [account.as_dict() for account in accounts],
            "note": (
                "Codes follow CVM's standard chart. Banks and insurers use different "
                "labels for the same code; DS_CONTA in the data is authoritative."
            ),
        }
    )


@mcp.tool(annotations=READ_ONLY)
async def get_financial_statements(
    cvm_codes: Annotated[
        list[int], Field(description="CVM codes (see search_company).", min_length=1, max_length=20)
    ],
    first_year: Annotated[int | None, Field(description="First fiscal year.")] = None,
    last_year: Annotated[int | None, Field(description="Last fiscal year.")] = None,
    data_type: Annotated[
        Literal["dfp", "itr"], Field(description="'dfp' = annual, 'itr' = quarterly.")
    ] = "dfp",
    doc_type: Annotated[
        str, Field(description="One or more of BPA, BPP, DRE, DFC_MD, DFC_MI, DMPL, DVA, or '*'.")
    ] = "DRE",
    format: Annotated[str, Field(description="'con' = consolidated, 'ind' = individual, or '*'.")] = "con",
    max_rows: Annotated[
        int, Field(description="Safety cap on returned rows.", ge=100, le=50_000)
    ] = 4000,
) -> str:
    """Full statement lines for one or more companies, as CSV.

    One row per reported account, with the raw value (`VL_CONTA`) and the value
    in reais (`VL_CONTA_BRL`).
    """
    try:
        kind = parse_data_type(data_type)
        docs = parse_docs(doc_type)
        formats = parse_formats(format)
        first, last = parse_years(first_year, last_year)
        codes = parse_codes(cvm_codes) or []
    except ValueError as exc:
        return error(str(exc))
    if not codes:
        return error("cvm_codes is required", hint="Use search_company to find the code.")

    try:
        frame, warnings = await _statement_frame(kind, codes, first, last, docs, formats)
    except NoData as exc:
        return error(str(exc))

    if len(frame) > max_rows:
        return error(
            f"query would return {len(frame)} rows, above {max_rows}",
            hint="Narrow the years, fewer companies, a single doc_type, or raise max_rows.",
            rows=len(frame),
            limit=max_rows,
        )

    sort_keys = [key for key in ("CD_CVM", "FISCAL_YEAR", "doc", "CD_CONTA") if key in frame]
    frame = frame.sort_values(sort_keys)
    return with_warnings(to_csv(frame, columns=STATEMENT_COLUMNS), warnings)


@mcp.tool(annotations=READ_ONLY)
async def get_account_history(
    cvm_codes: Annotated[
        list[int], Field(description="CVM codes of the companies.", min_length=1, max_length=20)
    ],
    account: Annotated[
        str,
        Field(
            description=(
                "Account to follow: a CD_CONTA code ('3.01'), a role ('revenue', "
                "'net_income', 'total_assets', 'equity', 'ebit') or, with by='name', "
                "a label fragment."
            )
        ),
    ],
    by: Annotated[Literal["code", "role", "name"], Field(description="How to read `account`.")] = "role",
    doc_type: Annotated[str, Field(description="BPA, BPP, DRE, DFC_MI, DFC_MD, DVA or DMPL.")] = "DRE",
    format: Annotated[str, Field(description="'con' or 'ind'.")] = "con",
    first_year: Annotated[int | None, Field(description="First fiscal year.")] = None,
    last_year: Annotated[int | None, Field(description="Last fiscal year.")] = None,
    data_type: Annotated[
        Literal["dfp", "itr"], Field(description="'dfp' = annual, 'itr' = quarterly.")
    ] = "dfp",
    max_rows: Annotated[int, Field(description="Safety cap on returned rows.", ge=50, le=50_000)] = 5000,
) -> str:
    """Follow one account over time for one or more companies (a compact series)."""
    try:
        kind = parse_data_type(data_type)
        docs = parse_docs(doc_type)
        formats = parse_formats(format)
        first, last = parse_years(first_year, last_year, span=5)
        codes = parse_codes(cvm_codes) or []
    except ValueError as exc:
        return error(str(exc))
    if len(docs) != 1 or len(formats) != 1:
        return error("get_account_history needs exactly one doc_type and one format")

    doc, fmt = docs[0], formats[0]
    target = str(account).strip()
    if not target:
        return error("account is empty")

    name_fragment: str | None = None
    role_filter: str | None = None
    if by == "code":
        code = target
    elif by == "role":
        try:
            catalog_dfp.resolve_role(doc, target)
        except KeyError as exc:
            return error(str(exc), hint="Call list_account_codes to see available roles.")
        code = None
        role_filter = target.strip().lower()
    else:
        code = None
        name_fragment = fold(target)

    try:
        frame, warnings = await _statement_frame(kind, codes, first, last, [doc], [fmt])
    except NoData as exc:
        return error(str(exc))

    if code is not None:
        frame = frame.loc[frame["CD_CONTA"].astype(str) == code]
    elif role_filter is not None:
        frame = catalog_dfp.annotate_roles(frame)
        frame = frame.loc[frame["ROLE"] == role_filter]
    else:
        labels = frame["DS_CONTA"].fillna("").map(fold)
        frame = frame.loc[labels.str.contains(name_fragment or "", regex=False)]

    if frame.empty:
        return error(
            f"no rows for account {target!r} in {doc}/{fmt}/{first}-{last}",
            hint="Call list_account_codes, or check that these companies file this document.",
            errors=warnings,
        )
    if len(frame) > max_rows:
        return error(
            f"query would return {len(frame)} rows, above {max_rows}",
            hint="Narrow the years or raise max_rows.",
        )

    names = await _company_names(codes)
    frame = catalog_dfp.annotate_roles(frame)
    frame["COMPANY"] = frame["CD_CVM"].map(names)
    frame = frame.sort_values(["CD_CVM", "DT_REFER", "CD_CONTA"])
    body = to_csv(
        frame.rename(
            columns={
                "CD_CVM": "cvm_code",
                "COMPANY": "company",
                "FISCAL_YEAR": "fiscal_year",
                "DT_REFER": "date",
                "PERIOD": "period",
                "CD_CONTA": "account_code",
                "DS_CONTA": "account_label",
                "ROLE": "role",
                "VL_CONTA_BRL": "value_brl",
                "QUARTER_LABEL": "quarter",
            }
        ),
        columns=(
            "cvm_code",
            "company",
            "fiscal_year",
            "date",
            "period",
            "account_code",
            "account_label",
            "role",
            "value_brl",
            "quarter",
        ),
    )
    return with_warnings(body, warnings)


@mcp.tool(annotations=READ_ONLY)
async def get_statement_summary(
    cvm_code: Annotated[int, Field(description="CVM code of the company.", ge=1)],
    year: Annotated[int | None, Field(description="Fiscal year. Default: current year.")] = None,
    format: Annotated[
        Literal["con", "ind"], Field(description="'con' = consolidated, 'ind' = individual.")
    ] = "con",
    doc_type: Annotated[
        str, Field(description="Documents to summarize. Default 'BPA,BPP,DRE'.")
    ] = "BPA,BPP,DRE",
    depth: Annotated[
        int, Field(description="Account nesting: 0 = totals only, 1 = headline (default).", ge=0, le=2)
    ] = 1,
    data_type: Annotated[
        Literal["dfp", "itr"], Field(description="'dfp' = annual, 'itr' = quarterly.")
    ] = "dfp",
) -> str:
    """Compact headline view of one company-year: totals, equity, revenue, profit.

    Returns the top-level accounts with a `role` column (`total_assets`,
    `equity`, `revenue`, `ebit`, `net_income`, ...).
    """
    try:
        kind = parse_data_type(data_type)
        docs = parse_docs(doc_type)
        formats = parse_formats(format)
        target_year = int(year) if year is not None else current_year()
        parse_years(target_year, target_year)
    except ValueError as exc:
        return error(str(exc))

    try:
        frame, warnings = await _statement_frame(
            kind, [int(cvm_code)], target_year, target_year, docs, formats
        )
    except NoData as exc:
        return error(
            str(exc),
            hint=f"No {kind.upper()} statement for company {cvm_code} in {target_year}.",
        )

    frame = frame.loc[frame["ACCOUNT_DEPTH"] <= depth].copy()
    if frame.empty:
        return error("no headline accounts found", hint="Try depth=2 or get_financial_statements.")

    frame = catalog_dfp.annotate_roles(frame)
    frame["value_brl"] = frame["VL_CONTA_BRL"]
    frame = frame.sort_values(["doc", "CD_CONTA"])
    body = to_csv(
        frame,
        columns=("doc", "CD_CONTA", "DS_CONTA", "ROLE", "value_brl", "FISCAL_YEAR", "DT_REFER", "DENOM_CIA"),
    )
    header = (
        f"# cd_cvm={cvm_code} | {format} | {kind.upper()} | values in BRL "
        f"(scaled from ESCALA_MOEDA)\n"
    )
    return with_warnings(header + body, warnings)


# ---------------------------------------------------------------------------
# FRE


@mcp.tool(annotations=READ_ONLY)
def list_fre_tables(
    detail: Annotated[bool, Field(description="Include curated key columns.")] = False,
) -> str:
    """List the 36 FRE corporate tables understood by this server."""
    from .catalog_fre import all_tables, count

    return json_out(
        {
            "count": count(),
            "tables": [table.as_dict(detail=detail) for table in all_tables()],
            "note": "FRE is annual corporate data; for statements use get_financial_statements.",
        }
    )


@mcp.tool(annotations=READ_ONLY)
async def get_fre_table(
    table: Annotated[
        str, Field(description="Table slug from list_fre_tables, e.g. 'posicao_acionaria'.")
    ],
    cd_cvm: Annotated[int | None, Field(description="Optional CVM code.")] = None,
    first_year: Annotated[int | None, Field(description="First reference year.", ge=MIN_YEAR)] = None,
    last_year: Annotated[int | None, Field(description="Last reference year.")] = None,
    version: Annotated[str, Field(description="'last' (default), 'first' or 'all'.")] = "last",
    max_rows: Annotated[int, Field(description="Maximum rows returned.", ge=1, le=MAX_TABLE_ROWS)] = DEFAULT_TABLE_ROWS,
    columns: Annotated[list[str] | None, Field(description="Optional subset of columns.")] = None,
) -> str:
    """Read rows from one FRE table, filtered by company and year.

    The generic workhorse; prefer the curated helpers when they fit.
    """
    try:
        resolved = get_fre_table_spec(table)
        spec = registry.get("fre").table(resolved.slug)
        wanted = parse_version(version)
        codes = parse_codes(cd_cvm)
        if first_year is None and last_year is None:
            try:
                available = await store.client.periods(registry.get("fre"))
                first = last = int(max(available))
            except Exception:  # noqa: BLE001
                first, last = parse_years(None, None)
        else:
            first, last = parse_years(first_year, last_year)
    except ValueError as exc:
        return error(str(exc))
    except KeyError as exc:
        return error(exc.args[0] if exc.args else str(exc))

    frame, warnings, years = await _collect_fre(spec.slug, codes, first, last, wanted)
    if frame.empty:
        code = f" for cd_cvm={cd_cvm}" if cd_cvm else ""
        return error(
            f"no {spec.slug} rows{code} in {first}-{last}",
            hint="Use get_company_overview to see which tables have data.",
            warnings=warnings,
        )
    if not years:
        warnings.append("no rows found")
    return render(frame, columns=columns, max_rows=int(max_rows), warnings=warnings)


@mcp.tool(annotations=READ_ONLY)
async def get_company_overview(
    cd_cvm: Annotated[int, Field(description="CVM code from search_company.", ge=1)],
    year: Annotated[int, Field(description="Reference year.", ge=MIN_YEAR)],
) -> str:
    """Show which FRE tables have data for one company-year.

    Downloads and parses the whole year once (the first call can take a while).
    """
    try:
        code, year = int(cd_cvm), int(year)
    except (TypeError, ValueError):
        return error("cd_cvm and year must be integers")

    try:
        frame = await store.registry()
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))
    rows = frame.loc[frame["CD_CVM"] == code]
    if rows.empty:
        return error(f"CVM code {code} not found", hint="Use search_company first.")
    company = clean_json_value(rows.iloc[0].DENOM_SOCIAL)

    try:
        outcome = await store.ensure_period("fre", str(year))
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    records: list[dict[str, Any]] = []
    for item in registry.get("fre").tables:
        status = outcome.get(item.slug, "unknown")
        if status != "ok":
            records.append({"table": item.slug, "title": item.title, "rows": 0, "status": status})
            continue
        try:
            subset = store.read("fre", item.slug, periods=[str(year)], isin={"cd_cvm": [code]})
        except NoData:
            records.append({"table": item.slug, "title": item.title, "rows": 0, "status": "ok"})
            continue
        reference = None
        if "data_referencia" in subset.columns:
            values = subset["data_referencia"].dropna()
            reference = values.max().date().isoformat() if not values.empty else None
        records.append(
            {
                "table": item.slug,
                "title": item.title,
                "rows": len(subset),
                "data_referencia": reference,
                "status": "ok",
            }
        )

    with_data = sum(1 for record in records if record["rows"] > 0)
    return json_out(
        {
            "cd_cvm": code,
            "company": company,
            "year": year,
            "tables_with_data": with_data,
            "tables_total": len(records),
            "tables": records,
        }
    )


@mcp.tool(annotations=READ_ONLY)
async def get_shareholders(
    cd_cvm: Annotated[int, Field(description="CVM code from search_company.", ge=1)],
    year: Annotated[int, Field(description="Reference year.", ge=MIN_YEAR)],
    version: Annotated[str, Field(description="'last' (default), 'first' or 'all'.")] = "last",
    max_rows: Annotated[int, Field(description="Maximum rows.", ge=1, le=MAX_TABLE_ROWS)] = DEFAULT_TABLE_ROWS,
) -> str:
    """Shareholder position: who owns the company and how much."""
    try:
        wanted = parse_version(version)
        frame, warnings, _ = await _collect_fre("posicao_acionaria", [int(cd_cvm)], int(year), int(year), wanted)
    except ValueError as exc:
        return error(str(exc))

    if frame.empty:
        return error(
            f"no shareholder data for cd_cvm={cd_cvm} in {year}",
            hint="Check get_company_overview; some years have no FRE filing.",
            warnings=warnings,
        )
    if "percentual_total_acoes_circulacao" in frame.columns:
        frame = frame.sort_values("percentual_total_acoes_circulacao", ascending=False, na_position="last")
    columns = [
        "nome_companhia",
        "data_referencia",
        "acionista",
        "tipo_pessoa_acionista",
        "cpf_cnpj_acionista",
        "quantidade_total_acoes_circulacao",
        "percentual_total_acoes_circulacao",
        "acionista_controlador",
        "nacionalidade",
        "sigla_uf",
        "residente_exterior",
    ]
    warnings.append("class-level detail: get_fre_table('posicao_acionaria_classe_acao', ...)")
    return render(frame, columns=columns, max_rows=int(max_rows), warnings=warnings)


@mcp.tool(annotations=READ_ONLY)
async def get_compensation(
    cd_cvm: Annotated[int, Field(description="CVM code from search_company.", ge=1)],
    year: Annotated[int, Field(description="Reference year.", ge=MIN_YEAR)],
    detail: Annotated[
        Literal["orgao", "max_min_avg", "variavel"],
        Field(description="'orgao' = total by body, 'max_min_avg' = ranges, 'variavel' = bonus."),
    ] = "orgao",
    version: Annotated[str, Field(description="'last' (default), 'first' or 'all'.")] = "last",
) -> str:
    """Compensation of the board, officers and fiscal council."""
    mapping = {
        "orgao": (
            "remuneracao_total_orgao",
            (
                "orgao_administracao",
                "numero_membros",
                "numero_membros_remunerados",
                "total_remuneracao_orgao",
                "salario",
                "beneficios_diretos_indiretos",
                "participacoes_comites",
                "bonus",
                "participacao_resultados",
                "outros_valores_fixos",
                "outros_valores_variaveis",
                "comissoes",
                "pos_emprego",
                "cessacao_cargo",
                "baseada_acoes",
            ),
        ),
        "max_min_avg": (
            "remuneracao_maxima_minima_media",
            (
                "orgao_administracao",
                "numero_membros",
                "numero_membros_remunerados",
                "valor_maior_remuneracao",
                "valor_menor_remuneracao",
                "valor_medio_remuneracao",
                "observacao",
            ),
        ),
        "variavel": (
            "remuneracao_variavel",
            (
                "orgao_administracao",
                "quantidade_total_membros",
                "quantidade_membros_remunerados",
                "bonus_valor_minimo",
                "bonus_valor_maximo",
                "bonus_valor_metas_atingidas",
                "bonus_valor_efetivo",
                "participacao_valor_minimo",
                "participacao_valor_maximo",
                "participacao_valor_metas_atingidas",
                "participacao_valor_efetivo",
            ),
        ),
    }
    slug, columns = mapping[detail]
    try:
        wanted = parse_version(version)
        frame, warnings, _ = await _collect_fre(slug, [int(cd_cvm)], int(year), int(year), wanted)
    except ValueError as exc:
        return error(str(exc))
    if frame.empty:
        return error(
            f"no compensation data for cd_cvm={cd_cvm} in {year}",
            hint="Try detail='max_min_avg' or 'variavel'.",
            warnings=warnings,
        )
    return render(frame, columns=list(columns), max_rows=500, warnings=warnings)


@mcp.tool(annotations=READ_ONLY)
async def get_board(
    cd_cvm: Annotated[int, Field(description="CVM code from search_company.", ge=1)],
    year: Annotated[int, Field(description="Reference year.", ge=MIN_YEAR)],
    version: Annotated[str, Field(description="'last' (default), 'first' or 'all'.")] = "last",
) -> str:
    """Board, officers and fiscal council composition."""
    try:
        wanted = parse_version(version)
        frame, warnings, _ = await _collect_fre(
            "administrador_membro_conselho_fiscal", [int(cd_cvm)], int(year), int(year), wanted
        )
    except ValueError as exc:
        return error(str(exc))
    if frame.empty:
        return error(
            f"no board data for cd_cvm={cd_cvm} in {year}",
            hint="Committee members are in membro_comite; use get_fre_table.",
            warnings=warnings,
        )
    columns = [
        "orgao_administracao",
        "nome",
        "cpf",
        "profissao",
        "cargo_eletivo_ocupado",
        "data_eleicao",
        "data_posse",
        "prazo_mandato",
        "eleito_controlador",
        "numero_mandatos_consecutivos",
        "percentual_participacao_reunioes",
    ]
    warnings.append("committee members: get_fre_table('membro_comite', ...)")
    return render(frame, columns=columns, max_rows=500, warnings=warnings)


@mcp.tool(annotations=READ_ONLY)
async def get_capital(
    cd_cvm: Annotated[int, Field(description="CVM code from search_company.", ge=1)],
    year: Annotated[int, Field(description="Reference year.", ge=MIN_YEAR)],
    version: Annotated[str, Field(description="'last' (default), 'first' or 'all'.")] = "last",
) -> str:
    """Share capital events: amounts, approvals and share counts."""
    try:
        wanted = parse_version(version)
        frame, warnings, _ = await _collect_fre("capital_social", [int(cd_cvm)], int(year), int(year), wanted)
    except ValueError as exc:
        return error(str(exc))
    if frame.empty:
        return error(f"no capital data for cd_cvm={cd_cvm} in {year}", warnings=warnings)
    if "data_autorizacao_aprovacao" in frame.columns:
        frame = frame.sort_values("data_autorizacao_aprovacao", ascending=False, na_position="last")
    columns = [
        "data_referencia",
        "tipo_capital",
        "data_autorizacao_aprovacao",
        "valor_capital",
        "quantidade_acoes_ordinarias",
        "quantidade_acoes_preferenciais",
        "quantidade_total_acoes",
        "prazo_integralizacao",
    ]
    warnings.append("class detail: get_fre_table('capital_social_classe_acao', ...)")
    return render(frame, columns=columns, max_rows=500, warnings=warnings)


# ---------------------------------------------------------------------------
# VLMO: insider trading


@mcp.tool(annotations=READ_ONLY)
async def get_insider_trades(
    company: Annotated[
        str,
        Field(description="Company: cd_cvm, CNPJ or name (partial). E.g. 'petrobras' or '9512'."),
    ],
    first_year: Annotated[int | None, Field(description="First year (2018+).")] = None,
    last_year: Annotated[int | None, Field(description="Last year.")] = None,
    role: Annotated[
        str | None, Field(description="Optional role filter, e.g. 'Diretor', 'Conselho', 'Controlador'.")
    ] = None,
    operation: Annotated[
        str | None, Field(description="Optional 'Credito' (buy) or 'Debito' (sell).")
    ] = None,
    max_rows: Annotated[int, Field(description="Maximum rows.", ge=1, le=20_000)] = 2000,
) -> str:
    """Transactions by controllers, officers and board members (CVM/VLMO).

    Returns who traded, the role, the movement type, date, quantity, unit price
    and volume. This is the insider-trading signal dataset (Instruction 358).
    """
    code, problem = await _resolve_company(company)
    if code is None:
        return error(problem or "company not found")

    try:
        first, last = parse_years(first_year, last_year, span=2)
    except ValueError as exc:
        return error(str(exc))

    periods = [str(year) for year in range(first, last + 1)]
    try:
        await store.ensure_many("vlmo", periods, ["transacoes"])
        frame = store.read("vlmo", "transacoes", periods=periods, isin={"cd_cvm": [code]})
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    if role and "tipo_cargo" in frame.columns:
        frame = frame.loc[frame["tipo_cargo"].fillna("").map(fold).str.contains(fold(role), regex=False)]
    if operation and "tipo_operacao" in frame.columns:
        wanted = fold(operation)
        frame = frame.loc[frame["tipo_operacao"].fillna("").map(fold).str.contains(wanted, regex=False)]

    if frame.empty:
        return json_out({"company": company, "cd_cvm": code, "count": 0, "hint": "No movements match."})
    total = len(frame)
    if total > max_rows:
        frame = frame.head(int(max_rows))

    if "data_movimentacao" in frame.columns:
        frame = frame.sort_values("data_movimentacao", ascending=False)
    columns = [
        "nome_companhia",
        "empresa",
        "tipo_cargo",
        "tipo_movimentacao",
        "descricao_movimentacao",
        "tipo_operacao",
        "tipo_ativo",
        "caracteristica_valor_mobiliario",
        "data_movimentacao",
        "quantidade",
        "preco_unitario",
        "volume",
    ]
    note = f"# NOTE: showing {len(frame)} of {total} movements\n" if total > max_rows else ""
    header = f"# source: CVM/VLMO | cd_cvm={code} | {first}-{last}\n"
    return note + header + to_csv(frame, columns=columns)


@mcp.tool(annotations=READ_ONLY)
async def insider_summary(
    company: Annotated[str, Field(description="cd_cvm, CNPJ or company name.")],
    first_year: Annotated[int | None, Field(description="First year (2018+).")] = None,
    last_year: Annotated[int | None, Field(description="Last year.")] = None,
) -> str:
    """Net insider buying/selling by year and role (aggregate of VLMO)."""
    code, problem = await _resolve_company(company)
    if code is None:
        return error(problem or "company not found")
    try:
        first, last = parse_years(first_year, last_year, span=3)
    except ValueError as exc:
        return error(str(exc))

    periods = [str(year) for year in range(first, last + 1)]
    try:
        await store.ensure_many("vlmo", periods, ["transacoes"])
        frame = store.read("vlmo", "transacoes", periods=periods, isin={"cd_cvm": [code]})
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))
    if frame.empty:
        return json_out({"cd_cvm": code, "count": 0})

    frame = frame.copy()
    frame["year"] = frame["_period"].astype(str)
    frame["direction"] = frame["tipo_operacao"].fillna("").map(fold)
    frame["signed_volume"] = frame["volume"].where(frame["direction"].str.contains("credito", regex=False), -frame["volume"])
    grouped = (
        frame.groupby(["year", "tipo_cargo"], dropna=False)
        .agg(
            movements=("volume", "size"),
            volume=("volume", "sum"),
            net_volume=("signed_volume", "sum"),
        )
        .reset_index()
        .sort_values(["year", "net_volume"], ascending=[False, False])
    )
    header = f"# source: CVM/VLMO | cd_cvm={code} | positive net_volume = net buying\n"
    return header + to_csv(grouped)


# ---------------------------------------------------------------------------
# ICBGC: governance


@mcp.tool(annotations=READ_ONLY)
async def governance_practices(
    company: Annotated[str, Field(description="cd_cvm, CNPJ or company name.")],
    year: Annotated[int, Field(description="Reference year (2018+).", ge=2018)],
    chapter: Annotated[str | None, Field(description="Optional chapter filter, e.g. 'Acionistas'.")] = None,
    adopted: Annotated[
        str | None, Field(description="Optional filter: 'Sim', 'Nao' or 'Parcialmente'.")
    ] = None,
    max_rows: Annotated[int, Field(description="Maximum rows.", ge=1, le=5000)] = 500,
) -> str:
    """Explain-or-comply governance report: each practice and whether it was adopted."""
    code, problem = await _resolve_company(company)
    if code is None:
        return error(problem or "company not found")

    try:
        await store.ensure("cgvn", "praticas", str(int(year)))
        frame = store.read("cgvn", "praticas", periods=[str(int(year))], isin={"cd_cvm": [code]})
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))
    if frame.empty:
        return json_out({"cd_cvm": code, "year": year, "count": 0})

    if chapter and "capitulo" in frame.columns:
        frame = frame.loc[frame["capitulo"].fillna("").map(fold).str.contains(fold(chapter), regex=False)]
    if adopted and "pratica_adotada" in frame.columns:
        wanted = fold(adopted)
        frame = frame.loc[frame["pratica_adotada"].fillna("").map(fold).str.contains(wanted, regex=False)]

    if len(frame) > max_rows:
        return error(f"query would return {len(frame)} rows, above {max_rows}")
    columns = [
        "capitulo",
        "principio",
        "pratica_recomendada",
        "pratica_adotada",
        "explicacao",
    ]
    header = f"# source: CVM/ICBGC | cd_cvm={code} | {year}\n"
    return header + to_csv(frame, columns=columns)


@mcp.tool(annotations=READ_ONLY)
async def governance_score(
    company: Annotated[str, Field(description="cd_cvm, CNPJ or company name.")],
    year: Annotated[int, Field(description="Reference year (2018+).", ge=2018)],
) -> str:
    """Adoption rate of the recommended governance practices, overall and by chapter."""
    code, problem = await _resolve_company(company)
    if code is None:
        return error(problem or "company not found")

    try:
        await store.ensure("cgvn", "praticas", str(int(year)))
        frame = store.read("cgvn", "praticas", periods=[str(int(year))], isin={"cd_cvm": [code]})
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))
    if frame.empty:
        return json_out({"cd_cvm": code, "year": year, "practices": 0})

    status = frame["pratica_adotada"].fillna("").map(fold)
    frame = frame.assign(
        _yes=status.str.startswith("sim"),
        _partial=status.str.contains("parcial", regex=False),
        _no=status.str.startswith("nao"),
    )
    applicable = frame.loc[frame["_yes"] | frame["_partial"] | frame["_no"]]

    def rate(subset: pd.DataFrame) -> float | None:
        total = len(subset)
        if not total:
            return None
        return round(100.0 * (subset["_yes"].sum() + 0.5 * subset["_partial"].sum()) / total, 2)

    by_chapter = pd.DataFrame(
        {
            "practices": applicable.groupby("capitulo", dropna=False).size(),
            "adopted": applicable.groupby("capitulo", dropna=False)["_yes"].sum(),
            "partial": applicable.groupby("capitulo", dropna=False)["_partial"].sum(),
            "not_adopted": applicable.groupby("capitulo", dropna=False)["_no"].sum(),
        }
    ).reset_index()
    by_chapter["adoption_pct"] = (
        (by_chapter["adopted"] + 0.5 * by_chapter["partial"]) / by_chapter["practices"] * 100
    ).round(2)
    by_chapter = by_chapter.sort_values("adoption_pct", ascending=False)
    return json_out(
        {
            "cd_cvm": code,
            "year": year,
            "company": clean_json_value(frame["nome_empresarial"].dropna().iloc[0])
            if "nome_empresarial" in frame.columns and frame["nome_empresarial"].notna().any()
            else None,
            "practices": len(applicable),
            "adopted": int(applicable["_yes"].sum()),
            "partial": int(applicable["_partial"].sum()),
            "not_adopted": int(applicable["_no"].sum()),
            "adoption_pct": rate(applicable),
            "note": "Score = adopted + half of partially adopted, over applicable practices.",
            "by_chapter": by_chapter.to_dict(orient="records"),
        }
    )

"""Tool surface: registration, naming and the no-network error paths."""

from __future__ import annotations

import asyncio

from cvm_mcp.server import mcp

EXPECTED = {
    "search_company",
    "resolve",
    "get_tickers",
    "get_financial_statements",
    "get_account_history",
    "get_statement_summary",
    "list_fre_tables",
    "get_fre_table",
    "get_company_overview",
    "get_shareholders",
    "get_compensation",
    "get_board",
    "get_capital",
    "get_insider_trades",
    "insider_summary",
    "governance_practices",
    "governance_score",
    "search_fund",
    "get_fund_nav",
    "get_fund_flows",
    "get_fund_portfolio",
    "rank_funds",
    "list_datasets",
    "describe_dataset",
    "cache_status",
    "refresh_cache",
    "run_query",
    "search_offerings",
    "issuance_volume",
    "league_table",
}


def tool_names() -> set[str]:
    tools = asyncio.run(mcp.list_tools())
    return {tool.name for tool in tools}


def test_expected_tools_are_registered():
    names = tool_names()
    missing = EXPECTED - names
    assert not missing, f"missing tools: {sorted(missing)}"


def test_tool_surface_stays_small():
    # The whole point of the generic run_query layer is not to have one tool per
    # table. Keep the curated surface under control.
    assert len(tool_names()) <= 40


def test_every_tool_has_a_description():
    for tool in asyncio.run(mcp.list_tools()):
        assert tool.description and len(tool.description) > 20, tool.name


def test_list_datasets_does_not_hit_the_network():
    from cvm_mcp.tools_meta import list_datasets

    payload = list_datasets()
    assert '"dfp"' in payload
    assert '"fi_inf_diario"' in payload
    assert '"oferta"' in payload


def test_describe_unknown_dataset_returns_an_error():
    from cvm_mcp.tools_meta import describe_dataset

    assert '"error"' in describe_dataset("does-not-exist")


def test_overview_without_registry_returns_an_error(monkeypatch):
    from cvm_mcp import tools_companies

    async def explode(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(tools_companies.store, "registry", explode)
    result = asyncio.run(tools_companies.get_company_overview(9512, 2023))
    assert '"error"' in result


def test_fund_family_routing():
    from cvm_mcp.tools_funds import _fund_family

    assert _fund_family("Classes de Cotas de Fundos FIF") == "fi"
    assert _fund_family("CLASSE FIF/FAPI") == "fi"
    assert _fund_family("Classes de Cotas de Fundos FIDC") == "fidc"
    assert _fund_family("Classes de Cotas de Fundos FII") == "fii"
    assert _fund_family("Classes de Cotas de Fundos FIIM") == "fii"
    assert _fund_family("Classes de Cotas de Fundos FIAGRO") == "fiagro"
    assert _fund_family("Classes de Cotas de Fundos FIP") == "fip"
    assert _fund_family(None) == "fi"


def test_month_helpers():
    from cvm_mcp.tools_funds import _month_iso, _month_range, _shift_month, _years_of

    assert _shift_month("202501", -1) == "202412"
    assert _shift_month("202512", 1) == "202601"
    assert _month_iso("202503") == "2025-03"
    assert _month_range("202511", "202602") == ["202511", "202512", "202601", "202602"]
    assert _years_of(["202412", "202501", "202503"]) == ["2024", "2025"]


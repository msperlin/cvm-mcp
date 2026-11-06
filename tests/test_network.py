"""Network tests: the unified server really returns CVM data.

Marked ``network`` so the default run stays offline:

    uv run pytest -m "not network"
    uv run pytest -m network
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from cvm_mcp import tools_companies, tools_funds, tools_offerings

pytestmark = pytest.mark.network


def test_search_company_resolves_vale():
    result = asyncio.run(tools_companies.search_company("vale", limit=3))
    assert "VALE S.A." in result
    assert '"cd_cvm": 4170' in result


def test_statement_summary_matches_known_vale_figures():
    result = asyncio.run(tools_companies.get_statement_summary(cvm_code=4170, year=2023))
    assert "total_assets" in result
    assert "455984000000.00" in result  # Vale total assets 2023, in BRL
    assert "208066000000.00" in result  # net revenue 2023


def test_fre_shareholders_for_vale():
    result = asyncio.run(tools_companies.get_shareholders(cd_cvm=4170, year=2023))
    assert "PREVI" in result
    assert "2023-12-31" in result


def test_insider_trades_for_vale():
    result = asyncio.run(
        tools_companies.get_insider_trades(company="4170", first_year=2025, last_year=2025, max_rows=3)
    )
    assert "CVM/VLMO" in result


def test_governance_score_for_vale():
    result = asyncio.run(tools_companies.governance_score(company="4170", year=2025))
    assert "adoption_pct" in result


def test_fund_nav_for_a_large_fif():
    result = asyncio.run(
        tools_funds.get_fund_nav(fund="07.593.972/0001-86", first_period="2025-09", last_period="2025-09")
    )
    assert "vl_quota" in result
    assert "2025-09" in result


def test_offerings_volume_for_2024():
    result = asyncio.run(
        tools_offerings.issuance_volume(
            group_by="asset_type", first_date="2024-01-01", last_date="2024-12-31", limit=5
        )
    )
    assert "Debêntures" in result


def test_fii_nav_and_portfolio():
    nav = asyncio.run(
        tools_funds.get_fund_nav(
            fund="00.332.266/0001-31", first_period="2025-01", last_period="2025-03"
        )
    )
    assert "CVM/FII" in nav
    assert "patrimonio_liquido" in nav
    assert "Shoppings" in nav

    portfolio = asyncio.run(
        tools_funds.get_fund_portfolio(
            fund="00.332.266/0001-31", period="2025-03", max_rows=50
        )
    )
    assert "CVM/FII" in portfolio
    assert "component,value" in portfolio


def test_fidc_report_and_portfolio():
    report = asyncio.run(
        tools_funds.get_fidc_report(
            fund="05.754.060/0001-13", first_period="2025-07", last_period="2025-09"
        )
    )
    assert "CVM/FIDC" in report
    assert "inadimplencia_pct" in report

    portfolio = asyncio.run(
        tools_funds.get_fund_portfolio(
            fund="05.754.060/0001-13", period="2025-09", max_rows=50
        )
    )
    assert "CVM/FIDC" in portfolio


def test_fiagro_nav():
    result = asyncio.run(
        tools_funds.get_fund_nav(
            fund="12.358.507/0001-09", first_period="2025-10", last_period="2025-10"
        )
    )
    assert "CVM/FIAGRO" in result
    assert "EXITUS" in result


def test_fund_family_routing_is_explicit():
    for query, family in (
        ("00.332.266/0001-31", "fii"),
        ("05.754.060/0001-13", "fidc"),
        ("12.358.507/0001-09", "fiagro"),
    ):
        result = asyncio.run(tools_funds.search_fund(query, limit=1))
        assert f'"family": "{family}"' in result


@pytest.mark.parity
def test_parity_with_standalone_servers():
    """Run the data-level parity script against the two standalone MCPs."""
    script = Path(__file__).resolve().parents[1] / "scripts" / "parity_vs_mcps.py"
    if not script.exists():
        pytest.skip("parity script not found")
    references = [
        Path(__file__).resolve().parents[2] / name for name in ("cvm-dfp-mcp", "cvm-fre-mcp")
    ]
    if not all(reference.exists() for reference in references):
        pytest.skip("legacy cvm-dfp-mcp / cvm-fre-mcp are no longer installed")
    result = subprocess.run(
        [sys.executable, str(script), "--year", "2023"],
        check=False,
        capture_output=True,
        text=True,
        timeout=2400,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "Parity confirmed" in result.stdout

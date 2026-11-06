"""Entry point: import the tool modules for their registration side effect, run."""

from __future__ import annotations

import os

from . import tools_companies, tools_funds, tools_meta, tools_offerings  # noqa: F401
from .app import mcp


@mcp.resource("cvm://datasets", mime_type="application/json")
def datasets_resource() -> str:
    """The full dataset registry."""
    return tools_meta.list_datasets(detail=True)


@mcp.prompt()
def analyze_company(company: str, year: str = "latest") -> str:
    """Draft a financial + governance profile of one Brazilian listed company."""
    return f"""\
Analyze {company} (reference year: {year}) using CVM open data.

Steps:
1. `resolve` or `search_company` to get the cd_cvm.
2. `get_statement_summary` for the headline figures of the year.
3. `get_account_history` for revenue, ebit, net_income and equity over 5 years.
4. `get_shareholders`, `get_board`, `get_compensation` for ownership and pay.
5. `governance_score` for the ICBGC adoption rate.
6. `get_insider_trades` for recent insider activity.
State the reference period and cite the dataset (CVM/DFP, CVM/FRE, CVM/VLMO).
Do not invent numbers: every figure must come from a tool result.
"""


@mcp.prompt()
def analyze_fund(fund: str, month: str = "latest") -> str:
    """Draft a performance and portfolio profile of one investment fund."""
    return f"""\
Analyze the fund {fund} (month: {month}) using CVM open data.

Steps:
1. `search_fund` to get the class CNPJ.
2. `get_fund_nav` for the last 12 months of quota and net asset value.
3. `get_fund_flows` for subscriptions, redemptions and net flow.
4. `get_fund_portfolio` for the current holdings and issuer concentration.
5. If the question compares funds, `rank_funds` on the same month.
State the period and cite "CVM/FI". Fund returns are quota returns, not
dividend-adjusted; say so explicitly.
"""


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        mcp.run()
    elif transport in {"streamable-http", "sse"}:
        mcp.run(
            transport=transport,  # type: ignore[arg-type]
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8000")),
        )
    else:
        raise SystemExit(
            f"invalid MCP_TRANSPORT: {transport!r} (use stdio, streamable-http or sse)"
        )


if __name__ == "__main__":
    main()

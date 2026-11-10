"""The MCP application object.

Kept separate from the tool modules so they can import ``mcp`` without creating
an import cycle; ``server.py`` imports the tool modules for their registration
side effect.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from . import __version__

INSTRUCTIONS = """\
One MCP server for the whole CVM open-data repository (dados.cvm.gov.br):
financial statements, corporate filings, fund portfolios, insider trading,
governance, public offerings and sanctioning proceedings.

How to work with this server:
1. Call `search_company` (or `resolve`) to turn a name, ticker or CNPJ into a
   `cd_cvm`. Codes, not tickers, are the key used by the corporate tools.
2. Call `list_datasets` to see everything available, and `describe_dataset`
   with `detail=True` for column meanings, units and caveats.
3. Prefer the curated tools (`get_statement_summary`, `get_fund_nav`,
   `get_insider_trades`, `governance_score`, ...) when they fit the question.
4. Before `run_query`, call `load_dataset` to download and parse the period
   (it reuses fresh archives; `refresh_cache` is the forcing version).
5. Use `run_query` (DuckDB SQL, read-only) to cross datasets. Views are named
   `<dataset>__<table>`, e.g. `dfp__dre_con`, `fi_inf_diario__informe`,
   `vlmo__transacoes`. `list_datasets` shows the view names.

Important:
- DFP/ITR values: `VL_CONTA` is raw, `VL_CONTA_BRL` is already scaled by
  ESCALA_MOEDA (thousands) and is the number to report.
- Notas explicativas are free text inside the full filing, not a CVM table:
  `get_dfp_notes` / `search_dfp_notes` download and read the filing PDF linked
  from `dfp__documento.link_doc`. Cite the PDF page they return.
- FRE and the newer datasets use snake_case columns; DFP/ITR keep CVM's
  uppercase names for compatibility with the standalone servers.
- Data is cached locally on first use; the first call for a period downloads
  and parses an archive (seconds to a minute, the CDA months are large).
- Fund tools route by instrument: FIF/FAPI report daily, FII/FIDC/FIAGRO
  monthly, FIP quarterly (not yet available).
- Do not invent numbers: every figure must come from a tool result. Cite
  "CVM/DFP", "CVM/FRE", "CVM/FI" or "CVM" with the reference period.
"""

mcp = MCPServer(
    "cvm-mcp",
    title="CVM — whole open-data repository",
    description=(
        "Unified access to Brazilian capital-market data: DFP/ITR statements, "
        "FRE corporate filings, insider trading, governance, funds, offerings "
        "and sanctions."
    ),
    instructions=INSTRUCTIONS,
    version=__version__,
)

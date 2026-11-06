# cvm-mcp

One MCP server for the whole [CVM open-data repository](https://dados.cvm.gov.br)
(`dados.cvm.gov.br`), the source behind the R packages *GetDFPData2* and
*GetFREData*.

It replaces, and is a strict superset of, `cvm-dfp-mcp` and `cvm-fre-mcp`:

| Domain | Datasets |
|---|---|
| Companies | `dfp`, `itr`, `fre` (36 tables), `fca`, `vlmo` (insider trading), `cgvn` (governance) |
| Funds | `fi_inf_diario`, `fi_cda`, `fi_cad`, `fii_inf_mensal`, `fidc_inf_mensal` (18 tables), `fiagro_inf_mensal` |
| Offerings | `oferta` (ICVM 400 / RCVM 160) |
| Enforcement | `sancionador` |
| Registry | `cad_cia_aberta` |

**15 datasets, 110 tables, 33 tools.**

## Why one server

The portal has ~1,259 files and 10.1 GB of archives, but only about five
*shapes* of data. The server describes each dataset declaratively in
`registry.py` — URL pattern, period granularity, tables inside the archive,
normalizer — and the store turns every `(dataset, table, period)` into one
Parquet partition. Adding a dataset is a data change, not a code change.

## Design

```
src/cvm_mcp/
├── app.py            MCP application object + instructions
├── registry.py       declarative catalog of datasets and tables
├── fetch.py          download, cache, revalidation, cross-process lock
├── store.py          Parquet partitions + DuckDB views + read-only SQL
├── normalize.py      Latin-1 reader, header cleaning, CNPJ, type inference
├── identity.py       CNPJ ↔ cd_cvm ↔ ticker resolution
├── schema_dfp.py     canonical DFP/ITR schema (shared with cvm-dfp-mcp)
├── catalog_dfp.py    curated chart of accounts (shared with cvm-dfp-mcp)
├── catalog_fre.py    the 36 FRE tables (shared with cvm-fre-mcp)
├── fre_normalize.py  FRE normalization (shared with cvm-fre-mcp)
├── tools_*.py        the MCP tools, one module per domain
└── server.py         entry point
```

Two tool layers:

* **Curated** (33 tools): `get_statement_summary`, `get_fund_nav`,
  `get_insider_trades`, `governance_score`, `league_table`, ...
* **Generic** (`run_query`): DuckDB SQL over every cached table, with views named
  `<dataset>__<table>`. This is the escape hatch that makes "the whole base"
  reachable without one tool per table.

## Parity with the standalone servers

`scripts/parity_vs_mcps.py` builds the same slices twice — once through each
standalone server's store, once through `cvm_mcp.store` — and compares every
cell. It needs `cvm-dfp-mcp` and `cvm-fre-mcp` checked out alongside this repo,
and skips with a message when they are absent. Last run (2023):

```
DFP/BPA:   196 rows, 21 columns compared
DFP/BPP:   306 rows, 21 columns compared
DFP/DRE:   107 rows, 21 columns compared
FRE/documento:                            5216 rows,  9 columns
FRE/posicao_acionaria:                   27850 rows, 30 columns
FRE/administrador_membro_conselho_fiscal: 9296 rows, 22 columns
FRE/remuneracao_total_orgao:              6254 rows, 28 columns
FRE/capital_social:                       2492 rows, 14 columns
FRE/transacao_parte_relacionada:         10411 rows, 23 columns
Parity confirmed: cvm-mcp matches cvm-dfp-mcp and cvm-fre-mcp.
```

Companies checked: Banco do Brasil (bank chart), Vale, and a second filer.
Tolerance: 0.01 on every numeric column; exact match on text, dates and keys.

## Install

```bash
uv sync
uv run cvm-mcp          # stdio
```

`opencode.example.json` shows the MCP client configuration. The cache lives in
`~/.cache/cvm-mcp` (override with `CVMMCP_CACHE_DIR`).

## Cache layout

```
~/.cache/cvm-mcp/
├── downloads/                     raw zips and CSVs, revalidated with If-Modified-Since
└── parquet/v1/<dataset>/<table>/period=<YYYY|YYYYMM|all>/data.parquet
```

Everything is lazy: a dataset is downloaded and parsed the first time a tool
asks for it. The current month is rewritten by CVM daily, so its partitions are
refreshed every 12 h (configurable with `CVMMCP_REFRESH_HOURS`).

## Tests

```bash
uv run pytest -m "not network"    # fast, offline
uv run pytest -m network          # hits dados.cvm.gov.br
uv run python scripts/parity_vs_mcps.py   # data parity with the standalone servers
```

The parity script builds the same DFP and FRE slices through `cvm-mcp` and
through `cvm-dfp-mcp` / `cvm-fre-mcp`, then compares them row by row.

## Coverage notes

* DFP/ITR keep CVM's uppercase column names and `VL_CONTA_BRL`.
* FRE keeps the GetFREData-compatible snake_case schema and key block.
* Newer datasets (VLMO, CGVN, funds, offerings) are normalized to snake_case and
  gain a 14-digit `cnpj` column plus a `_period` column.
* `fi_cda` is heavy (~270 MB uncompressed per month); build one block at a time.

### Fund family routing

The CVM splits funds by *instrument*, not by manager, and the tools hide that.
`get_fund_nav` and `get_fund_portfolio` resolve the class CNPJ in `fi_cad`,
read its `tipo_classe` and pick the dataset:

| Family | Series | Portfolio |
|---|---|---|
| FIF / FAPI | `fi_inf_diario` (daily) | `fi_cda` blocks 1–8 |
| FII | `fii_inf_mensal` (monthly) | `fii_inf_mensal/ativo_passivo` |
| FIDC | `fidc_inf_mensal/tab_iv` | `fidc_inf_mensal/tab_ii` (credit segments) |
| FIAGRO | `fiagro_inf_mensal` | `fi_cda/fie` |

`get_fidc_report` adds the credit dashboard (assets, portfolio, PL, delinquency).
FIP is quarterly and is not in the registry yet; the tools say so explicitly.

Known gaps: FIDC monthly reports start in 2025-01 — 2013–2024 lives in the
`HIST/` folder with a different layout (one archive per year, monthly members)
and is not wired up. FII history covers 2016 onwards.

## License

MIT.

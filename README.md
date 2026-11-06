# cvm-mcp

[![PyPI](https://img.shields.io/pypi/v/cvm-mcp)](https://pypi.org/project/cvm-mcp/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-stdio-6f42c1)](https://modelcontextprotocol.io/)

An MCP server for the [CVM open-data repository](https://dados.cvm.gov.br)
(`dados.cvm.gov.br`) — the Brazilian securities regulator and the source behind
the R packages *GetDFPData2* and *GetFREData*.

It gives Claude Code, OpenCode, Cursor, VS Code, Codex, Gemini CLI and any other
MCP client one interface to:

- annual and quarterly financial statements (DFP / ITR)
- the Formulário de Referência (FRE, 36 corporate tables)
- fund NAVs, flows, portfolios and credit-fund reports
- insider trading, governance, public offerings and sanctioning proceedings
- ad-hoc SQL over everything, via DuckDB

**15 datasets, 111 tables, 34 tools.** Everything runs locally and read-only;
data is downloaded on demand and cached on disk.

> This server consolidates the earlier `cvm-dfp-mcp` and `cvm-fre-mcp` servers
> and is a strict superset of both.

## Contents

- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Client setup](#client-setup)
  - [Claude Code](#claude-code)
  - [OpenCode](#opencode)
  - [Claude Desktop](#claude-desktop)
  - [Cursor](#cursor)
  - [VS Code / GitHub Copilot](#vs-code--github-copilot)
  - [Codex CLI](#codex-cli)
  - [Gemini CLI](#gemini-cli)
  - [Continue](#continue)
  - [Other MCP clients](#other-mcp-clients)
- [Example prompts](#example-prompts)
- [Datasets](#datasets)
- [Tools](#tools)
- [Configuration](#configuration)
- [Cache](#cache)
- [Data notes](#data-notes)
- [How it works](#how-it-works)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Requirements

- **Python 3.10+** — `uv` installs and manages it for you.
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — the only
  prerequisite.
- Network access to `dados.cvm.gov.br`.
- Disk space for the cache. It is lazy: nothing is downloaded until you ask for
  it. A full DFP + FRE history is a few GB; `fi_cda` is the heavy one (~270 MB
  uncompressed per month).

Install `uv`:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Verify with `uv --version`.

## Quick start

The package is on [PyPI](https://pypi.org/project/cvm-mcp/).

**Run without installing** (best for trying it out and for client configs):

```bash
uvx cvm-mcp
```

This starts a stdio MCP server and waits for a client. Seeing no output is
expected; press `Ctrl+C` to stop it.

**Install a persistent `cvm-mcp` command** (optional):

```bash
uv tool install cvm-mcp
cvm-mcp
```

If `cvm-mcp` is not found after installing, run `uv tool update-shell` and
restart your terminal.

**From source** (for development):

```bash
git clone https://github.com/msperlin/cvm-mcp.git
cd cvm-mcp
uv sync
uv run cvm-mcp
```

## Client setup

All configurations below run the server over **stdio**. They use `uvx`, which
resolves and caches the package on first use.

> **GUI clients and `PATH`:** Claude Desktop, Cursor and VS Code often do not
> inherit your shell `PATH`. If the server fails to start with “command not
> found”, run `which uvx` (macOS/Linux) or `where uvx` (Windows) and use that
> absolute path as `command` — commonly `~/.local/bin/uvx`. Alternatively,
> `uv tool install` and use the absolute path to `cvm-mcp`.

### Claude Code

Add it from the CLI, scoped to your user (available in every project):

```bash
claude mcp add --transport stdio --scope user cvm-mcp -- \
  uvx cvm-mcp
```

Check the connection:

```bash
claude mcp list
```

Or commit a project-scoped `.mcp.json` to the repository root:

```json
{
  "mcpServers": {
    "cvm-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "cvm-mcp"
      ],
      "timeout": 600000
    }
  }
}
```

`timeout` is in milliseconds and applies per tool call. The first CVM download
can take a while; 10 minutes is a safe ceiling.

### OpenCode

OpenCode V2 configures servers under `mcp.servers`. Add it globally with the
CLI:

```bash
opencode mcp add cvm-mcp --global -- \
  uvx cvm-mcp
```

Check it with `opencode mcp list` (or `/mcps` inside OpenCode).

Or edit `~/.config/opencode/opencode.json` (global) or `opencode.json`
(project):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "cvm-mcp": {
        "type": "local",
        "command": [
          "uvx",
          "cvm-mcp"
        ]
      }
    }
  }
}
```

The repository also ships a ready-to-copy
[`opencode.example.json`](opencode.example.json).

> OpenCode V1 used server names directly under `mcp`; V2 uses `mcp.servers`.

### Claude Desktop

Edit the Claude Desktop config file:

| OS | Path |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "cvm-mcp": {
      "command": "uvx",
      "args": [
        "cvm-mcp"
      ]
    }
  }
}
```

Quit Claude Desktop completely and reopen it. The server appears under
**Connectors** / the tools menu.

### Cursor

Create `~/.cursor/mcp.json` for all projects, or `.cursor/mcp.json` in a
project:

```json
{
  "mcpServers": {
    "cvm-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "cvm-mcp"
      ]
    }
  }
}
```

Reload the window if the server does not appear under **Customize → MCP**.

### VS Code / GitHub Copilot

Create `.vscode/mcp.json` in the workspace:

```json
{
  "servers": {
    "cvm-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": [
        "cvm-mcp"
      ]
    }
  }
}
```

VS Code uses the `servers` key. Portable `.mcp.json` files (Claude Desktop,
Cursor, Cline, etc.) use `mcpServers` instead — do not mix the two.

You can also run **MCP: Add Server** from the Command Palette and choose
**Command (stdio)** with:

```
uvx cvm-mcp
```

### Codex CLI

Add to `~/.codex/config.toml` (or `.codex/config.toml` in a trusted project):

```toml
[mcp_servers.cvm-mcp]
command = "uvx"
args = ["cvm-mcp"]
startup_timeout_sec = 30
tool_timeout_sec = 600
```

Codex reads server instructions and exposes the tools automatically. Use
`/mcp` in a Codex session to inspect the connection.

### Gemini CLI

Add to `~/.gemini/settings.json` (user) or `.gemini/settings.json` (project):

```json
{
  "mcpServers": {
    "cvm-mcp": {
      "command": "uvx",
      "args": [
        "cvm-mcp"
      ],
      "timeout": 600000
    }
  }
}
```

Then run `/mcp` inside Gemini CLI to check the connection. `timeout` is in
milliseconds.

### Continue

Create `.continue/mcpServers/cvm-mcp.yaml`:

```yaml
name: CVM MCP
version: 0.0.1
schema: v1
mcpServers:
  - name: CVM
    type: stdio
    command: uvx
    args:
      - "cvm-mcp"
```

Continue can also import the JSON block from the [Other MCP clients](#other-mcp-clients)
section into `.continue/mcpServers/`.

### Other MCP clients

Most clients accept a stdio server in this shape:

```json
{
  "mcpServers": {
    "cvm-mcp": {
      "command": "uvx",
      "args": [
        "cvm-mcp"
      ]
    }
  }
}
```

If your client asks for a single command line:

```bash
uvx cvm-mcp
```

Windows clients that cannot find `uvx` can use the executable installed by
`uv tool install` directly, e.g.
`C:\Users\<you>\.local\bin\cvm-mcp.exe`.

## Example prompts

Once connected, ask in natural language:

- “What were Vale's revenue, EBIT and net debt in 2024? Cite the CVM/DFP
  accounts.”
- “Compare the last five years of ROE, net margin and net debt/EBITDA for
  Petrobras, Vale and Itaú.”
- “Show the board composition, compensation and shareholders of Banco do
  Brasil in the latest Formulário de Referência.”
- “Which insiders bought or sold WEG shares in the last six months?”
- “List the largest FIDCs by net asset value in the latest month, with
  delinquency and credit segments.”
- “How much did Brazilian companies raise in public offerings in 2024, by asset
  type and lead underwriter?”
- “Find sanctioning proceedings mentioning insider trading since 2023.”
- “Load DFP 2024 and run a SQL query joining the income statement with the
  company registry.”

The server is instructed to cite the dataset and reference period and never to
invent figures.

## Datasets

| Domain | Datasets | Coverage |
|---|---|---|
| Companies | `dfp`, `itr`, `fre` (36 tables), `fca`, `vlmo` (insider trading), `cgvn` (governance) | DFP/ITR/FRE 2010+; FCA/VLMO/CGVN 2018+ |
| Funds | `fi_inf_diario`, `fi_cda`, `fi_cad`, `fii_inf_mensal`, `fidc_inf_mensal` (18 tables), `fiagro_inf_mensal` | FI daily 2021-01+; CDA 2023-01+; FII 2016+; FIDC 2025-01+; FIAGRO 2025-05+ |
| Offerings | `oferta` (ICVM 400 / RCVM 160) | 1988+ |
| Enforcement | `sancionador` (PAS) | current |
| Registry | `cad_cia_aberta` | current |

Use `list_datasets` from a client to see the exact table names and view names.
`describe_dataset` shows columns and coverage; `describe_dataset(detail=True)`
adds units and caveats.

## Tools

There are two layers.

**Curated tools** for the common questions:

| Group | Tools |
|---|---|
| Identity | `search_company`, `resolve`, `list_companies`, `get_tickers` |
| Statements | `list_account_codes`, `get_financial_statements`, `get_account_history`, `get_statement_summary` |
| Corporate filings | `list_fre_tables`, `get_fre_table`, `get_company_overview`, `get_shareholders`, `get_compensation`, `get_board`, `get_capital` |
| Insiders & governance | `get_insider_trades`, `insider_summary`, `governance_practices`, `governance_score` |
| Funds | `search_fund`, `get_fund_nav`, `get_fund_flows`, `get_fund_portfolio`, `get_fidc_report`, `rank_funds` |
| Offerings | `search_offerings`, `issuance_volume`, `league_table` |

**Meta tools** support both layers:

| Tool | Purpose |
|---|---|
| `list_datasets` | All datasets, tables and DuckDB view names |
| `describe_dataset` | Columns, cached periods and (with `detail=True`) column meanings |
| `load_dataset` | Download/parse a dataset or period without forcing a refresh |
| `cache_status` | What is cached, per dataset, with sizes |
| `refresh_cache` | Force a re-download and rebuild |
| `run_query` | Read-only DuckDB SQL over every cached table |

`run_query` sees tables as `<dataset>__<table>`, e.g. `dfp__dre_con`,
`fi_inf_diario__informe`, `vlmo__transacoes`. Only cached periods exist, so call
`load_dataset` first when you need a specific period.

## Configuration

All settings are optional environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `CVMMCP_CACHE_DIR` | `~/.cache/cvm-mcp` | Cache root (downloads + Parquet) |
| `CVMMCP_REFRESH_HOURS` | `12` | Revalidation window for CVM archives; `0` disables revalidation |
| `MCP_TRANSPORT` | `stdio` | `stdio`, `streamable-http` or `sse` |
| `MCP_HOST` | `127.0.0.1` | Host for the HTTP transports |
| `MCP_PORT` | `8000` | Port for the HTTP transports |

Add `CVMMCP_CACHE_DIR` wherever your client expects environment variables:

```json
{
  "env": {
    "CVMMCP_CACHE_DIR": "/absolute/path/to/cvm-cache"
  }
}
```

For a long-running HTTP server:

```bash
MCP_TRANSPORT=streamable-http MCP_HOST=127.0.0.1 MCP_PORT=8000 cvm-mcp
```

The HTTP transports have **no authentication**; bind to localhost or put the
server behind a reverse proxy.

## Cache

```
~/.cache/cvm-mcp/
├── downloads/                     raw zips and CSVs, revalidated with If-Modified-Since
│   └── <archive>.meta.json        remote Last-Modified/ETag + last revalidation time
└── parquet/v1/<dataset>/<table>/period=<YYYY|YYYYMM|all>/data.parquet
```

- Everything is lazy. The first call for a dataset downloads and parses the CVM
  archive; later calls read the Parquet partition.
- CVM rewrites the current month in place, so archives are revalidated every
  12 h by default (`CVMMCP_REFRESH_HOURS`).
- A `304 Not Modified` updates only the sidecar's `checked_at`; the archive mtime
  stays put, so the Parquet partition is reused instead of being parsed again.
- `load_dataset` populates the cache without forcing a refresh.
  `refresh_cache` forces a re-download and rebuild.
- `cache_status` reports datasets, periods, partition counts and bytes. To
  reclaim space, delete `~/.cache/cvm-mcp/parquet` (it is rebuilt on demand) or
  point `CVMMCP_CACHE_DIR` at a larger disk.

## Data notes

- **DFP / ITR** keep CVM's uppercase column names. `VL_CONTA` is the raw value;
  `VL_CONTA_BRL` is already scaled by `ESCALA_MOEDA` and is the number to report.
- **FRE** keeps the GetFREData-compatible snake_case schema and key block
  (`cd_cvm`, `cnpj`, `nome_companhia`, `data_referencia`, `versao`,
  `id_documento`).
- Newer datasets (VLMO, CGVN, funds, offerings) are normalized to snake_case and
  gain a 14-digit `cnpj` column and a `_period` column.
- `fi_cda` is heavy (~270 MB uncompressed per month). Build one block/period at a
  time; `load_dataset` refuses more than three periods of a heavy dataset in one
  call.

### Fund family routing

CVM splits funds by *instrument*, not by manager, and the tools hide that.
`get_fund_nav` and `get_fund_portfolio` resolve the class CNPJ in `fi_cad`, read
its `tipo_classe` and pick the dataset:

| Family | Series | Portfolio |
|---|---|---|
| FIF / FAPI | `fi_inf_diario` (daily) | `fi_cda` blocks 1–8 |
| FII | `fii_inf_mensal` (monthly) | `fii_inf_mensal/ativo_passivo` |
| FIDC | `fidc_inf_mensal/tab_iv` | `fidc_inf_mensal/tab_ii` (credit segments) |
| FIAGRO | `fiagro_inf_mensal` | `fi_cda/fie` |

`get_fidc_report` adds the credit dashboard (assets, portfolio, PL, delinquency).

**Known gaps:** FIP is quarterly and is not wired up yet; the tools say so
explicitly. FIDC monthly reports start in 2025-01 — 2013–2024 lives in the
`HIST/` folder with a different layout and is not wired up. FII history starts
in 2016.

## How it works

The portal has ~1,259 files and ~10 GB of archives, but only about five *shapes*
of data. Each dataset is described declaratively in `registry.py` — URL pattern,
period granularity, tables inside the archive, normalizer — and the store turns
every `(dataset, table, period)` into one Parquet partition. Adding a dataset is
a data change, not a code change.

```
src/cvm_mcp/
├── app.py             MCP application object + server instructions
├── registry.py        declarative catalog of datasets and tables
├── fetch.py           download, revalidation, metadata sidecars, file locks
├── store.py           Parquet partitions + DuckDB views + read-only SQL
├── normalize.py       Latin-1 reader, header cleaning, CNPJ, type inference
├── identity.py        CNPJ ↔ cd_cvm ↔ ticker resolution
├── schema_dfp.py      canonical DFP/ITR schema
├── catalog_dfp.py     curated chart of accounts and roles
├── catalog_fre.py     the 36 FRE tables
├── fre_normalize.py   FRE normalization
├── data_dictionary.py curated column descriptions, units and caveats
├── tools_*.py         the MCP tools, one module per domain
└── server.py          entry point
```

Each archive is downloaded once and revalidated with `If-Modified-Since`;
downloads are guarded by a cross-process file lock, and SQL is restricted to a
single read-only `SELECT` / `WITH` over the cached views.

## Development

```bash
git clone https://github.com/msperlin/cvm-mcp.git
cd cvm-mcp
uv sync

uv run pytest -m "not network"      # fast, offline
uv run pytest -m network            # hits dados.cvm.gov.br
uv run python scripts/smoke_mcp.py  # end-to-end stdio smoke test
```

To run the working tree instead of the GitHub package in a client config, point
the command at the checkout:

```json
{
  "command": "uv",
  "args": ["run", "--directory", "/absolute/path/to/cvm-mcp", "cvm-mcp"]
}
```

## Releasing

Publishing happens on GitHub, not from a laptop, through PyPI
[trusted publishing](https://docs.pypi.org/trusted-publishers/) (OIDC), so no
API token is stored anywhere.

1. Bump `version` in `pyproject.toml` and commit it.
2. Tag the commit with a matching `v` prefix and push it:

   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```

3. Draft a GitHub release for that tag and publish it.

The `publish` workflow in `.github/workflows/publish.yml` then runs the offline
test suite, builds the sdist and wheel, checks the metadata, verifies the tag
matches the version, and uploads to PyPI. Re-running it manually
(`workflow_dispatch`) rebuilds and re-uploads the current version, which PyPI
rejects if the files already exist — so only do that after a failed release.

One-time setup (already required before the first upload): on PyPI, add a
pending publisher for project `cvm-mcp` with owner `msperlin`, repository
`cvm-mcp`, workflow `publish.yml`, and environment `pypi`. The matching
environment is declared in the workflow.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `uvx: command not found` | Install `uv`, or use the absolute path from `which uvx` / `where uvx`. GUI clients often do not inherit `PATH`. |
| Server starts but no tools appear | Check the client's MCP panel: `claude mcp list`, `opencode mcp list`, `/mcp` in Gemini/Codex/Cursor/VS Code. Restart the app after editing JSON. |
| First tool call is slow | The server is downloading and parsing a CVM archive. Use `load_dataset` for the exact period you need, and raise the client's tool timeout. `fi_cda` is the heaviest dataset. |
| `run_query` says “no data cached” | Only cached periods are visible to SQL. Call `load_dataset` (e.g. `dataset="dfp", period="2024"`) or a curated tool first. |
| The latest month looks stale | CVM rewrites the current month in place. Call `refresh_cache` for that dataset/period. |
| Disk is filling up | Check `cache_status`; delete `~/.cache/cvm-mcp/parquet` (rebuilt on demand) or move `CVMMCP_CACHE_DIR`. |
| `CVM returned HTTP 5xx` / timeouts | CVM is temporarily unavailable; the server retries 429/500/502/503/504 with backoff. Check connectivity to `dados.cvm.gov.br`. |
| Unknown dataset/table | Call `list_datasets` and `describe_dataset`; ids and table slugs are exact. |

## License

MIT. CVM data is public; see the [CVM open-data portal](https://dados.cvm.gov.br)
for the original terms and update policy.

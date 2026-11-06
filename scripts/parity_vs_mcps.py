"""Data-level parity check: cvm-mcp vs the standalone cvm-dfp-mcp / cvm-fre-mcp.

The two standalone servers are the reference implementation for their domains.
This script builds the *same* slice twice -- once through each server's own
store, once through ``cvm_mcp.store`` -- and compares the frames cell by cell.

    uv run python scripts/parity_vs_mcps.py [--year 2023]

Exit code 1 on any structural or numerical mismatch.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SERVERS = ROOT.parent
for sibling in ("cvm-dfp-mcp/src", "cvm-fre-mcp/src"):
    candidate = SERVERS / sibling
    if candidate.exists():
        sys.path.insert(0, str(candidate))

from cvm_mcp.fetch import Client
from cvm_mcp.store import Store

DFP_COMPANIES = [1023, 4170, 16292]   # BB (bank chart), Vale, and a second filer
DFP_DOCS = ["BPA", "BPP", "DRE"]

FRE_TABLES = [
    "documento",
    "posicao_acionaria",
    "administrador_membro_conselho_fiscal",
    "remuneracao_total_orgao",
    "capital_social",
    "transacao_parte_relacionada",
]

NUMERIC_TOLERANCE = 0.01


def compare(name: str, left: pd.DataFrame, right: pd.DataFrame, keys: list[str]) -> list[str]:
    problems: list[str] = []
    left = left.drop(columns=["period", "_period", "ano"], errors="ignore")
    right = right.drop(columns=["period", "_period", "ano"], errors="ignore")

    if len(left) != len(right):
        problems.append(f"{name}: row count differs (ref={len(left)} mine={len(right)})")
        return problems

    common_keys = [key for key in keys if key in left.columns and key in right.columns]
    common = [column for column in left.columns if column in right.columns]
    if not common:
        return [f"{name}: no common columns"]

    if common_keys:
        left = left.sort_values(common_keys, na_position="first").reset_index(drop=True)
        right = right.sort_values(common_keys, na_position="first").reset_index(drop=True)

    for column in common:
        a, b = left[column], right[column]
        if pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(b):
            delta = (a.astype(float) - b.astype(float)).abs()
            worst = float(np.nanmax(delta)) if delta.notna().any() else 0.0
            if worst > NUMERIC_TOLERANCE:
                problems.append(f"{name}: {column} differs (max {worst:.4f})")
        else:
            a_text = a.astype("string").fillna("<NA>")
            b_text = b.astype("string").fillna("<NA>")
            if not a_text.equals(b_text):
                differing = int((a_text != b_text).sum())
                problems.append(f"{name}: {column} differs in {differing} rows")

    print(f"  {name}: {len(left)} rows, {len(common)} columns compared")
    return problems


async def dfp_reference(year: int, cache: Path) -> dict[str, pd.DataFrame]:
    from cvm_dfp.client import CVMClient
    from cvm_dfp.store import Store as ReferenceStore

    client = CVMClient(cache_dir=cache / "zips")
    store = ReferenceStore(client, root=cache)
    try:
        await store.ensure_many("dfp", [year], DFP_DOCS, ["con"])
    finally:
        await client.aclose()
    return {
        doc: store.query(
            "dfp",
            docs=[doc],
            fmts=["con"],
            first_year=year,
            last_year=year,
            cvm_codes=DFP_COMPANIES,
        )
        for doc in DFP_DOCS
    }


async def dfp_mine(year: int, cache: Path) -> dict[str, pd.DataFrame]:
    client = Client(cache_dir=cache)
    store = Store(client, root=cache)
    try:
        tables = [f"{doc.lower()}_con" for doc in DFP_DOCS]
        await store.ensure_many("dfp", [str(year)], tables)
        frames: dict[str, pd.DataFrame] = {}
        for doc in DFP_DOCS:
            frame = store.read(
                "dfp",
                f"{doc.lower()}_con",
                periods=[str(year)],
                isin={"CD_CVM": DFP_COMPANIES},
            )
            frame["doc"] = doc
            frames[doc] = frame
        return frames
    finally:
        await client.aclose()


async def fre_reference(year: int, cache: Path) -> dict[str, pd.DataFrame]:
    from cvm_fre.client import FREClient
    from cvm_fre.store import Store as ReferenceStore

    client = FREClient(cache_dir=cache / "zips")
    store = ReferenceStore(client, root=cache)
    try:
        frames = {}
        for table in FRE_TABLES:
            await store.ensure_table(year, table)
            frames[table] = store.read_table(year, table)
        return frames
    finally:
        await client.aclose()


async def fre_mine(year: int, cache: Path) -> dict[str, pd.DataFrame]:
    client = Client(cache_dir=cache)
    store = Store(client, root=cache)
    try:
        frames = {}
        for table in FRE_TABLES:
            await store.ensure("fre", table, str(year))
            frames[table] = store.read("fre", table, periods=[str(year)])
        return frames
    finally:
        await client.aclose()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2023)
    parser.add_argument("--workdir", type=Path, default=Path("/tmp/opencode/cvm-parity"))
    args = parser.parse_args()
    year = int(args.year)
    workdir = args.workdir
    workdir.mkdir(parents=True, exist_ok=True)

    print(f"DFP parity — {DFP_COMPANIES} in {year} ({', '.join(DFP_DOCS)})")
    reference = await dfp_reference(year, workdir / "dfp-ref")
    mine = await dfp_mine(year, workdir / "dfp-mine")
    problems: list[str] = []
    for doc in DFP_DOCS:
        problems.extend(
            compare(
                f"DFP/{doc}",
                reference[doc],
                mine[doc],
                keys=["CD_CVM", "CD_CONTA", "DS_CONTA", "DT_REFER"],
            )
        )

    print(f"\nFRE parity — {len(FRE_TABLES)} tables in {year}")
    reference = await fre_reference(year, workdir / "fre-ref")
    mine = await fre_mine(year, workdir / "fre-mine")
    for table in FRE_TABLES:
        problems.extend(
            compare(
                f"FRE/{table}",
                reference[table],
                mine[table],
                keys=["cd_cvm", "cnpj", "data_referencia", "versao", "id_documento"],
            )
        )

    if problems:
        print("\nMISMATCH")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nParity confirmed: cvm-mcp matches cvm-dfp-mcp and cvm-fre-mcp.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

"""Parquet store with one partition per ``(dataset, table, period)``.

Downloading and parsing a CVM archive is expensive (13-270 MB, seconds of CSV
parsing); a naive server would repeat that on every question. Here each slice is
parsed once and written as a single Parquet file::

    <cache>/parquet/v1/<dataset>/<table>/period=<YYYY|YYYYMM|all>/data.parquet

Queries then read only the partitions that match the requested period, and the
DuckDB layer turns every cached table into a view so ad-hoc SQL can cross
datasets without adding a tool per table.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import zipfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from . import fre_normalize, normalize, registry, schema_dfp
from .fetch import Client, PeriodNotFound

CACHE_VERSION = "v1"
REGISTRY_TTL_SECONDS = 24 * 3600


class StoreError(RuntimeError):
    """Base class for store failures."""


class NoData(StoreError):
    """No rows match, or the partition is unavailable."""


#: Legacy uppercase names the company tools (and the standalone servers) use.
REGISTRY_RENAME = {
    "cnpj_cia": "CNPJ_CIA",
    "cd_cvm": "CD_CVM",
    "denom_social": "DENOM_SOCIAL",
    "denom_comerc": "DENOM_COMERC",
    "sit": "SIT",
    "setor_ativ": "SETOR_ATIV",
    "tp_merc": "TP_MERC",
    "categ_reg": "CATEG_REG",
    "controle_acionario": "CONTROLE_ACIONARIO",
    "dt_reg": "DT_REG",
    "dt_cancel": "DT_CANCEL",
    "mun": "MUN",
    "uf": "UF",
}

REGISTRY_COLUMNS = tuple(REGISTRY_RENAME.values())

TICKER_RENAME = {
    "cnpj_companhia": "CNPJ_CIA",
    "nome_empresarial": "DENOM_SOCIAL",
    "valor_mobiliario": "VALOR_MOBILIARIO",
    "codigo_negociacao": "CODIGO_NEGOCIACAO",
    "mercado": "MERCADO",
    "segmento": "SEGMENTO",
    "sigla_classe_acao_preferencial": "CLASSE_ACAO_PREFERENCIAL",
    "data_inicio_negociacao": "DT_INI_NEGOCIACAO",
    "data_fim_negociacao": "DT_FIM_NEGOCIACAO",
}

TICKER_COLUMNS = (
    "CNPJ_CIA",
    "CD_CVM",
    "DENOM_SOCIAL",
    "VALOR_MOBILIARIO",
    "CODIGO_NEGOCIACAO",
    "MERCADO",
    "SEGMENTO",
    "CLASSE_ACAO_PREFERENCIAL",
    "DT_INI_NEGOCIACAO",
    "DT_FIM_NEGOCIACAO",
)

_FORBIDDEN = (
    "attach",
    "detach",
    "copy",
    "install",
    "load",
    "export",
    "import",
    "pragma",
    "create",
    "insert",
    "update",
    "delete",
    "drop",
    "alter",
    "call",
    "set ",
    "reset",
    "read_csv",
    "read_csv_auto",
    "read_parquet",
    "parquet_scan",
    "csv_scan",
    "read_json",
    "glob",
    "httpfs",
    "sqlite_scan",
    "postgres_scan",
    "mysql_scan",
)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))


def _period_key(period: str) -> str:
    return period or "all"


def _read_member(archive: Path, member: str) -> bytes:
    """Read one member from a zip, or the whole file when it is a plain CSV."""
    if archive.suffix.lower() in {".csv", ".txt"}:
        return archive.read_bytes()

    with zipfile.ZipFile(archive) as handle:
        names = {name.lower(): name for name in handle.namelist()}
        match = names.get(member.lower())
        if match is None:
            raise NoData(f"{archive.name}: {member} not found inside the archive")
        with handle.open(match) as stream:
            return stream.read()


class Store:
    def __init__(self, client: Client, root: str | Path | None = None) -> None:
        self.client = client
        self.root = Path(root) if root is not None else client.cache_dir
        self.parquet_root_all = self.root / "parquet"
        self.parquet_root = self.parquet_root_all / CACHE_VERSION
        self.derived_root = self.root / "derived"
        self.parquet_root.mkdir(parents=True, exist_ok=True)
        self.derived_root.mkdir(parents=True, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self._archive_locks: dict[str, asyncio.Lock] = {}
        self.last_errors: dict[tuple[str, str, str], str] = {}
        self._drop_stale_partitions()

    def _drop_stale_partitions(self) -> None:
        try:
            children = list(self.parquet_root_all.iterdir())
        except OSError:
            return
        for child in children:
            if child.is_dir() and child.name != CACHE_VERSION:
                shutil.rmtree(child, ignore_errors=True)

    # -- paths --------------------------------------------------------

    def partition_dir(self, dataset_id: str, table: str, period: str) -> Path:
        return (
            self.parquet_root
            / _slug(dataset_id)
            / _slug(table)
            / f"period={_period_key(period)}"
        )

    def partition_file(self, dataset_id: str, table: str, period: str) -> Path:
        return self.partition_dir(dataset_id, table, period) / "data.parquet"

    def has_partition(self, dataset_id: str, table: str, period: str) -> bool:
        return self.partition_file(dataset_id, table, period).exists()

    def cached_periods(self, dataset_id: str, table: str | None = None) -> list[str]:
        root = self.parquet_root / _slug(dataset_id)
        if table is not None:
            root = root / _slug(table)
        if not root.exists():
            return []
        periods: set[str] = set()
        for path in root.rglob("period=*"):
            value = path.name.split("=", 1)[1]
            periods.add("" if value == "all" else value)
        return sorted(periods)

    def cached_tables(self, dataset_id: str, period: str | None = None) -> list[str]:
        root = self.parquet_root / _slug(dataset_id)
        if not root.exists():
            return []
        tables: set[str] = set()
        for path in root.iterdir():
            if not path.is_dir():
                continue
            if period is None or self.partition_file(dataset_id, path.name, period).exists():
                tables.add(path.name)
        return sorted(tables)

    def cached_years(self, dataset_id: str) -> list[int]:
        years: list[int] = []
        for period in self.cached_periods(dataset_id):
            if len(period) == 4 and period.isdigit():
                years.append(int(period))
        return sorted(set(years))

    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    # -- building -----------------------------------------------------

    async def ensure(
        self,
        dataset_id: str,
        table: str,
        period: str = "",
        *,
        force: bool = False,
    ) -> Path:
        """Download + parse one ``(dataset, table, period)`` slice, once."""
        dataset = registry.get(dataset_id)
        item = dataset.table(table)
        period = "" if dataset.period == "none" else str(period)
        target = self.partition_file(dataset.id, item.slug, period)

        archive = await self._archive(dataset, period)
        if (
            target.exists()
            and self._schema_is_current(item, target)
            and not force
            and target.stat().st_mtime >= archive.stat().st_mtime
        ):
            return target

        if dataset.domain == "companies" and (
            dataset.normalizer is registry.Normalizer.GENERIC
            or (not item.statement and item.cnpj_column)
        ):
            # cd_cvm is attached at build time, so the registry must exist first.
            await self._ensure_registry()

        async with self._lock(f"{dataset.id}|{item.slug}|{period}"):
            if (
                target.exists()
                and self._schema_is_current(item, target)
                and not force
                and target.stat().st_mtime >= archive.stat().st_mtime
            ):
                return target
            await asyncio.to_thread(self._build, dataset, item, period, archive, force)
        return target

    @staticmethod
    def _schema_is_current(item: registry.Table, target: Path) -> bool:
        """False when a cached partition still carries a pre-rename header.

        Partitions built before the RCVM-175 aliases existed keep the old
        column names even though the archive did not change; rebuild them on
        first use instead of forcing a full cache wipe.
        """
        if not item.column_aliases:
            return True
        try:
            columns = set(pq.read_schema(target).names)
        except Exception:  # noqa: BLE001 - an unreadable file is rebuilt anyway
            return True
        return not any(old in columns for old, _ in item.column_aliases)

    async def ensure_period(
        self,
        dataset_id: str,
        period: str = "",
        *,
        tables: Sequence[str] | None = None,
        force: bool = False,
    ) -> dict[str, str]:
        """Build every (or the requested) table for one period."""
        dataset = registry.get(dataset_id)
        names = list(tables) if tables else [item.slug for item in dataset.tables]
        outcome: dict[str, str] = {}
        for name in names:
            try:
                await self.ensure(dataset.id, name, period, force=force)
            except Exception as exc:  # noqa: BLE001 - reported, not raised
                self.last_errors[(dataset.id, name, period)] = str(exc)
                outcome[name] = f"error: {exc}"
            else:
                outcome[name] = "ok"
        return outcome

    async def ensure_many(
        self,
        dataset_id: str,
        periods: Iterable[str],
        tables: Sequence[str] | None = None,
        *,
        force: bool = False,
    ) -> dict[str, str]:
        dataset = registry.get(dataset_id)
        outcome: dict[str, str] = {}
        for period in sorted({str(value) for value in periods}):
            result = await self.ensure_period(dataset.id, period, tables=tables, force=force)
            for name, status in result.items():
                outcome[f"{period}:{name}"] = status
        return outcome

    async def _archive(self, dataset: registry.Dataset, period: str):
        key = f"{dataset.id}|{period}"
        async with self._lock(f"archive|{key}"):
            if dataset.period == "none":
                url = registry.BASE_URL + dataset.url
            else:
                url = dataset.url_for(period)
            try:
                return await self.client.ensure(url, refresh_hours=dataset.refresh_hours)
            except Exception as exc:
                raise PeriodNotFound(
                    f"dataset {dataset.id!r} has no data for period {period!r}: {exc}"
                ) from exc

    def _build(
        self,
        dataset: registry.Dataset,
        item: registry.Table,
        period: str,
        archive: Path,
        force: bool,
    ) -> None:
        member = item.member_name(period=period)
        payload = None
        for name in item.member_names(period=period):
            try:
                payload = _read_member(archive, name)
            except NoData:
                continue
            break
        if payload is None:
            raise NoData(f"{archive.name}: {member} not found inside the archive")

        if dataset.normalizer is registry.Normalizer.DFP and item.statement:
            frame = schema_dfp.read_statement_csv(io.BytesIO(payload), clean=True)
            table = pa.Table.from_pandas(frame, schema=schema_dfp.ARROW_SCHEMA, preserve_index=False)
            doc, _, fmt = item.title.partition("/")
            table = table.append_column("doc", pa.array([doc] * table.num_rows, pa.string()))
            table = table.append_column("fmt", pa.array([fmt] * table.num_rows, pa.string()))
            table = table.append_column(
                "_period", pa.array([_period_key(period)] * table.num_rows, pa.string())
            )
            self._write(dataset.id, item.slug, period, table.to_pandas())
            return

        if dataset.normalizer is registry.Normalizer.FRE:
            frame = fre_normalize.canonicalize(fre_normalize.read_csv_bytes(payload))
            if item.slug != "documento":
                frame = fre_normalize.attach_cd_cvm(
                    frame, self._fre_master(dataset, period, archive)
                )
            frame["_period"] = _period_key(period)
            self._write(dataset.id, item.slug, period, frame)
            return

        frame = normalize.read_csv_bytes(payload, sep=dataset.sep, encoding=dataset.encoding)
        if item.column_aliases:
            # Map pre-RCVM-175 headers before canonicalize() orders the keys.
            frame.columns = normalize.clean_names(frame.columns)
            frame = normalize.apply_aliases(frame, item.column_aliases)
        frame = normalize.canonicalize(frame, key_columns=item.key_columns)
        if dataset.domain == "companies" and item.cnpj_column and "cd_cvm" not in frame.columns:
            company = self._registry_sync()
            if not company.empty:
                frame = normalize.attach_cd_cvm(
                    frame, company, cnpj_column=item.cnpj_column
                )
        if item.cnpj_column in frame.columns and "cnpj" not in frame.columns:
            # Normalized 14-digit CNPJ so joins and filters never depend on the
            # punctuation CVM happens to use in that file.
            frame["cnpj"] = normalize.cnpj_series(frame[item.cnpj_column])
        frame["_period"] = _period_key(period)
        self._write(dataset.id, item.slug, period, frame)

    def _fre_master(
        self, dataset: registry.Dataset, period: str, archive: Path
    ) -> pd.DataFrame:
        """The FRE document header, needed to attach ``cd_cvm`` to detail tables.

        Built on demand so a detail table can be requested without the caller
        having to know about the header.
        """
        path = self.partition_file(dataset.id, "documento", period)
        if not path.exists():
            master = dataset.table("documento")
            payload = _read_member(archive, master.member_name(period=period))
            frame = fre_normalize.canonicalize(fre_normalize.read_csv_bytes(payload))
            frame["_period"] = _period_key(period)
            self._write(dataset.id, "documento", period, frame)
        return pd.read_parquet(path)

    def _write(self, dataset_id: str, table: str, period: str, frame: pd.DataFrame) -> None:
        if frame.empty:
            raise NoData(f"{dataset_id}/{table}/{period or 'all'} parsed to zero rows")
        directory = self.partition_dir(dataset_id, table, period)
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / "data.parquet.tmp"
        frame.to_parquet(temporary, index=False, compression="zstd")
        os.replace(temporary, directory / "data.parquet")

    async def refresh(
        self, dataset_id: str, table: str | None = None, period: str | None = None
    ) -> dict[str, str]:
        """Force a rebuild of cached partitions."""
        dataset = registry.get(dataset_id)
        tables = [table] if table else (
            self.cached_tables(dataset_id)
            or [item.slug for item in dataset.tables if item.default]
        )
        periods = (
            [period]
            if period is not None
            else (self.cached_periods(dataset.id) or ([dataset.min_period] if dataset.min_period else [""]))
        )
        return await self.ensure_many(dataset.id, periods, tables, force=True)

    # -- reading ------------------------------------------------------

    def _periods_to_read(self, dataset_id: str, table: str, periods: Sequence[str] | None) -> list[str]:
        available = self.cached_periods(dataset_id, table)
        if not available:
            raise NoData(
                f"no {dataset_id}/{table} data cached yet; "
                "call the data tool first or refresh_cache"
            )
        if periods is None:
            return available
        selected: list[str] = []
        for period in periods:
            value = str(period)
            if value in ("", "all"):
                if "" in available:
                    selected.append("")
            elif value in available:
                selected.append(value)
        if not selected:
            raise NoData(
                f"none of the requested periods are cached for {dataset_id}/{table}. "
                f"Cached: {', '.join(available)}"
            )
        return selected

    def read(
        self,
        dataset_id: str,
        table: str,
        *,
        periods: Sequence[str] | None = None,
        equals: dict[str, Any] | None = None,
        isin: dict[str, Sequence[Any]] | None = None,
        columns: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """Read cached partitions, filtered in memory."""
        dataset = registry.get(dataset_id)
        item = dataset.table(table)
        selected = self._periods_to_read(dataset.id, item.slug, periods)

        frames: list[pd.DataFrame] = []
        for period in selected:
            frame = pd.read_parquet(self.partition_file(dataset.id, item.slug, period))
            if columns is not None:
                keep = [column for column in columns if column in frame.columns]
                if keep:
                    frame = frame.loc[:, keep]
            frames.append(frame)
        result = pd.concat(frames, ignore_index=True)

        for column, value in (equals or {}).items():
            if column not in result.columns:
                raise NoData(f"column {column!r} is not available in {dataset_id}/{item.slug}")
            result = result.loc[result[column] == value]
        for column, values in (isin or {}).items():
            if column not in result.columns:
                raise NoData(f"column {column!r} is not available in {dataset_id}/{item.slug}")
            result = result.loc[result[column].isin(list(values))]

        if result.empty:
            raise NoData("no rows match the requested filters")
        if limit is not None and len(result) > limit:
            result = result.head(limit)
        return result.reset_index(drop=True)

    # -- company registry ---------------------------------------------

    async def _ensure_registry(self) -> Path:
        return await self.ensure("cad_cia_aberta", "companhias")

    def _registry_sync(self) -> pd.DataFrame:
        path = self.partition_file("cad_cia_aberta", "companhias", "")
        if not path.exists():
            return pd.DataFrame()
        return self._prepare_registry(pd.read_parquet(path))

    @staticmethod
    def _prepare_registry(frame: pd.DataFrame) -> pd.DataFrame:
        frame = frame.rename(columns=REGISTRY_RENAME)
        for column in REGISTRY_COLUMNS:
            if column not in frame.columns:
                frame[column] = pd.NA
        frame = frame.copy()
        frame["CD_CVM"] = pd.to_numeric(frame["CD_CVM"], errors="coerce").astype("Int64")
        frame["CNPJ_CIA"] = frame["CNPJ_CIA"].astype("string").str.strip()
        frame = frame.dropna(subset=["CD_CVM"])
        return frame.drop_duplicates(subset=["CD_CVM", "DENOM_SOCIAL"]).reset_index(drop=True)

    async def registry(self, *, refresh: bool = False) -> pd.DataFrame:
        """Company registry, with the legacy uppercase column names."""
        path = await self._ensure_registry()
        frame = await asyncio.to_thread(pd.read_parquet, path)
        return self._prepare_registry(frame)

    async def tickers(self, year: int, *, refresh: bool = False) -> pd.DataFrame:
        """Ticker list from the FCA archive of one year."""
        path = await self.ensure("fca", "valor_mobiliario", str(year), force=refresh)
        frame = await asyncio.to_thread(pd.read_parquet, path)
        frame = frame.rename(columns=TICKER_RENAME)
        if "CNPJ_CIA" not in frame.columns:
            raise NoData(f"FCA {year}: no CNPJ column")
        frame["CNPJ_CIA"] = frame["CNPJ_CIA"].astype("string").str.strip()
        frame = frame.loc[:, [column for column in frame.columns if column != "cd_cvm"]]

        company = await self.registry()
        mapping = company.loc[:, ["CNPJ_CIA", "CD_CVM"]].drop_duplicates("CNPJ_CIA")
        frame = frame.merge(mapping, on="CNPJ_CIA", how="left")
        keep = [column for column in TICKER_COLUMNS if column in frame.columns]
        return frame.loc[:, keep].drop_duplicates().reset_index(drop=True)

    # -- ad-hoc SQL ---------------------------------------------------

    def views(self) -> dict[str, list[str]]:
        """``{view_name: [parquet paths]}`` for every cached table."""
        found: dict[str, list[str]] = {}
        if not self.parquet_root.exists():
            return found
        for dataset_dir in self.parquet_root.iterdir():
            if not dataset_dir.is_dir():
                continue
            for table_dir in dataset_dir.iterdir():
                if not table_dir.is_dir():
                    continue
                files = sorted(str(path) for path in table_dir.rglob("data.parquet"))
                if files:
                    found[f"{dataset_dir.name}__{table_dir.name}"] = files
        return found

    def query(self, sql: str, *, limit: int = 5000) -> pd.DataFrame:
        """Run a read-only SQL query over the cached tables."""
        statement = _sanitize(sql)
        views = self.views()
        if not views:
            raise NoData("no data cached yet; call a data tool or refresh_cache first")

        connection = duckdb.connect(":memory:")
        try:
            for name, files in views.items():
                connection.execute(
                    f'CREATE VIEW "{name}" AS SELECT * FROM read_parquet({_literal(files)})'
                )
            wrapped = f"SELECT * FROM ({statement}) AS _q LIMIT {int(limit) + 1}"
            frame = connection.execute(wrapped).fetch_df()
        finally:
            connection.close()

        if len(frame) > limit:
            raise StoreError(
                f"query returns more than {limit} rows; add filters or a lower LIMIT"
            )
        return frame

    # -- cache stats --------------------------------------------------

    def cache_status(self) -> dict[str, Any]:
        datasets: dict[str, Any] = {}
        total_bytes = 0
        total_files = 0
        for dataset in registry.all_datasets():
            root = self.parquet_root / _slug(dataset.id)
            if not root.exists():
                continue
            tables = self.cached_tables(dataset.id)
            periods = self.cached_periods(dataset.id)
            size = sum(path.stat().st_size for path in root.rglob("*.parquet"))
            files = sum(1 for _ in root.rglob("*.parquet"))
            total_bytes += size
            total_files += files
            datasets[dataset.id] = {
                "tables": tables,
                "periods": periods,
                "partitions": files,
                "bytes": size,
            }
        text_root = self.parquet_root / "doc_text"
        if text_root.exists():
            documents = sorted(
                {
                    path.parent.name
                    for path in text_root.rglob("data.parquet")
                }
            )
            size = sum(path.stat().st_size for path in text_root.rglob("*.parquet"))
            files = sum(1 for _ in text_root.rglob("*.parquet"))
            total_bytes += size
            total_files += files
            datasets["doc_text"] = {
                "tables": sorted(
                    {path.parent.parent.parent.name for path in text_root.rglob("data.parquet")}
                ),
                "periods": [],
                "documents": documents,
                "partitions": files,
                "bytes": size,
            }
        return {
            "cache_dir": str(self.root),
            "datasets_cached": len(datasets),
            "partitions": total_files,
            "bytes": total_bytes,
            "datasets": datasets,
        }


def _literal(values: Sequence[str]) -> str:
    """Render a Python list of paths as a DuckDB list literal."""
    escaped = ", ".join("'" + str(value).replace("'", "''") + "'" for value in values)
    return f"[{escaped}]"


def _sanitize(sql: str) -> str:
    """Reject anything that is not a single read-only SELECT/WITH."""
    text = str(sql).strip().rstrip(";").strip()
    if not text:
        raise StoreError("query is empty")
    if ";" in text:
        raise StoreError("only a single statement is allowed")

    lowered = re.sub(r"\s+", " ", re.sub(r"--[^\n]*|/\*.*?\*/", " ", text, flags=re.DOTALL)).lower()
    if not lowered.startswith(("select", "with")):
        raise StoreError("only SELECT / WITH queries are allowed")
    for token in _FORBIDDEN:
        if re.search(rf"(?<![a-z0-9_]){re.escape(token)}", lowered):
            raise StoreError(f"forbidden keyword in query: {token.strip()}")
    return text

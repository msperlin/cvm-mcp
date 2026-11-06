"""Meta tools: discover the datasets, inspect the cache, run ad-hoc SQL."""

from __future__ import annotations

import re
import time
from typing import Annotated, Any

import pyarrow.parquet as pq
from pydantic import Field

from . import data_dictionary, registry
from .app import mcp
from .fetch import describe_error
from .formatting import to_csv
from .runtime import store
from .toolkit import READ_ONLY, error, json_out

#: Safety valve for ``load_dataset``: one call should not start a multi-hour
#: build of the whole history.
MAX_BUILD_PARTITIONS = 300


@mcp.tool(annotations=READ_ONLY)
def list_datasets(
    domain: Annotated[
        str | None,
        Field(
            description="Optional domain filter: companies, funds, offerings, enforcement, registry."
        ),
    ] = None,
    detail: Annotated[
        bool, Field(description="Include the table list of each dataset.")
    ] = False,
) -> str:
    """List every CVM dataset this server understands.

    This is the entry point: each dataset has an `id`, a period granularity and
    one or more tables. Table ids are what `describe_dataset` and `run_query`
    use; SQL views are named `<dataset>__<table>`.
    """
    wanted = str(domain).strip().lower() if domain else None
    grouped: dict[str, list[dict[str, Any]]] = {}
    for dataset in registry.all_datasets():
        if wanted and dataset.domain != wanted:
            continue
        record: dict[str, Any] = {
            "id": dataset.id,
            "domain": dataset.domain,
            "title": dataset.title,
            "description": dataset.description,
            "layout": dataset.layout.value,
            "period": dataset.period,
            "min_period": dataset.min_period or None,
            "heavy": dataset.heavy,
            "tables": [table.slug for table in dataset.tables],
            "views": [f"{dataset.id}__{table.slug}" for table in dataset.tables],
        }
        if detail:
            record["table_details"] = [
                {
                    "table": table.slug,
                    "title": table.title,
                    "key_columns": list(table.key_columns),
                }
                for table in dataset.tables
            ]
            if dataset.notes:
                record["notes"] = dataset.notes
        grouped.setdefault(dataset.domain, []).append(record)

    if not grouped:
        return error(
            f"no dataset in domain {domain!r}",
            hint=f"Domains: {', '.join(sorted(registry.domains()))}",
        )
    return json_out(
        {
            "domains": grouped,
            "count": sum(len(items) for items in grouped.values()),
            "tables": registry.table_count(),
        }
    )


@mcp.tool(annotations=READ_ONLY)
def describe_dataset(
    dataset: Annotated[
        str,
        Field(
            description="Dataset id from list_datasets, e.g. 'dfp', 'fi_inf_diario'."
        ),
    ],
    table: Annotated[
        str | None, Field(description="Optional table inside the dataset.")
    ] = None,
    detail: Annotated[
        bool,
        Field(
            description=(
                "Include curated column meanings (description, unit, caveats). "
                "Use this before writing run_query SQL."
            )
        ),
    ] = False,
) -> str:
    """Show the tables, cached periods and columns of one dataset.

    Columns come from the local cache; if a table was never queried, `columns`
    is empty — call the corresponding data tool once to populate it. With
    `detail=True`, `column_details` explains units and pitfalls (e.g.
    `VL_CONTA` vs `VL_CONTA_BRL`, or FRE percentages).
    """
    try:
        spec = registry.get(dataset)
        tables = [spec.table(table)] if table else list(spec.tables)
    except KeyError as exc:
        return error(exc.args[0] if exc.args else str(exc))

    cached = store.cached_periods(spec.id)
    payload: dict[str, Any] = {
        "id": spec.id,
        "title": spec.title,
        "domain": spec.domain,
        "layout": spec.layout.value,
        "period": spec.period,
        "min_period": spec.min_period or None,
        "cached_periods": cached,
        "tables": [],
    }
    for item in tables:
        path = None
        for period in cached:
            candidate = store.partition_file(spec.id, item.slug, period)
            if candidate.exists():
                path = candidate
                break
        columns = list(pq.read_schema(path).names) if path else []
        entry: dict[str, Any] = {
            "table": item.slug,
            "title": item.title,
            "key_columns": list(item.key_columns),
            "columns": columns,
            "cached_periods": store.cached_periods(spec.id, item.slug),
        }
        if detail:
            described = data_dictionary.describe_columns(
                spec.id, item.slug, columns or None
            )
            entry["column_details"] = described
            if columns and len(described) < len(columns):
                entry["column_details_note"] = (
                    f"{len(described)} of {len(columns)} columns have a curated "
                    "description; the others are self-describing CVM fields"
                )
        payload["tables"].append(entry)
    if not cached:
        payload["hint"] = (
            "Nothing cached yet: call load_dataset (recommended) or a data tool "
            "to download and parse a period."
        )
    return json_out(payload)


@mcp.tool(annotations=READ_ONLY)
def cache_status() -> str:
    """What is already downloaded and parsed locally, per dataset."""
    return json_out(store.cache_status())


def _period_request(value: str | None) -> list[str] | None:
    """Parse a period argument: ``None`` = latest, ``['*']`` = every period."""
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"", "latest", "last", "current"}:
        return None
    if text in {"*", "all"}:
        return ["*"]
    return [part for part in re.split(r"[,;\s]+", str(value)) if part]


@mcp.tool(annotations=READ_ONLY)
async def load_dataset(
    dataset: Annotated[
        str,
        Field(
            description="Dataset id from list_datasets, e.g. 'dfp', 'fi_inf_diario'."
        ),
    ],
    table: Annotated[
        str | None,
        Field(description="Optional table; omit for all tables in the dataset."),
    ] = None,
    period: Annotated[
        str | None,
        Field(
            description=(
                "Year (YYYY) or month (YYYYMM); comma-separated values are accepted. "
                "Defaults to the latest published period. Use '*' for every period."
            )
        ),
    ] = None,
) -> str:
    """Download and parse a dataset/period into the local cache.

    This is the non-destructive counterpart of `refresh_cache`: it reuses
    archives that are still fresh and only builds the partitions that are
    missing. Call it before `run_query` so the SQL layer has the tables (and
    periods) it needs, and use `describe_dataset(detail=True)` for column
    meanings.
    """
    try:
        spec = registry.get(dataset)
        item = spec.table(table) if table else None
    except KeyError as exc:
        return error(exc.args[0] if exc.args else str(exc))

    requested = _period_request(period)
    tables = [item.slug] if item else [table_spec.slug for table_spec in spec.tables]
    if spec.period == "none":
        periods = [""]
    else:
        try:
            available = await store.client.periods(spec)
        except Exception as exc:  # noqa: BLE001
            return error(
                describe_error(exc), hint="Could not list the published periods."
            )

        if not available:
            return error(f"no periods published for {spec.id}")
        if requested == ["*"]:
            periods = list(available)
        elif requested is None:
            periods = [available[-1]]
        else:
            missing = [value for value in requested if value not in available]
            if missing:
                return error(
                    f"period(s) not published for {spec.id}: {', '.join(missing)}",
                    hint=f"Available: {available[0]}..{available[-1]}",
                    available=available[-12:],
                )
            periods = list(dict.fromkeys(requested))

        planned = len(periods) * len(tables)
        if spec.heavy and len(periods) > 3:
            return error(
                f"refusing to build {len(periods)} periods of the heavy dataset {spec.id!r}",
                hint="Pass at most three explicit periods per call.",
            )
        if planned > MAX_BUILD_PARTITIONS:
            return error(
                f"refusing to build {planned} partitions in one call",
                hint=(
                    "Narrow the periods or the table, then call load_dataset again. "
                    f"Limit: {MAX_BUILD_PARTITIONS}."
                ),
            )

    started = time.perf_counter()
    before = {
        (target, name): store.partition_file(spec.id, name, target).exists()
        for target in periods
        for name in tables
    }
    failed: dict[str, str] = {}
    for target in periods:
        outcome = await store.ensure_period(spec.id, target, tables=tables)
        for name, status in outcome.items():
            if status != "ok":
                key = f"{target}:{name}" if target else name
                failed[key] = status

    partitions: list[dict[str, Any]] = []
    built = 0
    for target in periods:
        for name in tables:
            path = store.partition_file(spec.id, name, target)
            if not path.exists():
                continue
            if not before[(target, name)]:
                built += 1
            try:
                rows = pq.ParquetFile(path).metadata.num_rows
            except OSError:
                rows = None
            partitions.append(
                {
                    "period": target or "all",
                    "table": name,
                    "rows": rows,
                    "bytes": path.stat().st_size,
                }
            )

    payload: dict[str, Any] = {
        "dataset": spec.id,
        "periods": [target or "all" for target in periods],
        "tables": tables,
        "status": "ok" if not failed else ("partial" if partitions else "error"),
        "ready": len(partitions),
        "built": built,
        "reused": len(partitions) - built,
        "failed": failed,
        "elapsed_seconds": round(time.perf_counter() - started, 3),
    }
    if len(partitions) <= 40:
        payload["partitions"] = partitions
    else:
        payload["totals"] = {
            "partitions": len(partitions),
            "rows": sum(entry["rows"] or 0 for entry in partitions),
            "bytes": sum(entry["bytes"] for entry in partitions),
        }
    return json_out(payload)


@mcp.tool(annotations=READ_ONLY)
async def refresh_cache(
    dataset: Annotated[str, Field(description="Dataset id from list_datasets.")],
    table: Annotated[
        str | None, Field(description="Optional table; omit for all tables.")
    ] = None,
    period: Annotated[
        str | None,
        Field(
            description="Year (YYYY) or month (YYYYMM). Omit to refresh everything cached."
        ),
    ] = None,
) -> str:
    """Force a re-download and rebuild of cached partitions.

    CVM rewrites the current period in place, so use this when the latest month
    looks stale.
    """
    try:
        spec = registry.get(dataset)
        outcome = await store.refresh(spec.id, table, period)
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))
    failures = {key: value for key, value in outcome.items() if value != "ok"}
    return json_out(
        {
            "dataset": spec.id,
            "rebuilt": len(outcome) - len(failures),
            "failed": failures,
        }
    )


@mcp.tool(annotations=READ_ONLY)
def run_query(
    sql: Annotated[
        str,
        Field(
            description=(
                "A single read-only SELECT/WITH query. Views are named "
                "<dataset>__<table>, e.g. dfp__dre_con, fi_inf_diario__informe, "
                "vlmo__transacoes. Only cached periods exist."
            )
        ),
    ],
    limit: Annotated[
        int, Field(description="Maximum rows returned.", ge=1, le=20_000)
    ] = 2000,
) -> str:
    """Run SQL across cached CVM tables (DuckDB engine).

    Use it for questions that combine datasets, for aggregates the curated tools
    do not expose, and to inspect a table before writing a longer query. Only
    SELECT/WITH is allowed; the result is capped by `limit`.
    """
    views = store.views()
    if not views:
        return error(
            "no data cached yet",
            hint="Call a curated tool (e.g. get_fund_nav) or refresh_cache first.",
        )
    try:
        frame = store.query(sql, limit=int(limit))
    except Exception as exc:  # noqa: BLE001
        return error(
            str(exc),
            hint="Call list_datasets to see the available views and describe_dataset for columns.",
            views=sorted(views)[:40],
        )
    body = to_csv(frame)
    header = f"# rows={len(frame)} | source: CVM (DuckDB over local cache)\n"
    return header + body

"""Meta tools: discover the datasets, inspect the cache, run ad-hoc SQL."""

from __future__ import annotations

from typing import Annotated, Any

import pyarrow.parquet as pq
from pydantic import Field

from . import registry
from .app import mcp
from .fetch import describe_error
from .formatting import to_csv
from .runtime import store
from .toolkit import READ_ONLY, error, json_out


@mcp.tool(annotations=READ_ONLY)
def list_datasets(
    domain: Annotated[
        str | None,
        Field(description="Optional domain filter: companies, funds, offerings, enforcement, registry."),
    ] = None,
    detail: Annotated[bool, Field(description="Include the table list of each dataset.")] = False,
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
    dataset: Annotated[str, Field(description="Dataset id from list_datasets, e.g. 'dfp', 'fi_inf_diario'.")],
    table: Annotated[str | None, Field(description="Optional table inside the dataset.")] = None,
) -> str:
    """Show the tables, cached periods and columns of one dataset.

    Columns come from the local cache; if a table was never queried, `columns`
    is empty — call the corresponding data tool once to populate it.
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
        payload["tables"].append(
            {
                "table": item.slug,
                "title": item.title,
                "key_columns": list(item.key_columns),
                "columns": columns,
                "cached_periods": store.cached_periods(spec.id, item.slug),
            }
        )
    if not cached:
        payload["hint"] = "Nothing cached yet: call a data tool to download and parse a period."
    return json_out(payload)


@mcp.tool(annotations=READ_ONLY)
def cache_status() -> str:
    """What is already downloaded and parsed locally, per dataset."""
    return json_out(store.cache_status())


@mcp.tool(annotations=READ_ONLY)
async def refresh_cache(
    dataset: Annotated[str, Field(description="Dataset id from list_datasets.")],
    table: Annotated[str | None, Field(description="Optional table; omit for all tables.")] = None,
    period: Annotated[
        str | None,
        Field(description="Year (YYYY) or month (YYYYMM). Omit to refresh everything cached."),
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
    limit: Annotated[int, Field(description="Maximum rows returned.", ge=1, le=20_000)] = 2000,
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

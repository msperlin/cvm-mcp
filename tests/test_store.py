"""Store behaviour that does not need the network."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from cvm_mcp import registry
from cvm_mcp.fetch import Client
from cvm_mcp.store import Store, StoreError, _literal, _sanitize


def make_store(tmp_path) -> Store:
    return Store(Client(cache_dir=tmp_path), root=tmp_path)


def test_partition_layout(tmp_path):
    store = make_store(tmp_path)
    path = store.partition_file("dfp", "dre_con", "2023")
    assert path.parent.name == "period=2023"
    assert path.parent.parent.name == "dre_con"
    assert path.parent.parent.parent.name == "dfp"


def test_periodless_dataset_uses_all(tmp_path):
    store = make_store(tmp_path)
    path = store.partition_file("oferta", "distribuicao", "")
    assert path.parent.name == "period=all"


def test_cache_status_is_empty_on_a_fresh_cache(tmp_path):
    status = make_store(tmp_path).cache_status()
    assert status["partitions"] == 0
    assert status["datasets"] == {}


def test_sanitize_accepts_select_and_with():
    assert _sanitize("SELECT * FROM dfp__dre_con") == "SELECT * FROM dfp__dre_con"
    assert (
        _sanitize("WITH x AS (SELECT 1) SELECT * FROM x;")
        == "WITH x AS (SELECT 1) SELECT * FROM x"
    )


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE x",
        "ATTACH 'x.db'",
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT 1; SELECT 2",
        "INSTALL httpfs",
        "COPY (SELECT 1) TO 'x.csv'",
        "PRAGMA database_list",
    ],
)
def test_sanitize_rejects_dangerous_sql(sql):
    with pytest.raises(StoreError):
        _sanitize(sql)


def test_literal_quotes_paths():
    assert _literal(["/a/b.parquet"]) == "['/a/b.parquet']"
    assert _literal(["a'b"]) == "['a''b']"


def test_query_without_cache_raises(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(Exception) as excinfo:
        store.query("SELECT 1")
    assert "no data cached" in str(excinfo.value)


def test_304_does_not_rebuild_a_partition(tmp_path, monkeypatch):
    """A revalidated-but-unchanged archive must not trigger a re-parse."""
    dataset = registry.Dataset(
        id="demo",
        domain="demo",
        title="Demo",
        description="",
        layout=registry.Layout.SINGLE_ARCHIVE,
        normalizer=registry.Normalizer.GENERIC,
        url="DEMO/DADOS/demo.csv",
        tables=(registry.Table(slug="rows", member="demo.csv", title="Rows"),),
        period="none",
        min_period="",
        refresh_hours=1e-9,
    )
    monkeypatch.setattr(registry, "get", lambda _: dataset)

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            body = b"id;name\n1;alpha\n2;beta\n" + (b"0;padding\n" * 200)
            return httpx.Response(200, content=body)
        return httpx.Response(304)

    store = Store(
        Client(cache_dir=tmp_path, transport=httpx.MockTransport(handler)),
        root=tmp_path,
    )

    first = asyncio.run(store.ensure("demo", "rows"))
    first_mtime = first.stat().st_mtime
    time.sleep(0.02)
    # The refresh window is microseconds, so this call revalidates (304).
    second = asyncio.run(store.ensure("demo", "rows"))

    assert second == first
    assert first.stat().st_mtime == first_mtime
    assert len(calls) == 2

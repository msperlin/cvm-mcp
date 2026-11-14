"""Store behaviour that does not need the network."""

from __future__ import annotations

import asyncio
import time
import zipfile

import httpx
import pandas as pd
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


def test_flat_dfp_member_bypasses_the_statement_schema(tmp_path):
    """The filing index is plain CVM CSV: no doc/fmt columns, snake_case."""
    store = make_store(tmp_path)
    archive = tmp_path / "dfp_cia_aberta_2024.zip"
    index = (
        "CNPJ_CIA;DT_REFER;VERSAO;DENOM_CIA;CD_CVM;CATEG_DOC;ID_DOC;DT_RECEB;LINK_DOC\n"
        "02.635.522/0001-95;2024-03-31;1;JALLES MACHADO S.A.;025496;DFP;159112;"
        "2024-06-16;http://example.test/filing\n"
    )
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("dfp_cia_aberta_2024.csv", index.encode("latin1"))

    dataset = registry.get("dfp")
    store._build(dataset, dataset.table("documento"), "2024", archive, False)

    frame = pd.read_parquet(store.partition_file("dfp", "documento", "2024"))
    assert "link_doc" in frame.columns
    assert frame.loc[0, "link_doc"] == "http://example.test/filing"
    assert frame.loc[0, "cnpj"] == "02635522000195"
    assert "doc" not in frame.columns and "fmt" not in frame.columns
    assert frame.loc[0, "_period"] == "2024"


def test_pre_rcvm_175_daily_report_is_readable(tmp_path):
    """2021-01..2023-11 files use CNPJ_FUNDO/TP_FUNDO; they must land on the
    current schema so the same query works across the 2023-12 rename."""
    store = make_store(tmp_path)
    archive = tmp_path / "inf_diario_fi_202311.zip"
    body = (
        "TP_FUNDO;CNPJ_FUNDO;DT_COMPTC;VL_TOTAL;VL_QUOTA;VL_PATRIM_LIQ;"
        "CAPTC_DIA;RESG_DIA;NR_COTST\n"
        "FI;07593972000186;2023-11-30;1000.00;1.234567;900.00;10.00;5.00;42\n"
    )
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("inf_diario_fi_202311.csv", body.encode("latin1"))

    dataset = registry.get("fi_inf_diario")
    store._build(dataset, dataset.table("informe"), "202311", archive, False)

    frame = pd.read_parquet(store.partition_file("fi_inf_diario", "informe", "202311"))
    assert "cnpj_fundo_classe" in frame.columns
    assert "tp_fundo_classe" in frame.columns
    assert "cnpj_fundo" not in frame.columns
    assert frame.loc[0, "cnpj"] == "07593972000186"
    assert frame.loc[0, "tp_fundo_classe"] == "FI"


def test_partition_built_with_pre_rename_headers_is_not_reused(tmp_path):
    store = make_store(tmp_path)
    item = registry.get("fi_inf_diario").table("informe")
    path = store.partition_file("fi_inf_diario", "informe", "202311")
    path.parent.mkdir(parents=True)

    pd.DataFrame({"cnpj_fundo": ["x"], "tp_fundo": ["FI"]}).to_parquet(path)
    assert store._schema_is_current(item, path) is False

    pd.DataFrame({"cnpj_fundo_classe": ["x"], "tp_fundo_classe": ["FI"]}).to_parquet(path)
    assert store._schema_is_current(item, path) is True


def test_cda_structured_portfolio_falls_back_to_the_fiim_member(tmp_path):
    """Archives before 2023-12 carry cda_fiim_<period>.csv, not cda_fie."""
    store = make_store(tmp_path)
    archive = tmp_path / "cda_fi_202306.zip"
    body = (
        "TP_FUNDO;CNPJ_FUNDO;DENOM_SOCIAL;DT_COMPTC;VL_PATRIM_LIQ\n"
        "FII;12345678000199;FUNDO TESTE;2023-06-30;500.00\n"
    )
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("cda_fiim_202306.csv", body.encode("latin1"))

    dataset = registry.get("fi_cda")
    store._build(dataset, dataset.table("fie"), "202306", archive, False)

    frame = pd.read_parquet(store.partition_file("fi_cda", "fie", "202306"))
    assert frame.loc[0, "cnpj"] == "12345678000199"
    assert frame.loc[0, "tp_fundo_classe"] == "FII"


def test_dfp_parecer_table_is_optional(tmp_path):
    spec = registry.get("dfp")
    assert spec.table("parecer").default is False
    assert spec.table("composicao_capital").default is True


def test_query_unions_columns_from_different_periods(tmp_path):
    """A column introduced mid-series must not be dropped by the view."""
    store = make_store(tmp_path)
    old = store.partition_file("fi_inf_diario", "informe", "202311")
    new = store.partition_file("fi_inf_diario", "informe", "202312")
    for path in (old, new):
        path.parent.mkdir(parents=True)
    pd.DataFrame(
        {"cnpj": ["a"], "dt_comptc": ["2023-11-30"], "_period": ["202311"]}
    ).to_parquet(old)
    pd.DataFrame(
        {
            "cnpj": ["a"],
            "dt_comptc": ["2023-12-01"],
            "id_subclasse": ["1"],
            "_period": ["202312"],
        }
    ).to_parquet(new)

    frame = store.query(
        "SELECT _period, id_subclasse FROM fi_inf_diario__informe ORDER BY _period"
    )
    assert frame["id_subclasse"].tolist()[0] is None or pd.isna(frame["id_subclasse"][0])
    assert frame["id_subclasse"].tolist()[1] == "1"


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

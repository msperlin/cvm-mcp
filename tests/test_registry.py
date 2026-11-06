"""Registry integrity: ids, aliases, URL templates and table members."""

from __future__ import annotations

import re

import pytest

from cvm_mcp import registry


def test_ids_are_unique_and_resolvable():
    ids = [dataset.id for dataset in registry.all_datasets()]
    assert len(ids) == len(set(ids))
    for dataset in registry.all_datasets():
        assert registry.get(dataset.id) is dataset
        for alias in dataset.aliases:
            assert registry.get(alias) is dataset


def test_every_dataset_declares_tables():
    for dataset in registry.all_datasets():
        assert dataset.tables, f"{dataset.id} has no tables"
        slugs = [table.slug for table in dataset.tables]
        assert len(slugs) == len(set(slugs)), f"{dataset.id} has duplicate table slugs"


def test_url_matches_period_granularity():
    for dataset in registry.all_datasets():
        has_placeholder = "{period}" in dataset.url
        if dataset.period == "none":
            assert not has_placeholder, f"{dataset.id} is period-less but has {{period}}"
        else:
            assert has_placeholder, f"{dataset.id} needs {{period}} in its URL"


def test_table_members_are_renderable():
    for dataset in registry.all_datasets():
        for table in dataset.tables:
            period = "202401" if dataset.period == "month" else "2024"
            if dataset.period == "none":
                period = ""
            name = table.member_name(period=period)
            assert isinstance(name, str) and name


def test_period_regex_matches_the_published_filenames():
    from cvm_mcp.fetch import period_regex

    for dataset in registry.all_datasets():
        if dataset.period == "none":
            continue
        filename = dataset.url.rsplit("/", 1)[-1]
        sample = "202401" if dataset.period == "month" else "2024"
        regex = period_regex(filename)
        match = regex.match(filename.replace("{period}", sample))
        assert match and match.group("period") == sample


def test_expected_coverage():
    ids = {dataset.id for dataset in registry.all_datasets()}
    for expected in (
        "dfp",
        "itr",
        "fre",
        "vlmo",
        "cgvn",
        "fi_inf_diario",
        "fi_cda",
        "fi_cad",
        "fii_inf_mensal",
        "fidc_inf_mensal",
        "fiagro_inf_mensal",
        "oferta",
    ):
        assert expected in ids
    assert registry.table_count() >= 100


def test_structured_fund_datasets_cover_their_tables():
    assert {table.slug for table in registry.get("fii").tables} == {
        "geral",
        "ativo_passivo",
        "complemento",
    }
    assert len(registry.get("fidc").tables) == 18
    assert {table.slug for table in registry.get("fiagro").tables} == {"informe", "subclasse"}
    # The structured-fund portfolio lives in the CDA archive.
    assert "fie" in {table.slug for table in registry.get("fi_cda").tables}


@pytest.mark.parametrize(
    "dataset, minimum",
    [
        ("fi_inf_diario", 202101),
        ("fidc_inf_mensal", 202501),
        ("fiagro_inf_mensal", 202505),
    ],
)
def test_monthly_datasets_start_when_expected(dataset, minimum):
    spec = registry.get(dataset)
    assert spec.period == "month"
    assert int(spec.min_period.replace("-", "")) == minimum


def test_unknown_dataset_suggests_close_matches():
    with pytest.raises(KeyError) as excinfo:
        registry.get("banana")
    assert "unknown dataset" in str(excinfo.value)


def test_statement_tables_cover_every_document_and_format():
    tables = registry.get("dfp").tables
    assert len(tables) == 14
    slugs = {table.slug for table in tables}
    assert {"bpa_con", "bpp_ind", "dre_con", "dva_ind"} <= slugs
    for table in tables:
        assert re.fullmatch(r"(bpa|bpp|dre|dfc_md|dfc_mi|dmpl|dva)_(con|ind)", table.slug)

"""Free-text extraction helpers: note segmentation and page selection."""

from __future__ import annotations

import zipfile

import pytest

from cvm_mcp import doc_text
from cvm_mcp.normalize import fold
from cvm_mcp.store import NoData
from cvm_mcp.tools_documents import _parse_pages


def page(number: int, text: str) -> dict:
    return {"pagina": number, "texto": text, "texto_fold": fold(text)}


def test_segment_notes_follows_sequential_headings_and_ignores_the_toc():
    pages = [
        page(
            1,
            "Sumário\n"
            "1 Contexto operacional .......... 3\n"
            "2 Base de preparação ............ 4\n"
            "3 Caixa e equivalentes .......... 5\n",
        ),
        page(2, "1 – Contexto operacional\nA companhia atua no setor sucroalcooleiro."),
        page(3, "2 – Base de preparação\nAs demonstrações foram elaboradas conforme o CPC."),
        page(4, "3 – Caixa e equivalentes\nCaixa e bancos: 1.000."),
        page(5, "Relatório do auditor independente\nOpinião sem ressalvas."),
    ]

    notes = doc_text.segment_notes(pages)

    assert [note["nota_numero"] for note in notes] == [1, 2, 3]
    assert notes[0]["nota_titulo"] == "Contexto operacional"
    assert notes[0]["pagina_inicio"] == 2
    assert "sucroalcooleiro" in notes[0]["texto"]
    assert notes[-1]["pagina_fim"] == 4
    assert all("auditor" not in note["texto_fold"] for note in notes)


def test_segment_notes_rejects_footnote_lookalikes():
    pages = [
        page(1, "1 – Contexto\nTexto da nota."),
        page(2, "2 – Base\nTexto da nota."),
        page(
            2,
            "3 - Denominação e objeto social anterior: uma empresa de turismo, "
            "conforme descrito no contrato social.",
        ),
        page(3, "3 – Práticas contábeis\nTexto da nota."),
    ]
    notes = doc_text.segment_notes(pages)
    assert [note["nota_numero"] for note in notes] == [1, 2, 3]
    assert notes[-1]["nota_titulo"] == "Práticas contábeis"


def test_segment_notes_accepts_long_titles():
    long_title = (
        "Reversão (redução) ao valor recuperável e ganhos (perdas) com baixa de "
        "ativos não circulantes, líquidos"
    )
    pages = [
        page(1, "1 – Contexto\nTexto."),
        page(2, f"2 – {long_title}\nTexto."),
        page(3, "3 – Seguintes\nTexto."),
    ]
    notes = doc_text.segment_notes(pages)
    assert notes[1]["nota_titulo"] == long_title


def test_segment_notes_starts_at_the_notes_section():
    """Numbered bullets in the management report must not start the sequence."""
    pages = [
        page(1, "Relatório da administração\n1 Reparação Brumadinho\n2 Reparação Mariana"),
        page(2, "Notas Explicativas às Demonstrações Financeiras\nEm milhões de reais"),
        page(3, "1 – Contexto operacional\nTexto."),
        page(4, "2 – Base de preparação\nTexto."),
    ]
    notes = doc_text.segment_notes(pages)
    assert [note["nota_numero"] for note in notes] == [1, 2]
    assert notes[0]["pagina_inicio"] == 3


def test_segment_notes_returns_nothing_when_numbering_is_missing():
    pages = [
        page(1, "Demonstrações financeiras"),
        page(2, "Notas explicativas\nTexto corrido sem títulos numerados."),
    ]
    assert doc_text.segment_notes(pages) == []


def test_segment_notes_stops_at_a_numbering_gap():
    pages = [
        page(1, "1 – Contexto\nTexto."),
        page(2, "2 – Base\nTexto."),
        page(3, "5 – Salto na numeração\nTexto."),
        page(4, "6 – Outra nota\nTexto."),
    ]
    notes = doc_text.segment_notes(pages)
    assert [note["nota_numero"] for note in notes] == [1, 2]


def test_pdf_member_picks_the_largest_pdf(tmp_path):
    archive = tmp_path / "filing.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("small.pdf", b"%PDF-1.4 small")
        handle.writestr("big.pdf", b"%PDF-1.4 " + b"x" * 5_000)
        handle.writestr("data.xml", b"<x/>")

    payload = doc_text.pdf_member(archive)

    assert payload.startswith(b"%PDF")
    assert len(payload) > 5_000


def test_pdf_member_rejects_an_archive_without_pdf(tmp_path):
    archive = tmp_path / "filing.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("data.xml", b"<x/>")

    with pytest.raises(NoData, match="no PDF"):
        doc_text.pdf_member(archive)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("3", [3]),
        ("3-5", [3, 4, 5]),
        ("5-3", [3, 4, 5]),
        ("1, 3 - 4", [1, 3, 4]),
        ("1;2", [1, 2]),
    ],
)
def test_parse_pages_accepts_ranges(spec, expected):
    assert _parse_pages(spec, last_page=10) == expected


def test_parse_pages_clips_and_rejects_garbage():
    assert _parse_pages("9-12", last_page=10) == [9, 10]
    with pytest.raises(ValueError):
        _parse_pages("abc", last_page=10)
    with pytest.raises(ValueError):
        _parse_pages("", last_page=10)

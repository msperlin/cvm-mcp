"""Full text of the DFP/ITR filings, where the explanatory notes live.

CVM publishes the statements as structured CSV but the *notas explicativas* only
exist inside the filing itself.  The ``LINK_DOC`` column of the DFP/ITR index
points to a ZIP that carries the full document as a PDF (plus an XML with the
statement numbers, which we already have in the CSV tables).  This module
downloads that ZIP, extracts the PDF text page by page and tries to split the
notes into their numbered sections.

Everything is best effort by design: the notes are free text, layouts differ
across companies, and some filings are scanned.  Every row keeps the page number
so an answer can always be traced back to the document.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
from pypdf import PdfReader

from .normalize import fold
from .store import NoData, Store

TEXT_DATASET = "doc_text"

#: Bumped when the note splitter changes, so cached notes are rebuilt from the
#: cached page text without re-downloading the filing.
SEGMENTER_VERSION = "4"

PAGE_COLUMNS = (
    "cd_cvm",
    "cnpj",
    "kind",
    "ano",
    "pagina",
    "texto",
    "texto_fold",
    "id_doc",
    "link_doc",
)

NOTE_COLUMNS = (
    "cd_cvm",
    "cnpj",
    "kind",
    "ano",
    "nota_numero",
    "nota_titulo",
    "pagina_inicio",
    "pagina_fim",
    "texto",
    "texto_fold",
    "id_doc",
    "link_doc",
)

#: ``1– O Banco e suas operações``, ``2. Base de preparação``, ``3 Apresentação``.
_HEADING = re.compile(r"^\s*(\d{1,2})\s*(?:[.\-–—)]|\s)\s*([A-ZÀ-Ú][^\n]{2,120})$")

#: Where the notes stop and the independent auditor's report starts.
_AUDITOR_MARKERS = (
    "relatorio do auditor independente",
    "relatorio dos auditores independentes",
    "parecer do auditor independente",
    "relatorio do auditor",
)


def _text_dir(store: Store, table: str, kind: str, year: int | str, code: int) -> Path:
    return (
        store.parquet_root
        / TEXT_DATASET
        / table
        / kind
        / f"{year}-{int(code)}"
    )


def pages_path(store: Store, kind: str, year: int | str, code: int) -> Path:
    return _text_dir(store, "pagina", kind, year, code) / "data.parquet"


def notes_path(store: Store, kind: str, year: int | str, code: int) -> Path:
    return _text_dir(store, "nota", kind, year, code) / "data.parquet"


def _write(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("data.parquet.tmp")
    frame.to_parquet(temporary, index=False, compression="zstd")
    temporary.replace(path)


def read_pages(store: Store, kind: str, year: int | str, code: int) -> pd.DataFrame:
    path = pages_path(store, kind, year, code)
    if not path.exists():
        return pd.DataFrame(columns=list(PAGE_COLUMNS))
    return pd.read_parquet(path)


def read_notes(store: Store, kind: str, year: int | str, code: int) -> pd.DataFrame:
    path = notes_path(store, kind, year, code)
    if not path.exists():
        return pd.DataFrame(columns=list(NOTE_COLUMNS))
    return pd.read_parquet(path)


def pdf_member(archive: Path) -> bytes:
    """Return the largest PDF inside a CVM filing ZIP."""
    try:
        with zipfile.ZipFile(archive) as handle:
            members = [
                info
                for info in handle.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".pdf")
            ]
            if not members:
                raise NoData(
                    f"{archive.name}: the filing ZIP has no PDF (only "
                    f"{', '.join(name.rsplit('.', 1)[-1] for name in handle.namelist()[:6])})"
                )
            chosen = max(members, key=lambda info: info.file_size)
            return handle.read(chosen)
    except zipfile.BadZipFile as exc:
        raise NoData(f"{archive.name}: not a valid ZIP ({exc})") from exc


def extract_pages(payload: bytes) -> list[dict[str, Any]]:
    """Page-by-page text, with an accent-free copy for searching."""
    reader = PdfReader(io.BytesIO(payload))
    if reader.is_encrypted:
        with contextlib.suppress(Exception):
            # CVM filings are not password-protected; an empty password is the
            # only thing worth trying before falling back to empty page text.
            reader.decrypt("")
    rows: list[dict[str, Any]] = []
    for number, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - one broken page must not kill the filing
            text = ""
        rows.append({"pagina": number, "texto": text, "texto_fold": fold(text)})
    return rows


def _notes_block(pages: list[dict[str, Any]], start_page: int) -> tuple[int, int]:
    """First and last page of the notes section (heuristic)."""
    last = pages[-1]["pagina"] if pages else start_page
    for row in pages:
        if row["pagina"] <= start_page:
            continue
        if any(marker in row["texto_fold"] for marker in _AUDITOR_MARKERS):
            return start_page, row["pagina"] - 1
    return start_page, last


def _notes_anchor(pages: list[dict[str, Any]]) -> int | None:
    """First page that opens the notes section, when the filing says so."""
    for row in pages:
        if "notas explicativas as demonstracoes" in row["texto_fold"]:
            return int(row["pagina"])
    return None


def segment_notes(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split the notes into numbered sections, or return ``[]`` when unsure.

    A heading is only accepted when its number is the next one expected, which
    rejects cross-references, and only after the page that opens the notes
    section, which rejects the numbered bullets of the management report.
    """
    anchor = _notes_anchor(pages)
    lines: list[tuple[int, str]] = []
    for row in pages:
        if anchor is not None and int(row["pagina"]) < anchor:
            continue
        if row["texto_fold"].count("....") >= 3:
            continue  # table of contents: dot leaders and page numbers
        for line in str(row["texto"]).splitlines():
            text = line.strip()
            if text:
                lines.append((row["pagina"], text))

    headings: list[tuple[int, int, str]] = []
    expected = 1
    for index, (page, text) in enumerate(lines):
        match = _HEADING.match(text)
        if not match:
            continue
        number = int(match.group(1))
        title = match.group(2).strip()
        # Footnotes and sentences look like headings but end with punctuation
        # and run long; real note titles are short labels.
        if title.endswith((".", ":", ";")):
            continue
        if len(title) > 140:
            continue
        if number != expected:
            continue
        headings.append((index, number, text))
        expected += 1

    if len(headings) < 2:
        return []

    start_page = lines[headings[0][0]][0]
    _, end_page = _notes_block(pages, start_page)

    notes: list[dict[str, Any]] = []
    for position, (index, number, title) in enumerate(headings):
        stop = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        block = [(page, text) for page, text in lines[index:stop] if page <= end_page]
        if not block:
            continue
        notas_texto = "\n".join(text for _, text in block).strip()
        notes.append(
            {
                "nota_numero": number,
                "nota_titulo": re.sub(r"^\d{1,2}\s*[.\-–—)]?\s*", "", title).strip(),
                "pagina_inicio": block[0][0],
                "pagina_fim": block[-1][0],
                "texto": notas_texto,
                "texto_fold": fold(notas_texto),
            }
        )
    return notes


async def filing(
    store: Store, kind: str, year: int, code: int
) -> dict[str, Any]:
    """Latest index row (link + version) for one company and year."""
    await store.ensure(kind, "documento", str(year))
    try:
        frame = store.read(
            kind,
            "documento",
            periods=[str(year)],
            equals={"cd_cvm": int(code)},
        )
    except NoData as exc:
        raise NoData(
            f"no {kind.upper()} filing indexed for cd_cvm={code} in {year}"
        ) from exc

    if "versao" in frame.columns:
        frame = frame.assign(_versao=pd.to_numeric(frame["versao"], errors="coerce"))
        frame = frame.sort_values("_versao", na_position="first").tail(1)
    row = frame.iloc[-1]
    link = str(row.get("link_doc") or "").strip()
    if not link or link.lower() == "nan":
        raise NoData(
            f"{kind.upper()} {year} for cd_cvm={code} has no LINK_DOC "
            "(the filing is not downloadable)"
        )
    cnpj = row.get("cnpj")
    if pd.isna(cnpj):
        cnpj = row.get("cnpj_cia")
    versao = pd.to_numeric(row.get("versao"), errors="coerce")
    return {
        "cd_cvm": int(code),
        "cnpj": cnpj,
        "id_doc": None if pd.isna(row.get("id_doc")) else row.get("id_doc"),
        "link_doc": link,
        "dt_refer": row.get("dt_refer"),
        "dt_receb": row.get("dt_receb"),
        "versao": (
            None
            if pd.isna(versao)
            else int(versao)
            if float(versao).is_integer()
            else float(versao)
        ),
    }


async def _filing_archive(store: Store, link: str) -> Path:
    """Download the filing ZIP, retrying once on CVM's transient error pages.

    ``rad.cvm.gov.br`` occasionally answers 200 with a tiny HTML page instead of
    the ZIP; the cache layer then rejects the file as invalid.  A second attempt
    after a short pause is enough in practice.
    """
    last: Exception | None = None
    for attempt in range(2):
        try:
            return await store.client.ensure(link)
        except Exception as exc:  # noqa: BLE001 - retried once, then reported
            last = exc
            if attempt == 0:
                await asyncio.sleep(2.0)
    assert last is not None
    raise last


def _notes_frame(
    pages: list[dict[str, Any]], record: dict[str, Any], kind: str, year: int, code: int
) -> pd.DataFrame:
    segmented = segment_notes(pages)
    if not segmented:
        return pd.DataFrame(columns=list(NOTE_COLUMNS))
    notes = pd.DataFrame(segmented)
    notes.insert(0, "ano", int(year))
    notes.insert(0, "kind", kind)
    notes.insert(1, "cnpj", record["cnpj"])
    notes.insert(1, "cd_cvm", int(code))
    notes["id_doc"] = record["id_doc"]
    notes["link_doc"] = record["link_doc"]
    return notes.loc[:, list(NOTE_COLUMNS)]


def _stamp_path(store: Store, kind: str, year: int | str, code: int) -> Path:
    return _text_dir(store, "nota", kind, year, code) / "segmenter.txt"


def _notes_current(store: Store, kind: str, year: int, code: int, pages_file: Path) -> bool:
    stamp = _stamp_path(store, kind, year, code)
    notes_file = notes_path(store, kind, year, code)
    if not stamp.exists():
        return False
    if not notes_file.exists():
        # An empty segmentation is a valid result: the stamp alone proves it.
        return stamp.read_text(encoding="utf-8").strip() == SEGMENTER_VERSION
    return (
        stamp.read_text(encoding="utf-8").strip() == SEGMENTER_VERSION
        and notes_file.stat().st_mtime >= pages_file.stat().st_mtime
    )


def _store_notes(
    store: Store,
    pages: list[dict[str, Any]],
    record: dict[str, Any],
    kind: str,
    year: int,
    code: int,
) -> pd.DataFrame:
    notes = _notes_frame(pages, record, kind, year, code)
    path = notes_path(store, kind, year, code)
    if notes.empty:
        path.unlink(missing_ok=True)
    else:
        _write(notes, path)
    stamp = _stamp_path(store, kind, year, code)
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(SEGMENTER_VERSION, encoding="utf-8")
    return notes


async def ensure_text(
    store: Store,
    kind: str,
    year: int,
    code: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Download the filing, extract its text and cache it as Parquet.

    Returns a small metadata dict with the paths, page count and whether the
    notes could be segmented.
    """
    record = await filing(store, kind, year, code)
    pages_file = pages_path(store, kind, year, code)

    archive = await _filing_archive(store, record["link_doc"])
    cached = (
        not force
        and pages_file.exists()
        and pages_file.stat().st_mtime >= archive.stat().st_mtime
    )
    if cached:
        page_frame = read_pages(store, kind, year, code)
        pages = [
            {"pagina": row.pagina, "texto": row.texto, "texto_fold": row.texto_fold}
            for row in page_frame.itertuples()
        ]
        if _notes_current(store, kind, year, code, pages_file):
            notes = read_notes(store, kind, year, code)
        else:
            notes = _store_notes(store, pages, record, kind, year, code)
        return {
            **record,
            "archive": str(archive),
            "pages": len(pages),
            "notes": len(notes),
            "cached": True,
        }

    payload = pdf_member(archive)
    pages = extract_pages(payload)
    if not pages:
        raise NoData(f"{kind.upper()} {year} for cd_cvm={code}: the PDF has no pages")

    frame = pd.DataFrame(pages)
    frame.insert(0, "ano", int(year))
    frame.insert(0, "kind", kind)
    frame.insert(1, "cnpj", record["cnpj"])
    frame.insert(1, "cd_cvm", int(code))
    frame["id_doc"] = record["id_doc"]
    frame["link_doc"] = record["link_doc"]
    frame = frame.loc[:, list(PAGE_COLUMNS)]
    _write(frame, pages_file)

    notes = _store_notes(store, pages, record, kind, year, code)

    return {
        **record,
        "archive": str(archive),
        "pdf_bytes": len(payload),
        "pages": len(pages),
        "notes": len(notes),
        "cached": False,
    }

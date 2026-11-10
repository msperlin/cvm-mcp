"""Tools for the free-text side of the DFP/ITR filings.

The notes to the financial statements (``notas explicativas``) are not part of
the structured CSV tables: they exist only inside the full filing, whose ZIP is
linked from the ``dfp__documento``/``itr__documento`` index.  These tools expose
that link, the extracted page text and a best-effort index of the numbered
notes.
"""

from __future__ import annotations

import re
import time
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import Field

from . import doc_text
from .app import mcp
from .fetch import describe_error
from .runtime import store
from .store import NoData
from .toolkit import READ_ONLY, error, json_out, parse_codes, parse_data_type

MAX_FILINGS = 20


async def _company_names(codes: list[int]) -> dict[int, str]:
    try:
        frame = await store.registry()
    except Exception:  # noqa: BLE001 - names are cosmetic
        return {}
    subset = frame.loc[frame["CD_CVM"].isin(codes)]
    return {
        int(code): str(name)
        for code, name in zip(subset["CD_CVM"], subset["DENOM_SOCIAL"])
    }


def _parse_pages(spec: str, last_page: int) -> list[int]:
    pages: list[int] = []
    for token in re.split(r"[,;]", spec or ""):
        token = token.strip()
        if not token:
            continue
        match = re.fullmatch(r"(\d+)\s*[-–:]\s*(\d+)", token)
        if match:
            first, final = int(match.group(1)), int(match.group(2))
        elif token.isdigit():
            first = final = int(token)
        else:
            raise ValueError(f"invalid page range {token!r}; use '12' or '12-18'")
        if first > final:
            first, final = final, first
        pages.extend(range(max(1, first), min(last_page, final) + 1))
    if not pages:
        raise ValueError("pages is empty; use e.g. '25-30'")
    return sorted(dict.fromkeys(pages))


def _note_for_page(notes: pd.DataFrame, page: int) -> int | None:
    if notes.empty:
        return None
    match = notes.loc[
        (notes["pagina_inicio"] <= page) & (notes["pagina_fim"] >= page)
    ]
    if match.empty:
        return None
    return int(match.iloc[0]["nota_numero"])


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


@mcp.tool(annotations=READ_ONLY)
async def get_dfp_document(
    cvm_codes: Annotated[
        list[int],
        Field(description="CVM codes (see search_company).", min_length=1, max_length=MAX_FILINGS),
    ],
    year: Annotated[int, Field(description="Reference year of the filing (YYYY).")],
    data_type: Annotated[
        Literal["dfp", "itr"], Field(description="'dfp' = annual, 'itr' = quarterly.")
    ] = "dfp",
) -> str:
    """Filing metadata and the download link of the full document.

    The link points to the CVM ZIP with the complete filing: an XML with the
    statement numbers, a PDF with the *notas explicativas* and the company data.
    Use `get_dfp_notes` to read the notes without handling the file yourself.
    """
    try:
        kind = parse_data_type(data_type)
        codes = parse_codes(cvm_codes) or []
    except ValueError as exc:
        return error(str(exc))
    if not codes:
        return error("cvm_codes is required", hint="Use search_company to find the code.")

    names = await _company_names(codes)
    filings: list[dict[str, Any]] = []
    for code in codes:
        try:
            record = await doc_text.filing(store, kind, year, code)
        except Exception as exc:  # noqa: BLE001
            filings.append({"cd_cvm": code, "error": describe_error(exc)})
            continue
        record["denom_cia"] = names.get(code)
        filings.append(record)

    return json_out(
        {
            "dataset": f"CVM/{kind.upper()}",
            "year": year,
            "filings": filings,
            "note": (
                "The explanatory notes are free text inside the linked filing, not a "
                "CVM table. Call get_dfp_notes to download and extract them."
            ),
        }
    )


@mcp.tool(annotations=READ_ONLY)
async def get_dfp_notes(
    cvm_code: Annotated[int, Field(description="CVM code of the company.")],
    year: Annotated[int, Field(description="Reference year of the filing (YYYY).")],
    note: Annotated[
        int | None,
        Field(description="Number of the note to read (omit to list the notes)."),
    ] = None,
    pages: Annotated[
        str | None,
        Field(
            description=(
                "Raw page selection of the PDF, e.g. '25' or '25-31'. Use it when the "
                "notes could not be split into numbered sections."
            )
        ),
    ] = None,
    data_type: Annotated[
        Literal["dfp", "itr"], Field(description="'dfp' = annual, 'itr' = quarterly.")
    ] = "dfp",
    max_chars: Annotated[
        int, Field(description="Cap on the returned text.", ge=500, le=200_000)
    ] = 30_000,
) -> str:
    """Read the explanatory notes of one company/year.

    Without `note` and `pages`, lists the detected numbered notes (number, title,
    pages). The first call for a company/year downloads the filing PDF (~15 MB)
    and extracts its text; later calls read the local cache.
    """
    try:
        kind = parse_data_type(data_type)
    except ValueError as exc:
        return error(str(exc))
    if note is not None and pages is not None:
        return error("pass either note or pages, not both")

    started = time.perf_counter()
    try:
        info = await doc_text.ensure_text(store, kind, year, int(cvm_code))
    except (NoData, ValueError) as exc:
        return error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return error(describe_error(exc))

    notes = doc_text.read_notes(store, kind, year, int(cvm_code))
    name = (await _company_names([int(cvm_code)])).get(int(cvm_code))
    base: dict[str, Any] = {
        "dataset": f"CVM/{kind.upper()}",
        "cd_cvm": int(cvm_code),
        "denom_cia": name,
        "year": year,
        "pages": info.get("pages"),
        "notes_detected": len(notes),
        "cached": info.get("cached"),
        "source": info.get("link_doc"),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
    }

    if pages is not None:
        frame = doc_text.read_pages(store, kind, year, int(cvm_code))
        if frame.empty:
            return error("no extracted text for this filing", **base)
        try:
            selected = _parse_pages(pages, int(frame["pagina"].max()))
        except ValueError as exc:
            return error(str(exc))
        chunk = frame.loc[frame["pagina"].isin(selected)]
        body = "\n\n".join(
            f"--- página {int(row.pagina)} ---\n{row.texto}"
            for row in chunk.itertuples()
        )
        text, truncated = _clip(body, max_chars)
        return json_out(
            {
                **base,
                "page_selection": selected,
                "text": text,
                "truncated": truncated,
                "hint": (
                    "Use search_dfp_notes to locate a term with page numbers, or "
                    "note=<number> when the notes were segmented."
                ),
            }
        )

    if note is not None:
        if notes.empty:
            return error(
                "the notes of this filing could not be split into numbered sections",
                hint="Call again with pages='first-last' or use search_dfp_notes.",
                **base,
            )
        match = notes.loc[notes["nota_numero"] == int(note)]
        if match.empty:
            return error(
                f"note {note} not found in this filing",
                available=[int(value) for value in notes["nota_numero"].tolist()],
                **base,
            )
        row = match.iloc[0]
        text, truncated = _clip(str(row["texto"]), max_chars)
        return json_out(
            {
                **base,
                "note": int(row["nota_numero"]),
                "title": row["nota_titulo"],
                "page_from": int(row["pagina_inicio"]),
                "page_to": int(row["pagina_fim"]),
                "text": text,
                "truncated": truncated,
            }
        )

    if notes.empty:
        return json_out(
            {
                **base,
                "notes_indexed": False,
                "hint": (
                    "Numbered headers were not recognised (common in scanned or "
                    "unusually laid out filings). Use pages='first-last' or "
                    "search_dfp_notes to read the text."
                ),
            }
        )

    return json_out(
        {
            **base,
            "notes_indexed": True,
            "notes": [
                {
                    "note": int(row["nota_numero"]),
                    "title": row["nota_titulo"],
                    "page_from": int(row["pagina_inicio"]),
                    "page_to": int(row["pagina_fim"]),
                    "chars": len(str(row["texto"])),
                }
                for _, row in notes.sort_values("nota_numero").iterrows()
            ],
            "hint": "Call again with note=<number> to read one note.",
        }
    )


@mcp.tool(annotations=READ_ONLY)
async def search_dfp_notes(
    term: Annotated[str, Field(description="Text to look for (accent and case insensitive).", min_length=2)],
    cvm_codes: Annotated[
        list[int],
        Field(description="CVM codes to search (see search_company).", min_length=1, max_length=5),
    ],
    year: Annotated[int, Field(description="Reference year of the filing (YYYY).")],
    data_type: Annotated[
        Literal["dfp", "itr"], Field(description="'dfp' = annual, 'itr' = quarterly.")
    ] = "dfp",
    limit: Annotated[int, Field(description="Maximum number of snippets.", ge=1, le=100)] = 20,
    context: Annotated[
        int, Field(description="Characters of context around each hit.", ge=50, le=2000)
    ] = 300,
) -> str:
    """Search the extracted text of the filing, returning snippets with pages.

    Searches every page (not only the detected notes) so a hit is never lost to
    a segmentation failure. Each match carries the PDF page and, when known, the
    number of the note it belongs to.
    """
    try:
        kind = parse_data_type(data_type)
        codes = parse_codes(cvm_codes) or []
    except ValueError as exc:
        return error(str(exc))
    needle = doc_text.fold(term).strip()
    if not needle:
        return error("term is empty")

    names = await _company_names(codes)
    matches: list[dict[str, Any]] = []
    searched: list[dict[str, Any]] = []
    failures: dict[str, str] = {}
    truncated = False

    for code in codes:
        try:
            info = await doc_text.ensure_text(store, kind, year, code)
        except Exception as exc:  # noqa: BLE001
            failures[str(code)] = describe_error(exc)
            continue
        pages = doc_text.read_pages(store, kind, year, code)
        notes = doc_text.read_notes(store, kind, year, code)
        hits = 0
        for row in pages.itertuples():
            hay = str(row.texto_fold)
            original = str(row.texto)
            start = hay.find(needle)
            while start >= 0:
                if len(matches) >= limit:
                    truncated = True
                    break
                if len(hay) == len(original):
                    snippet = original[max(0, start - context) : start + len(needle) + context]
                else:
                    snippet = hay[max(0, start - context) : start + len(needle) + context]
                matches.append(
                    {
                        "cd_cvm": code,
                        "denom_cia": names.get(code),
                        "page": int(row.pagina),
                        "note": _note_for_page(notes, int(row.pagina)),
                        "snippet": " ".join(snippet.split()),
                    }
                )
                hits += 1
                start = hay.find(needle, start + len(needle))
            if truncated:
                break
        searched.append(
            {
                "cd_cvm": code,
                "pages": info.get("pages"),
                "hits": hits,
                "cached": info.get("cached"),
            }
        )
        if truncated:
            break

    payload: dict[str, Any] = {
        "dataset": f"CVM/{kind.upper()}",
        "term": term,
        "year": year,
        "matches": matches,
        "searched": searched,
        "truncated": truncated,
        "source": "full filing PDF linked from the DFP/ITR index (free text)",
    }
    if failures:
        payload["failed"] = failures
    return json_out(payload)

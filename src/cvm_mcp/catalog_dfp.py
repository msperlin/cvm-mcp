"""A small, curated catalog of the standard CVM chart of accounts.

``CD_CONTA`` is stable *inside* one industry chart, but the same code means
different things across charts: ``3.09`` is "resultado liquido das operacoes
continuadas" for an industrial company and "lucro/prejuizo consolidado do
periodo" for a bank, and ``2.03`` is shareholders' equity for an industrial but
"provisoes" for a bank.

Resolution therefore uses the code **and** the label: a code is only trusted
when ``DS_CONTA`` confirms it, otherwise the label alone decides.  Every tool
also returns the actual ``DS_CONTA`` so the answer can be verified.

``3.99`` (earnings per share) is intentionally absent: CVM publishes ``0`` in the
parent account and the real figure in ``3.99.01.01`` / ``3.99.02.01``, one per
share class, and those are per-share values rather than monetary amounts.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Final

import pandas as pd


def fold(text: object) -> str:
    """Lowercase, accent-free form used for label matching."""
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


@dataclass(frozen=True)
class Account:
    doc: str
    code: str
    label: str
    role: str
    #: Accent-free fragments that must appear in ``DS_CONTA`` for this code to
    #: be trusted.  Empty means "trust the code alone".
    matches: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "doc_type": self.doc,
            "code": self.code,
            "label": self.label,
            "role": self.role,
            "matches": list(self.matches),
        }

    def matches_label(self, folded_label: str) -> bool:
        if not self.matches:
            return True
        return any(fragment in folded_label for fragment in self.matches)


ROLES: Final[tuple[Account, ...]] = (
    # -- balance sheet: assets ---------------------------------------
    Account("BPA", "1", "Ativo Total", "total_assets", ("ativo total",)),
    Account("BPA", "1.01", "Ativo Circulante", "current_assets", ("ativo circulante",)),
    Account(
        "BPA", "1.02", "Ativo Nao Circulante", "non_current_assets", ("ativo nao circulante",)
    ),
    # -- balance sheet: liabilities + equity -------------------------
    Account("BPP", "2", "Passivo Total", "total_liabilities_and_equity", ("passivo total",)),
    Account("BPP", "2.01", "Passivo Circulante", "current_liabilities", ("passivo circulante",)),
    Account(
        "BPP",
        "2.02",
        "Passivo Nao Circulante",
        "non_current_liabilities",
        ("passivo nao circulante",),
    ),
    # Banks report equity as 2.07 / 2.08, so the label rules here.
    Account("BPP", "2.03", "Patrimonio Liquido Consolidado", "equity", ("patrimonio liquido",)),
    # -- income statement --------------------------------------------
    Account("DRE", "3.01", "Receita de Venda de Bens e/ou Servicos", "revenue", ("receita",)),
    Account(
        "DRE",
        "3.02",
        "Custo dos Bens e/ou Servicos Vendidos",
        "cost",
        ("custo dos bens", "despesas da intermediacao", "despesas da atividade seguradora"),
    ),
    Account("DRE", "3.03", "Resultado Bruto", "gross_profit", ("resultado bruto",)),
    Account(
        "DRE",
        "3.04",
        "Despesas/Receitas Operacionais",
        "operating_expenses",
        ("despesas/receitas operacionais", "despesas e receitas operacionais"),
    ),
    # Only industrial/insurance charts report EBIT; banks go straight to pretax.
    Account(
        "DRE",
        "3.05",
        "Resultado Antes do Resultado Financeiro e dos Tributos",
        "ebit",
        ("resultado antes do resultado financeiro",),
    ),
    Account("DRE", "3.06", "Resultado Financeiro", "financial_result", ("resultado financeiro",)),
    Account(
        "DRE",
        "3.07",
        "Resultado Antes dos Tributos sobre o Lucro",
        "pretax_income",
        ("resultado antes dos tributos",),
    ),
    Account(
        "DRE",
        "3.08",
        "Imposto de Renda e Contribuicao Social",
        "income_tax",
        ("imposto de renda",),
    ),
    Account(
        "DRE",
        "3.09",
        "Resultado Liquido das Operacoes Continuadas",
        "continuing_operations",
        ("operacoes continuadas",),
    ),
    Account(
        "DRE",
        "3.10",
        "Resultado Liquido de Operacoes Descontinuadas",
        "discontinued_operations",
        ("operacoes descontinuadas",),
    ),
    # The bottom line, whichever code the chart uses (3.09, 3.11 or 3.13) and
    # whichever label the filing year uses ("do Periodo" / "Consolidado").
    Account(
        "DRE",
        "3.11",
        "Lucro/Prejuizo Consolidado do Periodo",
        "net_income",
        (
            "lucro/prejuizo consolidado do periodo",
            "lucro/prejuizo do periodo",
            "lucro ou prejuizo liquido consolidado",
        ),
    ),
    # -- cash flow (indirect method) ---------------------------------
    Account(
        "DFC_MI",
        "6.01",
        "Caixa Liquido Atividades Operacionais",
        "operating_cash_flow",
        ("atividades operacionais",),
    ),
    Account(
        "DFC_MI",
        "6.02",
        "Caixa Liquido Atividades de Investimento",
        "investing_cash_flow",
        ("atividades de investimento",),
    ),
    Account(
        "DFC_MI",
        "6.03",
        "Caixa Liquido Atividades de Financiamento",
        "financing_cash_flow",
        ("atividades de financiamento",),
    ),
    Account(
        "DFC_MI",
        "6.05",
        "Aumento (Reducao) de Caixa e Equivalentes",
        "net_cash_change",
        ("aumento (reducao) de caixa",),
    ),
    # -- value added -------------------------------------------------
    Account("DVA", "7.01", "Receitas", "value_added_revenue"),
    Account("DVA", "7.02", "Insumos Adquiridos de Terceiros", "value_added_inputs"),
    Account("DVA", "7.03", "Valor Adicionado Bruto", "gross_value_added"),
    Account("DVA", "7.07", "Valor Adicionado Total a Distribuir", "value_added_to_distribute"),
    Account("DVA", "7.08", "Distribuicao do Valor Adicionado", "value_added_distribution"),
)

_BY_CODE: Final[dict[tuple[str, str], Account]] = {(a.doc, a.code): a for a in ROLES}
_BY_ROLE: Final[dict[tuple[str, str], Account]] = {(a.doc, a.role): a for a in ROLES}
_BY_DOC: Final[dict[str, tuple[Account, ...]]] = {
    doc: tuple(account for account in ROLES if account.doc == doc)
    for doc in dict.fromkeys(account.doc for account in ROLES)
}

DOC_TYPES_WITH_CATALOG: Final[tuple[str, ...]] = tuple(_BY_DOC)


def accounts_for(doc_type: str | None = None) -> tuple[Account, ...]:
    """Every catalog entry, optionally filtered by document type."""
    if doc_type is None:
        return ROLES
    return _BY_DOC.get(doc_type.upper(), ())


def lookup_code(doc_type: str, code: str) -> Account | None:
    return _BY_CODE.get((doc_type.upper(), str(code).strip()))


def lookup_role(doc_type: str, role: str) -> Account | None:
    return _BY_ROLE.get((doc_type.upper(), str(role).strip().lower()))


def resolve_role(doc_type: str, role: str) -> str:
    """Turn a role name into the catalog ``CD_CONTA``, raising a helpful error.

    The returned code is only a *hint*: ``role_for_row`` may match a different
    code in an industry chart that numbers the statement differently.
    """
    account = lookup_role(doc_type, role)
    if account is not None:
        return account.code
    known = sorted(name for doc, name in _BY_ROLE if doc == doc_type.upper())
    raise KeyError(
        f"unknown role {role!r} for {doc_type.upper()}. Known roles: {', '.join(known)}"
    )


def _depth(code: str) -> int:
    return str(code).count(".")


def role_for_row(doc: str, code: str, label: str) -> str | None:
    """Resolve one reported line to a role, trusting the label over the code."""
    folded_label = fold(label)
    entry = _BY_CODE.get((str(doc), str(code).strip()))
    if entry is not None and entry.matches_label(folded_label):
        return entry.role

    # The code did not confirm.  Re-resolve by label, but only for top-level
    # lines: sub-accounts such as "Outras Receitas Operacionais" must not be
    # mistaken for the revenue line.
    if _depth(code) > 1:
        return None
    for candidate in _BY_DOC.get(str(doc), ()):
        if candidate.matches and candidate.matches_label(folded_label):
            return candidate.role
    return None


def top_level(frame: pd.DataFrame, *, max_depth: int = 1) -> pd.DataFrame:
    """Keep the headline lines of a statement (depth 0 or 1)."""
    if frame.empty or "ACCOUNT_DEPTH" not in frame.columns:
        return frame
    return frame.loc[frame["ACCOUNT_DEPTH"] <= max_depth].copy()


def annotate_roles(frame: pd.DataFrame) -> pd.DataFrame:
    """Attach ``ROLE`` / ``CATALOG_LABEL`` columns where a line is recognized."""
    if frame.empty or "CD_CONTA" not in frame.columns:
        return frame

    frame = frame.copy()
    documents = frame["doc"].astype(str) if "doc" in frame.columns else pd.Series("", index=frame.index)
    labels = frame["DS_CONTA"].fillna("").astype(str) if "DS_CONTA" in frame.columns else None
    codes = frame["CD_CONTA"].astype(str)

    roles: list[object] = []
    catalog_labels: list[object] = []
    for index, doc, code in zip(frame.index, documents, codes):
        label = labels.loc[index] if labels is not None else ""
        role = role_for_row(doc, code, label)
        roles.append(role if role is not None else pd.NA)
        entry = lookup_role(doc, role) if role is not None else None
        catalog_labels.append(entry.label if entry is not None else pd.NA)

    frame["ROLE"] = roles
    frame["CATALOG_LABEL"] = catalog_labels
    return frame

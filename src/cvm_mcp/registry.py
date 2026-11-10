"""Declarative registry of the CVM open-data datasets.

Adding a dataset should be a *data* change, not a code change: describe the URL
pattern, the period granularity and the tables inside the archive, and pick one
of the four layouts the store already understands.

    statements      DFP/ITR: one zip per year, 7 documents x 2 formats inside
    yearly_tables   FRE/VLMO/CGVN/FCA: one zip per year, N named tables inside
    monthly_tables  FI/FIDC/CDA: one zip per month, N named tables inside
    single_archive  one zip (or CSV) with a fixed set of tables, no period
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

BASE_URL = "https://dados.cvm.gov.br/dados/"


class Layout(str, Enum):
    STATEMENTS = "statements"
    YEARLY_TABLES = "yearly_tables"
    MONTHLY_TABLES = "monthly_tables"
    SINGLE_ARCHIVE = "single_archive"


class Normalizer(str, Enum):
    """Which normalization pipeline the store applies to the raw CSV."""

    DFP = "dfp"       # canonical statement schema (uppercase, VL_CONTA_BRL)
    FRE = "fre"       # snake_case + shared key block + cd_cvm
    GENERIC = "generic"  # snake_case + inferred types


@dataclass(frozen=True)
class Table:
    slug: str
    member: str
    title: str = ""
    key_columns: tuple[str, ...] = ()
    cnpj_column: str | None = None
    #: ``True`` for the ``DOC/fmt`` statement members that share the canonical
    #: DFP schema.  Flat members (index, pareceres, composition) go through the
    #: generic normalizer instead.
    statement: bool = False
    #: Whether ``load_dataset`` builds this table when no table is named.  Big
    #: free-text members default to off and are loaded explicitly.
    default: bool = True

    def member_name(self, *, period: str, doc: str | None = None, fmt: str | None = None) -> str:
        return self.member.format(period=period, year=period, doc=doc, fmt=fmt)


@dataclass(frozen=True)
class Dataset:
    id: str
    domain: str
    title: str
    description: str
    layout: Layout
    normalizer: Normalizer
    url: str
    tables: tuple[Table, ...]
    period: str = "year"           # "year" | "month" | "none"
    min_period: str = "2010"
    refresh_hours: float = 12.0
    heavy: bool = False
    encoding: str = "latin1"
    sep: str = ";"
    notes: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def directory(self) -> str:
        return self.url.rsplit("/", 1)[0] + "/"

    def url_for(self, period: str) -> str:
        return BASE_URL + self.url.format(period=period)

    def table(self, name: str) -> Table:
        key = str(name).strip().lower()
        for table in self.tables:
            if table.slug == key:
                return table
        close = [table.slug for table in self.tables if key in table.slug or table.slug in key][:5]
        hint = f" Close matches: {', '.join(close)}." if close else ""
        raise KeyError(
            f"unknown table {name!r} in dataset {self.id!r}.{hint} "
            f"Available: {', '.join(t.slug for t in self.tables)}"
        )


def _statement_tables(kind: str) -> tuple[Table, ...]:
    docs = ("BPA", "BPP", "DRE", "DFC_MD", "DFC_MI", "DMPL", "DRA", "DVA")
    formats = ("con", "ind")
    tables: list[Table] = []
    for doc in docs:
        for fmt in formats:
            tables.append(
                Table(
                    slug=f"{doc.lower()}_{fmt}",
                    member=f"{kind}_cia_aberta_{doc}_{fmt}_{{period}}.csv",
                    title=f"{doc}/{fmt}",
                    statement=True,
                )
            )
    return tuple(tables)


def _dfp_flat_tables(kind: str) -> tuple[Table, ...]:
    """Non-statement members of the DFP/ITR archive.

    ``documento`` is the index with the ``LINK_DOC`` of the full filing (the
    only place the explanatory notes live), ``parecer`` holds the free-text
    auditor/director statements, and ``composicao_capital`` the share counts.
    """
    return (
        Table(
            slug="documento",
            member=f"{kind}_cia_aberta_{{period}}.csv",
            title="Filing index and document links",
            key_columns=("cnpj_cia", "cd_cvm"),
            cnpj_column="cnpj_cia",
        ),
        Table(
            slug="composicao_capital",
            member=f"{kind}_cia_aberta_composicao_capital_{{period}}.csv",
            title="Composição do capital",
            key_columns=("cnpj_cia",),
            cnpj_column="cnpj_cia",
        ),
        Table(
            slug="parecer",
            member=f"{kind}_cia_aberta_parecer_{{period}}.csv",
            title="Pareceres e declarações (free text)",
            key_columns=("cnpj_cia",),
            cnpj_column="cnpj_cia",
            default=False,
        ),
    )


def _fre_tables() -> tuple[Table, ...]:
    from .catalog_fre import all_tables

    tables: list[Table] = []
    for item in all_tables():
        member = (
            f"fre_cia_aberta_{item.source}_{{period}}.csv"
            if item.source
            else "fre_cia_aberta_{period}.csv"
        )
        tables.append(
            Table(
                slug=item.slug,
                member=member,
                title=item.title,
                key_columns=item.key_columns,
            )
        )
    return tuple(tables)


DATASETS: tuple[Dataset, ...] = (
    # -- companies ---------------------------------------------------
    Dataset(
        id="dfp",
        domain="companies",
        title="CVM/DFP — annual financial statements",
        description=(
            "Demonstrações Financeiras Padronizadas: BPA, BPP, DRE, DFC_MD, DFC_MI, "
            "DMPL, DRA and DVA, consolidated and individual, 2010 onwards, plus the "
            "filing index with the download link of the full filing (where the "
            "explanatory notes live) and the free-text pareceres."
        ),
        layout=Layout.STATEMENTS,
        normalizer=Normalizer.DFP,
        url="CIA_ABERTA/DOC/DFP/DADOS/dfp_cia_aberta_{period}.zip",
        tables=_statement_tables("dfp") + _dfp_flat_tables("dfp"),
        min_period="2010",
        aliases=("financial_statements", "demonstrativos"),
    ),
    Dataset(
        id="itr",
        domain="companies",
        title="CVM/ITR — quarterly financial statements",
        description=(
            "Formulário de Informações Trimestrais (year-to-date figures), plus the "
            "filing index with the download link of the full filing and the "
            "free-text pareceres."
        ),
        layout=Layout.STATEMENTS,
        normalizer=Normalizer.DFP,
        url="CIA_ABERTA/DOC/ITR/DADOS/itr_cia_aberta_{period}.zip",
        tables=_statement_tables("itr") + _dfp_flat_tables("itr"),
        min_period="2010",
        aliases=("quarterly",),
    ),
    Dataset(
        id="fre",
        domain="companies",
        title="CVM/FRE — Formulário de Referência",
        description=(
            "36 corporate tables: shareholders, board, compensation, capital, "
            "related-party transactions, auditors, employees, securities."
        ),
        layout=Layout.YEARLY_TABLES,
        normalizer=Normalizer.FRE,
        url="CIA_ABERTA/DOC/FRE/DADOS/fre_cia_aberta_{period}.zip",
        tables=_fre_tables(),
        min_period="2010",
        aliases=("formulario_referencia",),
    ),
    Dataset(
        id="fca",
        domain="companies",
        title="CVM/FCA — Formulário Cadastral (tickers)",
        description="Securities issued by each company; the CNPJ ↔ cd_cvm ↔ ticker bridge.",
        layout=Layout.YEARLY_TABLES,
        normalizer=Normalizer.GENERIC,
        url="CIA_ABERTA/DOC/FCA/DADOS/fca_cia_aberta_{period}.zip",
        tables=(
            Table(
                slug="valor_mobiliario",
                member="fca_cia_aberta_valor_mobiliario_{period}.csv",
                title="Securities / tickers",
                key_columns=("cnpj_companhia", "codigo_negociacao"),
                cnpj_column="cnpj_companhia",
            ),
        ),
        min_period="2018",
        aliases=("tickers",),
    ),
    Dataset(
        id="vlmo",
        domain="companies",
        title="CVM/VLMO — insider trading (valores mobiliários negociados e detidos)",
        description=(
            "Positions and transactions of controllers, officers and board members, "
            "article 11 of CVM Instruction 358. 2018 onwards."
        ),
        layout=Layout.YEARLY_TABLES,
        normalizer=Normalizer.GENERIC,
        url="CIA_ABERTA/DOC/VLMO/DADOS/vlmo_cia_aberta_{period}.zip",
        tables=(
            Table(
                slug="posicao",
                member="vlmo_cia_aberta_{period}.csv",
                title="Filing header / consolidated position",
                key_columns=("cnpj_companhia", "data_referencia"),
                cnpj_column="cnpj_companhia",
            ),
            Table(
                slug="transacoes",
                member="vlmo_cia_aberta_con_{period}.csv",
                title="Individual movements (who traded what, when, at what price)",
                key_columns=("cnpj_companhia", "data_movimentacao"),
                cnpj_column="cnpj_companhia",
            ),
        ),
        min_period="2018",
        aliases=("insiders", "insider"),
    ),
    Dataset(
        id="cgvn",
        domain="companies",
        title="CVM/ICBGC — Informe do Código de Governança",
        description=(
            "Explain-or-comply governance report: each recommended practice and "
            "whether the company adopted it. 2018 onwards."
        ),
        layout=Layout.YEARLY_TABLES,
        normalizer=Normalizer.GENERIC,
        url="CIA_ABERTA/DOC/CGVN/DADOS/cgvn_cia_aberta_{period}.zip",
        tables=(
            Table(
                slug="documento",
                member="cgvn_cia_aberta_{period}.csv",
                title="Filing header",
                key_columns=("cnpj_companhia", "data_referencia"),
                cnpj_column="cnpj_companhia",
            ),
            Table(
                slug="praticas",
                member="cgvn_cia_aberta_praticas_{period}.csv",
                title="Recommended vs adopted practices",
                key_columns=("cnpj_companhia", "id_item"),
                cnpj_column="cnpj_companhia",
            ),
        ),
        min_period="2018",
        aliases=("governanca", "governance"),
    ),
    # -- funds -------------------------------------------------------
    Dataset(
        id="fi_inf_diario",
        domain="funds",
        title="CVM/FI — Informe Diário",
        description=(
            "Daily net asset value, quota price, redemptions, subscriptions and "
            "number of unitholders for every investment fund. 2021 onwards."
        ),
        layout=Layout.MONTHLY_TABLES,
        normalizer=Normalizer.GENERIC,
        url="FI/DOC/INF_DIARIO/DADOS/inf_diario_fi_{period}.zip",
        tables=(
            Table(
                slug="informe",
                member="inf_diario_fi_{period}.csv",
                title="Daily fund report",
                key_columns=("cnpj_fundo_classe", "dt_comptc"),
                cnpj_column="cnpj_fundo_classe",
            ),
        ),
        period="month",
        min_period="2021-01",
        aliases=("funds_daily", "cota"),
    ),
    Dataset(
        id="fi_cda",
        domain="funds",
        title="CVM/FI — Composição e Diversificação das Aplicações (carteira)",
        description=(
            "Full portfolio of every fund: asset, issuer, quantity, market value and "
            "cost. Split into 8 blocks (BLC_1..8) plus the net asset value (PL)."
        ),
        layout=Layout.MONTHLY_TABLES,
        normalizer=Normalizer.GENERIC,
        url="FI/DOC/CDA/DADOS/cda_fi_{period}.zip",
        tables=(
            Table("pl", "cda_fi_PL_{period}.csv", "Net asset value", ("cnpj_fundo_classe",), "cnpj_fundo_classe"),
            Table(
                "fie",
                "cda_fie_{period}.csv",
                "Structured-fund portfolio (FII, FIDC, FIAGRO, FIP)",
                ("cnpj_fundo_classe",),
                "cnpj_fundo_classe",
            ),
            *tuple(
                Table(
                    f"blc_{index}",
                    f"cda_fi_BLC_{index}_{{period}}.csv",
                    f"Portfolio block {index}",
                    ("cnpj_fundo_classe",),
                    "cnpj_fundo_classe",
                )
                for index in range(1, 9)
            ),
        ),
        period="month",
        min_period="2023-01",
        heavy=True,
        notes="One month is ~270 MB uncompressed; build a single block at a time.",
        aliases=("carteira", "portfolio"),
    ),
    Dataset(
        id="fi_cad",
        domain="funds",
        title="CVM/FI — Cadastro de fundos, classes e subclasses",
        description=(
            "Registry of funds, classes and subclasses: CNPJ, CVM code, manager, "
            "auditor, classification, status."
        ),
        layout=Layout.SINGLE_ARCHIVE,
        normalizer=Normalizer.GENERIC,
        url="FI/CAD/DADOS/registro_fundo_classe.zip",
        tables=(
            Table("fundo", "registro_fundo.csv", "Funds", ("cnpj_fundo",), "cnpj_fundo"),
            Table("classe", "registro_classe.csv", "Classes", ("cnpj_classe",), "cnpj_classe"),
            Table("subclasse", "registro_subclasse.csv", "Subclasses", ("id_registro_classe",)),
        ),
        period="none",
        min_period="",
        refresh_hours=24,
        aliases=("fund_registry", "cadastro_fundos"),
    ),
    # -- structured funds --------------------------------------------
    Dataset(
        id="fii_inf_mensal",
        domain="funds",
        title="CVM/FII — Informe Mensal",
        description=(
            "Monthly report of every real-estate fund: net asset value, book value "
            "per share, unitholders, dividend yield, segment and mandate, plus the "
            "balance-sheet composition."
        ),
        layout=Layout.YEARLY_TABLES,
        normalizer=Normalizer.GENERIC,
        url="FII/DOC/INF_MENSAL/DADOS/inf_mensal_fii_{period}.zip",
        tables=(
            Table(
                "geral",
                "inf_mensal_fii_geral_{period}.csv",
                "Identity, segment and mandate",
                ("cnpj_fundo_classe", "data_referencia"),
                "cnpj_fundo_classe",
            ),
            Table(
                "ativo_passivo",
                "inf_mensal_fii_ativo_passivo_{period}.csv",
                "Balance-sheet composition (properties, CRI, FII quotas, cash...)",
                ("cnpj_fundo_classe", "data_referencia"),
                "cnpj_fundo_classe",
            ),
            Table(
                "complemento",
                "inf_mensal_fii_complemento_{period}.csv",
                "Unitholders, NAV, returns, dividend yield and fees",
                ("cnpj_fundo_classe", "data_referencia"),
                "cnpj_fundo_classe",
            ),
        ),
        period="year",
        min_period="2016",
        aliases=("fii", "fundos_imobiliarios"),
    ),
    Dataset(
        id="fidc_inf_mensal",
        domain="funds",
        title="CVM/FIDC — Informe Mensal",
        description=(
            "Monthly credit-fund report: assets, credit portfolio by segment, "
            "delinquency, net asset value and credit quality (18 tables)."
        ),
        layout=Layout.MONTHLY_TABLES,
        normalizer=Normalizer.GENERIC,
        url="FIDC/DOC/INF_MENSAL/DADOS/inf_mensal_fidc_{period}.zip",
        tables=tuple(
            Table(
                f"tab_{slug}",
                f"inf_mensal_fidc_tab_{name}_{{period}}.csv",
                f"FIDC table {name}",
                ("cnpj_fundo_classe", "dt_comptc"),
                "cnpj_fundo_classe",
            )
            for slug, name in (
                ("i", "I"),
                ("ii", "II"),
                ("iii", "III"),
                ("iv", "IV"),
                ("v", "V"),
                ("vi", "VI"),
                ("vii", "VII"),
                ("viii", "VIII"),
                ("ix", "IX"),
                ("x", "X"),
                ("x_1", "X_1"),
                ("x_1_1", "X_1_1"),
                ("x_2", "X_2"),
                ("x_3", "X_3"),
                ("x_4", "X_4"),
                ("x_5", "X_5"),
                ("x_6", "X_6"),
                ("x_7", "X_7"),
            )
        ),
        period="month",
        min_period="2025-01",
        aliases=("fidc", "direitos_creditorios"),
    ),
    Dataset(
        id="fiagro_inf_mensal",
        domain="funds",
        title="CVM/FIAGRO — Informe Mensal",
        description=(
            "Monthly report of agribusiness chain funds: net asset value, "
            "unitholders, returns, fees and asset composition."
        ),
        layout=Layout.MONTHLY_TABLES,
        normalizer=Normalizer.GENERIC,
        url="FIAGRO/DOC/INF_MENSAL/DADOS/inf_mensal_fiagro_{period}.zip",
        tables=(
            Table(
                "informe",
                "inf_mensal_fiagro_{period}.csv",
                "Fund report",
                ("cnpj_classe", "data_referencia"),
                "cnpj_classe",
            ),
            Table(
                "subclasse",
                "inf_mensal_fiagro_subclasse_{period}.csv",
                "Subclass net asset value per quota",
                ("cnpj_classe", "data_referencia"),
                "cnpj_classe",
            ),
        ),
        period="month",
        min_period="2025-05",
        aliases=("fiagro", "agronegocio"),
    ),
    # -- market ------------------------------------------------------
    Dataset(
        id="oferta",
        domain="offerings",
        title="CVM — Ofertas Públicas de Distribuição",
        description=(
            "Registered, exempt and restricted offerings since 1988: issuer, lead "
            "underwriter, asset type, rite, dates, price and amount."
        ),
        layout=Layout.SINGLE_ARCHIVE,
        normalizer=Normalizer.GENERIC,
        url="OFERTA/DISTRIB/DADOS/oferta_distribuicao.zip",
        tables=(
            Table(
                "distribuicao",
                "oferta_distribuicao.csv",
                "Offerings (ICVM 400 / RCVM 160)",
                ("numero_processo", "nome_emissor"),
                "cnpj_emissor",
            ),
            Table(
                "resolucao_160",
                "oferta_resolucao_160.csv",
                "RCVM 160 requests",
                ("numero_processo", "nome_emissor"),
                "cnpj_emissor",
            ),
        ),
        period="none",
        min_period="",
        refresh_hours=24,
        aliases=("ofertas", "offerings"),
    ),
    Dataset(
        id="sancionador",
        domain="enforcement",
        title="CVM — Processos Sancionadores (PAS)",
        description="Sanctioning proceedings and the accused parties, with current phase.",
        layout=Layout.SINGLE_ARCHIVE,
        normalizer=Normalizer.GENERIC,
        url="PROCESSO/SANCIONADOR/DADOS/processo_sancionador.zip",
        tables=(
            Table("processos", "processo_sancionador.csv", "Proceedings", ("nup",)),
            Table("acusados", "processo_sancionador_acusado.csv", "Accused parties", ("nup",)),
        ),
        period="none",
        min_period="",
        refresh_hours=24,
        aliases=("sancoes", "sanctions"),
    ),
    # -- registry ----------------------------------------------------
    Dataset(
        id="cad_cia_aberta",
        domain="registry",
        title="CVM — Cadastro de companhias abertas",
        description="Company registry: CNPJ, cd_cvm, sector, market, status, address.",
        layout=Layout.SINGLE_ARCHIVE,
        normalizer=Normalizer.GENERIC,
        url="CIA_ABERTA/CAD/DADOS/cad_cia_aberta.csv",
        tables=(
            Table("companhias", "cad_cia_aberta.csv", "Companies", ("cnpj_cia", "cd_cvm"), "cnpj_cia"),
        ),
        period="none",
        min_period="",
        refresh_hours=24,
        aliases=("registry", "cadastro"),
    ),
)

BY_ID: dict[str, Dataset] = {}
for _dataset in DATASETS:
    BY_ID[_dataset.id] = _dataset
    for _alias in _dataset.aliases:
        BY_ID.setdefault(_alias, _dataset)


def get(dataset_id: str) -> Dataset:
    """Resolve a dataset id or alias."""
    key = str(dataset_id).strip().lower()
    dataset = BY_ID.get(key)
    if dataset is None:
        close = [name for name in {d.id for d in DATASETS} if key in name or name in key][:5]
        hint = f" Close matches: {', '.join(close)}." if close else ""
        raise KeyError(f"unknown dataset {dataset_id!r}.{hint} Use list_datasets to see all.")
    return dataset


def all_datasets() -> tuple[Dataset, ...]:
    return DATASETS


def domains() -> dict[str, list[Dataset]]:
    grouped: dict[str, list[Dataset]] = {}
    for dataset in DATASETS:
        grouped.setdefault(dataset.domain, []).append(dataset)
    return grouped


def table_count() -> int:
    return sum(len(dataset.tables) for dataset in DATASETS)

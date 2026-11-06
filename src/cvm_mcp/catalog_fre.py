"""Catalog of the 36 CSV tables published in CVM's FRE yearly archives.

CVM ships one zip per year at
``DOC/FRE/DADOS/fre_cia_aberta_{year}.zip``. Inside it there is one document
header table (``fre_cia_aberta_{year}.csv``) plus 35 detail tables named
``fre_cia_aberta_{slug}_{year}.csv``.

The R package ``GetFREData`` exposes the same tables with a ``cia_aberta_``
prefix (it strips only ``fre_`` and the year). Every entry here keeps that
legacy name as an alias, so callers can use either.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BASE_URL = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/FRE/DADOS/"


@dataclass(frozen=True)
class Table:
    slug: str
    source: str
    title: str
    description: str
    key_columns: tuple[str, ...] = ()
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def member(self, year: int) -> str:
        """Name of this table's CSV inside the year archive."""
        if self.source:
            return f"fre_cia_aberta_{self.source}_{int(year)}.csv"
        return f"fre_cia_aberta_{int(year)}.csv"

    def as_dict(self, *, detail: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "table": self.slug,
            "title": self.title,
            "description": self.description,
        }
        if self.aliases:
            payload["aliases"] = [legacy_name(self), *self.aliases]
        else:
            payload["aliases"] = [legacy_name(self)]
        if detail and self.key_columns:
            payload["key_columns"] = list(self.key_columns)
        return payload


TABLES: tuple[Table, ...] = (
    Table(
        "documento",
        "",
        "Document header",
        "One row per FRE filing: company, reference date, version, receipt date and the CVM link.",
        (
            "cd_cvm",
            "cnpj",
            "nome_companhia",
            "data_referencia",
            "versao",
            "id_documento",
            "data_recebimento",
        ),
        aliases=("cia_aberta",),
    ),
    Table(
        "acao_entregue",
        "acao_entregue",
        "Shares delivered",
        "Shares delivered or transferred during the reference period.",
        ("nome_companhia", "data_referencia", "versao"),
    ),
    Table(
        "administrador_pcd",
        "administrador_PCD",
        "Administrators with disabilities",
        "Administrators declared as persons with a disability (PCD).",
        ("nome", "cargo_eletivo_ocupado", "data_referencia"),
    ),
    Table(
        "administrador_declaracao_genero",
        "administrador_declaracao_genero",
        "Administrator gender declaration",
        "Gender breakdown of the company's administrators.",
        ("nome", "genero", "data_referencia"),
    ),
    Table(
        "administrador_declaracao_raca",
        "administrador_declaracao_raca",
        "Administrator race declaration",
        "Race/colour breakdown of the company's administrators.",
        ("nome", "raca", "data_referencia"),
    ),
    Table(
        "administrador_membro_conselho_fiscal",
        "administrador_membro_conselho_fiscal",
        "Administrators and fiscal council",
        "Board members, officers and fiscal council members: role, election, term and background.",
        (
            "orgao_administracao",
            "nome",
            "cpf",
            "profissao",
            "cargo_eletivo_ocupado",
            "data_eleicao",
            "data_posse",
            "eleito_controlador",
        ),
    ),
    Table(
        "auditor",
        "auditor",
        "Independent auditor",
        "Auditor identity, contract dates, services provided, fees and replacement reason.",
        (
            "auditor",
            "cnpj_auditor",
            "data_inicio_contratacao",
            "data_fim_contratacao",
            "servico_contratado",
            "remuneracao_auditor",
        ),
    ),
    Table(
        "capital_social",
        "capital_social",
        "Share capital",
        "Issued/subscribed capital events, amounts and share counts.",
        (
            "tipo_capital",
            "data_autorizacao_aprovacao",
            "valor_capital",
            "quantidade_acoes_ordinarias",
            "quantidade_acoes_preferenciais",
            "quantidade_total_acoes",
        ),
    ),
    Table(
        "capital_social_classe_acao",
        "capital_social_classe_acao",
        "Share capital by class",
        "Share counts per capital event and preferred-share class.",
        ("id_capital_social", "tipo_classe_acao_preferencial", "quantidade_acoes"),
    ),
    Table(
        "capital_social_titulo_conversivel",
        "capital_social_titulo_conversivel",
        "Convertible securities",
        "Convertible securities linked to the share capital.",
        ("tipo_titulo", "quantidade_titulos", "data_referencia"),
    ),
    Table(
        "distribuicao_capital",
        "distribuicao_capital",
        "Capital distribution",
        "Number of individual, corporate and institutional shareholders and free-float shares.",
        (
            "quantidade_acionistas_pf",
            "quantidade_acionistas_pj",
            "quantidade_acionistas_investidores_institucionais",
            "quantidade_total_acoes_circulacao",
            "percentual_total_acoes_circulacao",
        ),
    ),
    Table(
        "distribuicao_capital_classe_acao",
        "distribuicao_capital_classe_acao",
        "Capital distribution by class",
        "Free-float distribution per share class.",
        ("tipo_classe_acao_preferencial", "quantidade_acoes", "percentual_acoes"),
    ),
    Table(
        "empregado_pcd",
        "empregado_PCD",
        "Employees with disabilities",
        "Employees declared as persons with a disability (PCD).",
        ("quantidade_empregados", "data_referencia"),
    ),
    Table(
        "empregado_local_declaracao_genero",
        "empregado_local_declaracao_genero",
        "Employees by location and gender",
        "Employee headcount by location and gender.",
        ("local", "genero", "quantidade_empregados"),
    ),
    Table(
        "empregado_local_declaracao_raca",
        "empregado_local_declaracao_raca",
        "Employees by location and race",
        "Employee headcount by location and race/colour.",
        ("local", "raca", "quantidade_empregados"),
    ),
    Table(
        "empregado_local_faixa_etaria",
        "empregado_local_faixa_etaria",
        "Employees by location and age",
        "Employee headcount by location and age bracket.",
        ("local", "faixa_etaria", "quantidade_empregados"),
    ),
    Table(
        "empregado_posicao_declaracao_genero",
        "empregado_posicao_declaracao_genero",
        "Employees by position and gender",
        "Employee headcount by position and gender.",
        ("posicao", "genero", "quantidade_empregados"),
    ),
    Table(
        "empregado_posicao_declaracao_raca",
        "empregado_posicao_declaracao_raca",
        "Employees by position and race",
        "Employee headcount by position and race/colour.",
        ("posicao", "raca", "quantidade_empregados"),
    ),
    Table(
        "empregado_posicao_faixa_etaria",
        "empregado_posicao_faixa_etaria",
        "Employees by position and age",
        "Employee headcount by position and age bracket.",
        ("posicao", "faixa_etaria", "quantidade_empregados"),
    ),
    Table(
        "empregado_posicao_local",
        "empregado_posicao_local",
        "Employees by position and location",
        "Employee headcount by position and location.",
        ("posicao", "local", "quantidade_empregados"),
    ),
    Table(
        "membro_comite",
        "membro_comite",
        "Committee members",
        "Committee membership: committee type, role, election and term.",
        ("nome", "cpf", "tipo_comite", "cargo_ocupado", "data_eleicao"),
    ),
    Table(
        "mercado_estrangeiro",
        "mercado_estrangeiro",
        "Foreign market",
        "Securities listed or traded on foreign markets.",
        ("pais", "mercado", "codigo_negociacao", "data_referencia"),
    ),
    Table(
        "outro_valor_mobiliario",
        "outro_valor_mobiliario",
        "Other securities",
        "Other securities issued by the company.",
        ("tipo_valor_mobiliario", "quantidade", "data_referencia"),
    ),
    Table(
        "participacao_sociedade",
        "participacao_sociedade",
        "Equity interests",
        "Interests held by the company in other companies: stake, market and book value.",
        (
            "razao_social",
            "cnpj",
            "tipo_sociedade",
            "participacao_emissor",
            "valor_mercado",
            "valor_contabil",
        ),
    ),
    Table(
        "posicao_acionaria",
        "posicao_acionaria",
        "Shareholder position",
        "Shareholders, type (individual/corporate), shares held, ownership percentages and controller flag.",
        (
            "acionista",
            "tipo_pessoa_acionista",
            "cpf_cnpj_acionista",
            "quantidade_total_acoes_circulacao",
            "percentual_total_acoes_circulacao",
            "acionista_controlador",
            "nacionalidade",
            "sigla_uf",
            "residente_exterior",
        ),
    ),
    Table(
        "posicao_acionaria_classe_acao",
        "posicao_acionaria_classe_acao",
        "Shareholder position by class",
        "Shares held by each shareholder, broken down by preferred-share class.",
        ("id_acionista", "tipo_classe_acao_preferencial", "quantidade_acoes", "percentual_acoes"),
    ),
    Table(
        "relacao_familiar",
        "relacao_familiar",
        "Family relationships",
        "Family relationships among administrators.",
        ("nome", "nome_familiar", "tipo_relacao", "data_referencia"),
    ),
    Table(
        "relacao_subordinacao",
        "relacao_subordinacao",
        "Subordination relationships",
        "Subordination, control or affiliation relationships.",
        ("nome", "nome_relacionado", "tipo_relacao", "data_referencia"),
    ),
    Table(
        "remuneracao_acao",
        "remuneracao_acao",
        "Share-based compensation",
        "Compensation linked to shares.",
        ("orgao_administracao", "quantidade_acoes", "valor", "data_referencia"),
    ),
    Table(
        "remuneracao_maxima_minima_media",
        "remuneracao_maxima_minima_media",
        "Compensation: max, min and average",
        "Highest, lowest and average compensation by governing body.",
        (
            "orgao_administracao",
            "numero_membros",
            "valor_maior_remuneracao",
            "valor_menor_remuneracao",
            "valor_medio_remuneracao",
        ),
    ),
    Table(
        "remuneracao_total_orgao",
        "remuneracao_total_orgao",
        "Compensation by body",
        "Total compensation by governing body, split into salary, bonus, committees and other amounts.",
        (
            "orgao_administracao",
            "numero_membros",
            "numero_membros_remunerados",
            "total_remuneracao_orgao",
            "salario",
            "bonus",
            "participacao_resultados",
            "outros_valores_fixos",
            "outros_valores_variaveis",
        ),
    ),
    Table(
        "remuneracao_variavel",
        "remuneracao_variavel",
        "Variable compensation",
        "Bonus and profit-sharing ranges and effective amounts by governing body.",
        (
            "orgao_administracao",
            "quantidade_total_membros",
            "bonus_valor_efetivo",
            "participacao_valor_efetivo",
        ),
    ),
    Table(
        "responsavel",
        "responsavel",
        "Responsible officers",
        "Officers responsible for the FRE filing.",
        ("nome_responsavel", "cargo_responsavel"),
    ),
    Table(
        "titular_valor_mobiliario",
        "titular_valor_mobiliario",
        "Security holders",
        "Holders of securities issued by the company.",
        ("nome", "cpf_cnpj", "tipo_valor_mobiliario", "quantidade", "data_referencia"),
    ),
    Table(
        "titulo_exterior",
        "titulo_exterior",
        "Securities issued abroad",
        "Securities issued or traded abroad.",
        ("pais", "tipo_titulo", "quantidade", "data_referencia"),
    ),
    Table(
        "transacao_parte_relacionada",
        "transacao_parte_relacionada",
        "Related-party transactions",
        "Transactions with related parties: counterparty, amount and nature.",
        ("parte_relacionada", "tipo_transacao", "valor", "data_referencia"),
    ),
)

BY_SLUG: dict[str, Table] = {table.slug: table for table in TABLES}
BY_NAME: dict[str, Table] = {}


def legacy_name(table: Table) -> str:
    """GetFREData's list name: ``fre_`` and the year stripped, ``cia_aberta_`` kept."""
    return "cia_aberta" + (f"_{table.source}" if table.source else "")


for _table in TABLES:
    BY_NAME[_table.slug] = _table
    BY_NAME[_table.slug.replace("_", "-")] = _table
    _legacy = legacy_name(_table)
    BY_NAME[_legacy] = _table
    BY_NAME[_legacy.replace("_", "-")] = _table
    for _alias in _table.aliases:
        BY_NAME[_alias] = _table
        BY_NAME[_alias.replace("_", "-")] = _table


class UnknownTable(KeyError):
    """The requested table is not part of the FRE catalog."""


def get_table(name: str) -> Table:
    """Resolve a slug or a GetFREData ``cia_aberta_*`` alias."""
    key = str(name).strip().lower()
    table = BY_NAME.get(key)
    if table is not None:
        return table
    close = [slug for slug in BY_SLUG if key in slug or slug in key][:5]
    hint = f" Close matches: {', '.join(close)}." if close else ""
    raise UnknownTable(f"unknown FRE table {name!r}.{hint} Use list_tables to see all 36.")


def all_tables() -> tuple[Table, ...]:
    return TABLES


def count() -> int:
    return len(TABLES)

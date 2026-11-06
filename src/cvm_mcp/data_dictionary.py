"""Curated column meanings for the tables agents query most.

CVM's own headers are terse and sometimes ambiguous (``VL_CONTA`` vs
``VL_CONTA_BRL``, ``ORDEM_EXERC``, FRE percentages).  ``describe_dataset`` can
list column *names* for free, but names alone are not enough to write a correct
``run_query``.  This module keeps a small, reviewed dictionary next to the
registry so the generic SQL layer and the curated tools share one definition.

Only the columns that actually need an explanation are listed; anything else is
a self-describing CVM field.  ``describe_columns`` preserves the caller's column
order so the output reads like the table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ColumnInfo:
    description: str
    unit: str | None = None
    notes: str | None = None

    def as_dict(self, name: str) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": name, "description": self.description}
        if self.unit:
            payload["unit"] = self.unit
        if self.notes:
            payload["notes"] = self.notes
        return payload


def _info(
    description: str, *, unit: str | None = None, notes: str | None = None
) -> ColumnInfo:
    return ColumnInfo(description=description, unit=unit, notes=notes)


# ---------------------------------------------------------------------------
# Columns shared by several datasets

COMMON: dict[str, ColumnInfo] = {
    "cnpj": _info(
        "CNPJ as 14 digits (punctuation removed by the server).",
        notes="Join key across datasets; the original CVM column may be formatted.",
    ),
    "cnpj_cia": _info(
        "CNPJ of the company as published (punctuated).",
        notes="Prefer `cnpj` for joins.",
    ),
    "cd_cvm": _info(
        "CVM company code (integer).",
        notes="Primary key for the corporate tools; resolve names/tickers with search_company.",
    ),
    "denom_social": _info("Company or fund legal name."),
    "nome_companhia": _info("Company legal name as filed."),
    "versao": _info(
        "Filing version; higher numbers are later amendments.",
        notes="FRE tools default to the last version per company/reference date.",
    ),
    "data_referencia": _info(
        "Reference date of the filing (usually the fiscal year-end).", unit="date"
    ),
    "data_recebimento": _info("Date CVM received the filing.", unit="date"),
    "id_documento": _info(
        "FRE document id.",
        notes="Joins the detail tables to `fre__documento`.",
    ),
    "_period": _info(
        "Cache partition the row came from.",
        notes="YYYY for yearly data, YYYYMM for monthly data, 'all' for period-less datasets.",
    ),
}


# ---------------------------------------------------------------------------
# DFP / ITR share the canonical statement schema (every doc/format table)

STATEMENTS: dict[str, ColumnInfo] = {
    "CNPJ_CIA": _info(
        "CNPJ of the company as published.", notes="Prefer `cnpj` for joins."
    ),
    "CD_CVM": _info("CVM company code."),
    "DENOM_CIA": _info("Company legal name."),
    "DT_REFER": _info("Reference date of the filing (fiscal year-end).", unit="date"),
    "DT_INI_EXERC": _info(
        "Start of the income-statement period.",
        unit="date",
        notes="Blank on balance-sheet rows, which are snapshots.",
    ),
    "DT_FIM_EXERC": _info("End of the income-statement period.", unit="date"),
    "VERSAO": _info(
        "Filing version.",
        notes="The loader keeps the highest version per account (amended filings).",
    ),
    "GRUPO_DFP": _info(
        "Statement group label, e.g. 'DF Consolidado - Demonstração do Resultado'."
    ),
    "MOEDA": _info("Reporting currency ('REAL')."),
    "ESCALA_MOEDA": _info(
        "Scale applied to VL_CONTA.",
        notes="'MIL' means thousands, 'UNIDADE' means units.",
    ),
    "ORDEM_EXERC": _info(
        "Exercise order: ULTIMO (current period) or ANTERIOR (comparative).",
        notes="The loader keeps only ULTIMO; compare years by reading separate partitions.",
    ),
    "CD_CONTA": _info(
        "Account code.",
        notes="1 = assets, 2 = liabilities/equity, 3 = income, 6 = cash flow, 7 = value added.",
    ),
    "DS_CONTA": _info(
        "Account label.",
        notes="Authoritative: banks and insurers reuse codes with different labels.",
    ),
    "VL_CONTA": _info(
        "Value exactly as published, in ESCALA_MOEDA units (usually thousands).",
        notes="Do not report directly; use VL_CONTA_BRL.",
    ),
    "VL_CONTA_BRL": _info(
        "Value in BRL, already scaled by ESCALA_MOEDA.",
        unit="BRL",
        notes="This is the number to report.",
    ),
    "COLUNA_DF": _info("Statement column label, e.g. 'Último exercício'."),
    "ACCOUNT_DEPTH": _info(
        "Nesting depth of CD_CONTA (0 = total line).",
        notes="Use <= 1 to keep only totals/headline accounts.",
    ),
    "FISCAL_YEAR": _info("Fiscal year derived from DT_REFER.", unit="year"),
    "PERIOD": _info(
        "Income-statement period type.",
        notes="annual | ytd | quarter | snapshot. ITR files publish YTD and quarter-only rows.",
    ),
    "QUARTER_LABEL": _info("ITR quarter label, e.g. '2T2025'; blank for DFP."),
    "doc": _info("Document: BPA, BPP, DRE, DFC_MD, DFC_MI, DMPL or DVA."),
    "fmt": _info(
        "Statement format: 'con' = consolidated, 'ind' = individual (parent only)."
    ),
}


# ---------------------------------------------------------------------------
# Dataset-level columns.  Only for datasets whose tables share a schema.

DATASET_COLUMNS: dict[str, dict[str, ColumnInfo]] = {
    "dfp": STATEMENTS,
    "itr": STATEMENTS,
    "fidc_inf_mensal": {
        "cnpj_fundo_classe": _info("FIDC class CNPJ."),
        "dt_comptc": _info("Accounting date.", unit="date"),
        "denom_social": _info("Fund or class name."),
    },
}


# ---------------------------------------------------------------------------
# Table-level columns: the precise list for each table.

TABLE_COLUMNS: dict[tuple[str, str], dict[str, ColumnInfo]] = {
    # -- registry / market -------------------------------------------
    ("cad_cia_aberta", "companhias"): {
        "sit": _info("Registry status, e.g. 'ATIVO' or 'CANCELADO'."),
        "setor_ativ": _info(
            "CVM sector classification.",
            notes="Useful for peer/sector aggregation.",
        ),
        "tp_merc": _info("Market type."),
        "categ_reg": _info("Registration category."),
        "controle_acionario": _info("Ownership/control type."),
        "dt_reg": _info("Registration date.", unit="date"),
        "dt_cancel": _info("Cancellation date, if any.", unit="date"),
        "mun": _info("City."),
        "uf": _info("State."),
        "auditor": _info("Auditor name."),
    },
    ("fca", "valor_mobiliario"): {
        "cnpj_companhia": _info("Company CNPJ as published."),
        "nome_empresarial": _info("Company legal name."),
        "valor_mobiliario": _info(
            "Security type, e.g. 'Ação Ordinária' or 'Debênture'."
        ),
        "codigo_negociacao": _info(
            "Exchange ticker, e.g. PETR4.",
            notes="Blank for unlisted securities.",
        ),
        "mercado": _info("Trading market, e.g. 'BOLSA' or 'BALCÃO'."),
        "segmento": _info("B3 listing segment, e.g. 'Novo Mercado'."),
        "sigla_classe_acao_preferencial": _info(
            "Preferred-share class, when applicable."
        ),
        "data_inicio_negociacao": _info(
            "First trading date of the security.", unit="date"
        ),
        "data_fim_negociacao": _info(
            "Last trading date, if the security was delisted.", unit="date"
        ),
    },
    ("vlmo", "transacoes"): {
        "data_movimentacao": _info("Trade date.", unit="date"),
        "tipo_empresa": _info("Issuer category in the filing."),
        "empresa": _info("Company that filed the position."),
        "tipo_cargo": _info("Insider role, e.g. 'Diretor', 'Conselheiro'."),
        "tipo_movimentacao": _info("Movement type (purchase, sale, exercise, etc.)."),
        "descricao_movimentacao": _info("Free-text description of the movement."),
        "tipo_operacao": _info("Operation code as published."),
        "tipo_ativo": _info("Security type, e.g. 'Ação', 'Debênture'."),
        "caracteristica_valor_mobiliario": _info("Security class/details."),
        "intermediario": _info("Broker that executed the trade."),
        "quantidade": _info("Quantity traded.", unit="shares"),
        "preco_unitario": _info("Unit price.", unit="BRL"),
        "volume": _info("Total trade value.", unit="BRL"),
    },
    ("cgvn", "praticas"): {
        "id_item": _info("ICBGC item id."),
        "capitulo": _info("Chapter of the governance code."),
        "principio": _info("Governance principle."),
        "pratica_recomendada": _info("Recommended practice text."),
        "pratica_adotada": _info(
            "Whether the company adopted the practice.",
            notes="Explain-or-comply; read alongside `explicacao`.",
        ),
        "explicacao": _info("Company explanation when the practice was not adopted."),
    },
    # -- FRE ---------------------------------------------------------
    ("fre", "documento"): {
        "categ_doc": _info("FRE document category/version label."),
        "link_documento": _info("URL of the filed document."),
    },
    ("fre", "posicao_acionaria"): {
        "id_acionista": _info("Shareholder id inside the filing."),
        "acionista": _info("Shareholder name."),
        "tipo_pessoa_acionista": _info("Individual or legal entity."),
        "cpf_cnpj_acionista": _info("Shareholder CPF/CNPJ."),
        "acionista_relacionado": _info("Related shareholder, when applicable."),
        "quantidade_acao_ordinaria_circulacao": _info(
            "Ordinary shares held.", unit="shares"
        ),
        "percentual_acao_ordinaria_circulacao": _info(
            "Ordinary shares held.", unit="percent"
        ),
        "quantidade_acao_preferencial_circulacao": _info(
            "Preferred shares held.", unit="shares"
        ),
        "percentual_acao_preferencial_circulacao": _info(
            "Preferred shares held.", unit="percent"
        ),
        "quantidade_total_acoes_circulacao": _info("Total shares held.", unit="shares"),
        "percentual_total_acoes_circulacao": _info(
            "Total shares held.", unit="percent"
        ),
        "acionista_controlador": _info("Whether the shareholder is the controller."),
        "participante_acordo_acionistas": _info(
            "Whether the shareholder is party to a shareholders' agreement."
        ),
        "data_composicao_capital_social": _info(
            "Date of the ownership snapshot.", unit="date"
        ),
        "data_ultima_alteracao": _info("Last change date.", unit="date"),
    },
    ("fre", "capital_social"): {
        "tipo_capital": _info("Capital type (authorised/issued)."),
        "valor_capital": _info("Share capital.", unit="BRL"),
        "data_autorizacao_aprovacao": _info("Approval date.", unit="date"),
        "quantidade_acoes_ordinarias": _info("Ordinary shares.", unit="shares"),
        "quantidade_acoes_preferenciais": _info("Preferred shares.", unit="shares"),
        "quantidade_total_acoes": _info("Total shares.", unit="shares"),
    },
    ("fre", "remuneracao_total_orgao"): {
        "orgao_administracao": _info(
            "Management body (board, executive board, fiscal council)."
        ),
        "total_remuneracao": _info("Total compensation for the year.", unit="BRL"),
        "total_remuneracao_orgao": _info(
            "Total compensation for this body.", unit="BRL"
        ),
        "numero_membros": _info("Number of members.", unit="count"),
        "numero_membros_remunerados": _info("Number of paid members.", unit="count"),
        "salario": _info("Salary.", unit="BRL"),
        "beneficios_diretos_indiretos": _info(
            "Direct and indirect benefits.", unit="BRL"
        ),
        "participacoes_comites": _info("Committee fees.", unit="BRL"),
        "bonus": _info("Bonus.", unit="BRL"),
        "participacao_resultados": _info("Profit sharing.", unit="BRL"),
    },
    ("fre", "endividamento"): {
        "passivo": _info("Liabilities.", unit="BRL"),
        "indice_endividamento": _info("Indebtedness ratio.", unit="ratio"),
        "tipo_indice": _info("Which ratio definition was used."),
    },
    ("fre", "obrigacao"): {
        "tipo_obrigacao": _info("Debt type."),
        "tipo_garantia": _info("Guarantee type."),
        "divida_inferior_1_ano": _info("Debt due within one year.", unit="BRL"),
        "divida_1a3_anos": _info("Debt due in 1-3 years.", unit="BRL"),
        "divida_3a5_anos": _info("Debt due in 3-5 years.", unit="BRL"),
        "divida_superior_5_anos": _info("Debt due after five years.", unit="BRL"),
        "divida_total": _info("Total debt.", unit="BRL"),
    },
    ("fre", "distribuicao_dividendos"): {
        "lucro_liquido_ajustado": _info("Adjusted net income.", unit="BRL"),
        "taxa_retorno_patrimonio_liquido_emissor": _info(
            "Return on equity.", unit="percent"
        ),
        "lucro_liquido_retido": _info("Retained earnings.", unit="BRL"),
        "dividendo_distribuido_total": _info(
            "Total dividends distributed.", unit="BRL"
        ),
        "dividendo_distribuido_lucro_liquido_ajustado": _info(
            "Dividend payout ratio.", unit="percent"
        ),
        "data_aprovacao_retencao": _info(
            "Approval date of the retention.", unit="date"
        ),
    },
    ("fre", "transacao_parte_relacionada"): {
        "parte_relacionada": _info("Related party name."),
        "relacao_emissor": _info("Relationship with the issuer."),
        "data_transacao": _info("Transaction date.", unit="date"),
        "objeto_contrato": _info("Contract subject."),
        "montante_envolvido": _info("Amount involved.", unit="BRL"),
        "saldo_existente": _info("Outstanding balance.", unit="BRL"),
        "natureza_razao_operacao": _info("Nature/reason of the transaction."),
    },
    ("fre", "informacao_financeira"): {
        "codigo_conta": _info("Account code."),
        "descricao_conta": _info("Account label."),
        "valor": _info(
            "Reported value, in ESCALA_MOEDA units.",
            notes="Check tipo_moeda/escala_moeda.",
        ),
        "informacao_financeira": _info("Which statement the line belongs to."),
        "tipo_moeda": _info("Currency."),
        "escala_moeda": _info("Scale of `valor`."),
    },
    ("fre", "plano_recompra"): {
        "data_deliberacao": _info("Approval date.", unit="date"),
        "data_inicio_recompra": _info("Buyback start date.", unit="date"),
        "data_fim_recompra": _info("Buyback end date.", unit="date"),
        "valor_reserva_disponivel_recompra": _info(
            "Reserve available for the buyback.", unit="BRL"
        ),
    },
    ("fre", "valor_mobiliario_tesouraria_movimentacao"): {
        "quantidade_inicial": _info("Opening treasury position.", unit="shares"),
        "valor_total_inicial": _info("Opening treasury value.", unit="BRL"),
        "quantidade_adquirida": _info("Shares acquired.", unit="shares"),
        "valor_total_adquirido": _info("Value acquired.", unit="BRL"),
        "quantidade_alienada": _info("Shares disposed of.", unit="shares"),
        "valor_total_alienado": _info("Value disposed of.", unit="BRL"),
    },
    ("fre", "administrador_membro_conselho_fiscal"): {
        "nome": _info("Member name."),
        "cpf": _info("Member CPF."),
        "cargo_eletivo_ocupado": _info("Position held."),
        "data_eleicao": _info("Election date.", unit="date"),
        "data_posse": _info("Inauguration date.", unit="date"),
        "prazo_mandato": _info("Term length."),
        "eleito_controlador": _info("Whether elected by the controller."),
        "percentual_participacao_reunioes": _info(
            "Meeting attendance.", unit="percent"
        ),
    },
    ("fre", "auditor"): {
        "auditor": _info("Audit firm name."),
        "cnpj_auditor": _info("Audit firm CNPJ."),
        "data_inicio_contratacao": _info("Contract start.", unit="date"),
        "data_fim_contratacao": _info("Contract end.", unit="date"),
        "servico_contratado": _info("Service contracted."),
        "remuneracao_auditor": _info("Audit fees.", unit="BRL"),
    },
    # -- funds -------------------------------------------------------
    ("fi_cad", "classe"): {
        "cnpj_classe": _info(
            "Fund class CNPJ (14 digits).", notes="Primary key for fund tools."
        ),
        "id_registro_fundo": _info("Fund registration id."),
        "id_registro_classe": _info("Class registration id."),
        "codigo_cvm": _info("CVM fund/class code."),
        "tipo_classe": _info(
            "Instrument family, e.g. 'Classes de Cotas de Fundos FIF'.",
            notes="Fund tools use this to route FIF/FII/FIDC/FIAGRO.",
        ),
        "denominacao_social": _info("Fund or class legal name."),
        "situacao": _info("Registration status."),
        "classificacao": _info("CVM classification."),
        "classe_cotas": _info("Share-class type."),
        "publico_alvo": _info("Target investor profile."),
        "patrimonio_liquido": _info(
            "Net asset value at the reference date.", unit="BRL"
        ),
        "data_patrimonio_liquido": _info(
            "Reference date of the net asset value.", unit="date"
        ),
        "cnpj_administrador": _info("Administrator CNPJ."),
        "administrador": _info("Administrator name."),
        "cnpj_custodiante": _info("Custodian CNPJ."),
        "custodiante": _info("Custodian name."),
        "cnpj_auditor": _info("Auditor CNPJ."),
        "auditor": _info("Auditor name."),
    },
    ("fi_inf_diario", "informe"): {
        "cnpj_fundo_classe": _info("Fund class CNPJ."),
        "dt_comptc": _info("Accounting date of the report.", unit="date"),
        "tp_fundo_classe": _info("Fund/class type."),
        "id_subclasse": _info("Subclass id, when applicable."),
        "vl_total": _info("Total portfolio value.", unit="BRL"),
        "vl_quota": _info("Quota price.", unit="BRL"),
        "vl_patrim_liq": _info("Net asset value (PL).", unit="BRL"),
        "captc_dia": _info("Subscriptions on the day.", unit="BRL"),
        "resg_dia": _info("Redemptions on the day.", unit="BRL"),
        "nr_cotst": _info("Number of unitholders.", unit="count"),
    },
    ("fii_inf_mensal", "geral"): {
        "cnpj_fundo_classe": _info("FII class CNPJ."),
        "nome_fundo_classe": _info("Fund name."),
        "segmento_atuacao": _info(
            "Real-estate segment, e.g. 'Shoppings', 'Logística'."
        ),
        "mandato": _info("Fund mandate."),
        "tipo_gestao": _info("Management type (active/passive)."),
        "publico_alvo": _info("Target investor profile."),
        "codigo_isin": _info("ISIN, when informed."),
        "quantidade_cotas_emitidas": _info("Shares issued.", unit="count"),
        "mercado_negociacao_bolsa": _info("Whether the fund trades on an exchange."),
    },
    ("fii_inf_mensal", "complemento"): {
        "cnpj_fundo_classe": _info("FII class CNPJ."),
        "total_numero_cotistas": _info("Number of unitholders.", unit="count"),
        "valor_ativo": _info("Total assets.", unit="BRL"),
        "patrimonio_liquido": _info("Net asset value.", unit="BRL"),
        "cotas_emitidas": _info("Shares issued.", unit="count"),
        "valor_patrimonial_cotas": _info("Book value per share.", unit="BRL"),
        "percentual_rentabilidade_efetiva_mes": _info(
            "Monthly effective return.", unit="percent"
        ),
        "percentual_rentabilidade_patrimonial_mes": _info(
            "Monthly book return.", unit="percent"
        ),
        "percentual_dividend_yield_mes": _info(
            "Monthly dividend yield.", unit="percent"
        ),
        "percentual_amortizacao_cotas_mes": _info(
            "Monthly amortisation.", unit="percent"
        ),
        "percentual_despesas_taxa_administracao": _info(
            "Management fee.", unit="percent"
        ),
    },
    ("fii_inf_mensal", "ativo_passivo"): {
        "cnpj_fundo_classe": _info("FII class CNPJ."),
        "total_necessidades_liquidez": _info("Liquidity needs.", unit="BRL"),
        "disponibilidades": _info("Cash and equivalents.", unit="BRL"),
        "titulos_publicos": _info("Government securities.", unit="BRL"),
        "titulos_privados": _info("Corporate securities.", unit="BRL"),
        "imoveis_renda_acabados": _info("Completed rental properties.", unit="BRL"),
        "cri": _info("CRI holdings.", unit="BRL"),
        "cri_cra": _info("CRI/CRA holdings.", unit="BRL"),
        "acoes": _info("Equity holdings.", unit="BRL"),
        "total_investido": _info("Total invested assets.", unit="BRL"),
        "total_passivo": _info("Total liabilities.", unit="BRL"),
    },
    ("fidc_inf_mensal", "tab_i"): {
        "tab_i_vl_ativo": _info("Total assets (Table I).", unit="BRL"),
        "tab_i1_vl_disp": _info("Cash and equivalents.", unit="BRL"),
        "tab_i2_vl_carteira": _info("Credit portfolio.", unit="BRL"),
        "tab_i2a_vl_dircred_risco": _info(
            "Credit rights with issuer risk.", unit="BRL"
        ),
        "tab_i2a1_vl_cred_venc_ad": _info("Performing credit.", unit="BRL"),
        "tab_i2a2_vl_cred_venc_inad": _info("Overdue credit.", unit="BRL"),
        "tab_i2a3_vl_cred_inad": _info("Defaulted credit.", unit="BRL"),
        "tab_i2b_vl_dircred_sem_risco": _info(
            "Credit rights without issuer risk.", unit="BRL"
        ),
        "tab_i2c_vl_vlmob": _info("Securities holdings.", unit="BRL"),
        "tab_i3_vl_posicao_deriv": _info("Derivative position.", unit="BRL"),
        "tab_i4_vl_outro_ativo": _info("Other assets.", unit="BRL"),
        "tab_i4a_vl_cprazo": _info("Short-term receivables.", unit="BRL"),
        "tab_i4b_vl_lprazo": _info("Long-term receivables.", unit="BRL"),
    },
    ("fidc_inf_mensal", "tab_ii"): {
        "tab_ii_vl_carteira": _info("Portfolio by segment (Table II).", unit="BRL"),
        "tab_ii_a_vl_indust": _info("Industrial credit.", unit="BRL"),
        "tab_ii_b_vl_imobil": _info("Real-estate credit.", unit="BRL"),
        "tab_ii_c_vl_comerc": _info("Retail/commercial credit.", unit="BRL"),
        "tab_ii_d_vl_serv": _info("Services credit.", unit="BRL"),
        "tab_ii_e_vl_agroneg": _info("Agribusiness credit.", unit="BRL"),
        "tab_ii_f_vl_financ": _info("Financial credit.", unit="BRL"),
        "tab_ii_g_vl_credito": _info("Credit-card receivables.", unit="BRL"),
        "tab_ii_h_vl_factor": _info("Factoring receivables.", unit="BRL"),
        "tab_ii_i_vl_setor_publico": _info("Public-sector receivables.", unit="BRL"),
        "tab_ii_j_vl_judicial": _info("Judicial receivables.", unit="BRL"),
        "tab_ii_k_vl_marca": _info("Brand/trademark receivables.", unit="BRL"),
    },
    ("fidc_inf_mensal", "tab_iv"): {
        "tab_iv_a_vl_pl": _info("Net asset value (Table IV).", unit="BRL"),
        "tab_iv_b_vl_pl_medio": _info("Average net asset value.", unit="BRL"),
    },
    ("fiagro_inf_mensal", "informe"): {
        "cnpj_classe": _info("FIAGRO class CNPJ."),
        "nome_classe": _info("Class name."),
        "numero_cotistas": _info("Number of unitholders.", unit="count"),
        "valor_ativo": _info("Total assets.", unit="BRL"),
        "patrimonio_liquido": _info("Net asset value.", unit="BRL"),
        "cotas_emitidas": _info("Shares issued.", unit="count"),
        "valor_patrimonial_cotas": _info("Book value per share.", unit="BRL"),
        "rentabilidade_efetiva_mes": _info("Monthly effective return.", unit="percent"),
        "rentabilidade_patrimonial_mes": _info("Monthly book return.", unit="percent"),
        "dividend_yield_mes": _info("Monthly dividend yield.", unit="percent"),
        "percentual_despesas_taxa_administracao": _info(
            "Management fee.", unit="percent"
        ),
        "total_investido": _info("Total invested assets.", unit="BRL"),
        "total_passivo": _info("Total liabilities.", unit="BRL"),
    },
    # -- offerings ---------------------------------------------------
    ("oferta", "distribuicao"): {
        "numero_processo": _info("CVM process number."),
        "nome_emissor": _info("Issuer name."),
        "cnpj_emissor": _info("Issuer CNPJ."),
        "numero_registro_oferta": _info("Offering registration number."),
        "tipo_oferta": _info("Offering type (primary/secondary/mixed)."),
        "tipo_ativo": _info("Security type, e.g. 'Debêntures', 'Ações'."),
        "rito_oferta": _info("Registration rite."),
        "modalidade_oferta": _info("Offering modality."),
        "cnpj_lider": _info("Lead underwriter CNPJ."),
        "nome_lider": _info("Lead underwriter name."),
        "data_registro_oferta": _info("Registration date.", unit="date"),
        "data_inicio_oferta": _info("Offering start date.", unit="date"),
        "data_encerramento_oferta": _info("Offering end date.", unit="date"),
        "quantidade_total": _info("Total securities offered.", unit="count"),
        "preco_unitario": _info("Unit price.", unit="BRL"),
        "valor_total": _info("Total offering value.", unit="BRL"),
    },
    ("oferta", "resolucao_160"): {
        "numero_processo": _info("CVM process number."),
        "nome_emissor": _info("Issuer name."),
        "cnpj_emissor": _info("Issuer CNPJ."),
        "cnpj_lider": _info("Lead underwriter CNPJ."),
        "nome_lider": _info("Lead underwriter name."),
        "status_requerimento": _info("Status of the RCVM 160 request."),
        "valor_total_registrado": _info("Registered amount.", unit="BRL"),
        "destinacao_recursos": _info("Use of proceeds."),
        "mercado_negociacao": _info("Trading market."),
        "tipo_lastro": _info("Underlying asset/backing type."),
        "regime_fiduciario": _info("Fiduciary regime."),
    },
    # -- enforcement -------------------------------------------------
    ("sancionador", "processos"): {
        "nup": _info(
            "Unified process number (NUP).",
            notes="Join key with `sancionador__acusados`.",
        ),
        "objeto": _info("Subject of the proceeding."),
        "ementa": _info("Summary/headnote."),
        "data_abertura": _info("Opening date.", unit="date"),
        "fase_atual": _info("Current phase."),
        "subfase_atual": _info("Current sub-phase."),
        "local_atual": _info("Current location/unit."),
        "data_ultima_movimentacao": _info("Last movement date.", unit="date"),
    },
    ("sancionador", "acusados"): {
        "nup": _info(
            "Unified process number (NUP).",
            notes="Join key with `sancionador__processos`.",
        ),
        "nome_acusado": _info("Accused party name."),
        "situacao": _info("Current status of the accused party."),
        "data_situacao": _info("Date of the current status.", unit="date"),
    },
}


def _merged(dataset: str, table: str) -> dict[str, ColumnInfo]:
    merged = dict(COMMON)
    merged.update(DATASET_COLUMNS.get(dataset, {}))
    merged.update(TABLE_COLUMNS.get((dataset, table), {}))
    return merged


def describe_columns(
    dataset: str,
    table: str,
    columns: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Return curated descriptions for ``columns``, preserving their order.

    When ``columns`` is ``None`` (e.g. the table was never cached) every entry
    known for that dataset/table is returned instead.
    """
    merged = _merged(dataset, table)
    names = sorted(merged) if columns is None else [str(column) for column in columns]

    described: list[dict[str, Any]] = []
    for name in names:
        info = merged.get(name)
        if info is not None:
            described.append(info.as_dict(name))
    return described


def has_documentation(dataset: str, table: str) -> bool:
    """Whether this table has any curated column beyond the shared keys."""
    specific = set(DATASET_COLUMNS.get(dataset, {})) | set(
        TABLE_COLUMNS.get((dataset, table), {})
    )
    return bool(specific)


__all__ = ["ColumnInfo", "describe_columns", "has_documentation"]

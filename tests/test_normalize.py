"""Normalization rules: encoding, headers, CNPJ and type inference."""

from __future__ import annotations

import pandas as pd

from cvm_mcp.normalize import (
    apply_aliases,
    canonicalize,
    clean_names,
    cnpj_series,
    fold,
    normalize_cnpj,
    read_csv_bytes,
)


def test_clean_names():
    assert clean_names(["CNPJ_Companhia", "Data_Referencia", "VL_CONTA"]) == [
        "cnpj_companhia",
        "data_referencia",
        "vl_conta",
    ]
    assert clean_names(["a  b", "a__b"]) == ["a_b", "a_b"]


def test_apply_aliases_maps_the_pre_rcvm_175_headers():
    frame = pd.DataFrame(
        {"TP_FUNDO": ["FI"], "CNPJ_FUNDO": ["07593972000186"], "VL_QUOTA": [1.0]}
    )
    frame.columns = clean_names(frame.columns)
    result = apply_aliases(
        frame,
        (("tp_fundo", "tp_fundo_classe"), ("cnpj_fundo", "cnpj_fundo_classe")),
    )
    assert list(result.columns) == ["tp_fundo_classe", "cnpj_fundo_classe", "vl_quota"]
    assert result.loc[0, "cnpj_fundo_classe"] == "07593972000186"


def test_apply_aliases_prefers_the_new_header_when_both_exist():
    frame = pd.DataFrame(
        {"CNPJ_FUNDO": ["old"], "CNPJ_FUNDO_CLASSE": ["new"], "VL_QUOTA": [1.0]}
    )
    frame.columns = clean_names(frame.columns)
    result = apply_aliases(frame, (("cnpj_fundo", "cnpj_fundo_classe"),))
    assert list(result.columns) == ["cnpj_fundo_classe", "vl_quota"]
    assert result.loc[0, "cnpj_fundo_classe"] == "new"


def test_fold_is_accent_insensitive():
    assert fold("Petróleo Brasileiro S.A.") == "petroleo brasileiro s.a."
    assert fold("ITAÚ") == "itau"


def test_normalize_cnpj():
    assert normalize_cnpj("00.000.000/0001-91") == "00000000000191"
    assert normalize_cnpj("00000000000191") == "00000000000191"
    assert normalize_cnpj("123") is None
    assert normalize_cnpj(None) is None


def test_cnpj_series_keeps_only_valid_values():
    values = pd.Series(["00.000.000/0001-91", "123", None], dtype="string")
    result = cnpj_series(values)
    assert result.iloc[0] == "00000000000191"
    assert result.iloc[1] is pd.NA
    assert result.iloc[2] is pd.NA


def test_read_latin1_and_unbalanced_quotes():
    payload = (
        "Nome;Valor\n"
        'Titulos ("VJ);10\n'
        "Ação;20\n"
    ).encode("latin1")
    frame = read_csv_bytes(payload)
    assert len(frame) == 2
    assert frame.iloc[1, 0] == "Ação"


def test_canonicalize_types_and_identifiers():
    frame = pd.DataFrame(
        {
            "CNPJ_Classe": ["07593972000186", "42737077000199"],
            "Data_Referencia": ["2025-09-30", "2025-09-30"],
            "VL_PATRIM_LIQ": ["1000.50", "2000.00"],
            "Nome": ["Fundo A", "Fundo B"],
        }
    )
    out = canonicalize(frame, key_columns=("cnpj_classe", "data_referencia"))
    assert list(out.columns)[:2] == ["cnpj_classe", "data_referencia"]
    # A 14-digit identifier must stay text, never become a float.
    assert out["cnpj_classe"].iloc[0] == "07593972000186"
    assert pd.api.types.is_float_dtype(out["vl_patrim_liq"])
    assert pd.api.types.is_string_dtype(out["data_referencia"])
    assert out["data_referencia"].iloc[0] == "2025-09-30"


def test_canonicalize_keeps_short_codes_as_numbers():
    frame = pd.DataFrame({"CD_CVM": ["9512", "4170"], "Nome": ["Petrobras", "Vale"]})
    out = canonicalize(frame)
    assert pd.api.types.is_float_dtype(out["cd_cvm"])

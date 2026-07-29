"""Pure-Python tests for GS1 SSCC-18 check-digit/composition math (B4).

No Odoo. Run: pytest tests/test_gs1_sscc.py -q
"""
import pytest

from mml_edi.parsers.gs1_sscc import (
    GS1SSCCError,
    build_sscc18,
    gs1_mod10_check_digit,
    validate_sscc18,
)


# --- gs1_mod10_check_digit -------------------------------------------------

def test_check_digit_matches_real_nimbrel_desadv_pallet_sscc():
    """00502000000045350114 (AI 00 + 18-digit SSCC) is a valid GS1 mod-10
    SSCC test vector (internal-use 0200000-range company prefix) — verifies
    our algorithm produces a check digit consistent with a known-good SSCC
    of this shape, same structure as the Nimbrel_DESADV.pdf p.56 example."""
    sscc = "502000000045350114"
    payload, check = sscc[:-1], sscc[-1]
    assert gs1_mod10_check_digit(payload) == check


def test_check_digit_matches_real_nimbrel_desadv_carton_sscc():
    sscc = "602000000027682506"
    payload, check = sscc[:-1], sscc[-1]
    assert gs1_mod10_check_digit(payload) == check


def test_check_digit_second_carton_sscc():
    sscc = "602000000027682490"
    payload, check = sscc[:-1], sscc[-1]
    assert gs1_mod10_check_digit(payload) == check


def test_check_digit_rejects_non_numeric():
    with pytest.raises(GS1SSCCError):
        gs1_mod10_check_digit("12A456")


def test_check_digit_rejects_empty():
    with pytest.raises(GS1SSCCError):
        gs1_mod10_check_digit("")


def test_check_digit_known_vector():
    # Textbook GS1 mod-10 example: GTIN-13 '0036000291452' has a 12-digit
    # payload '003600029145' and check digit '2' — same algorithm as SSCC.
    assert gs1_mod10_check_digit("003600029145") == "2"


# --- build_sscc18 -----------------------------------------------------------

def test_build_sscc18_correct_length_and_structure():
    sscc = build_sscc18("0200000", 1)
    assert len(sscc) == 18
    assert sscc.isdigit()
    assert sscc.startswith("0" + "0200000")  # default extension digit '0'


def test_build_sscc18_is_valid():
    sscc = build_sscc18("0200000", 42)
    assert validate_sscc18(sscc) is True


def test_build_sscc18_serial_zero_padded():
    sscc = build_sscc18("0200000", 1)
    # extension(1) + prefix(7) + serial(9) + check(1) == 18
    serial_field = sscc[1 + 7:1 + 7 + 9]
    assert serial_field == "000000001"


def test_build_sscc18_deterministic_check_digit():
    a = build_sscc18("0200000", 555)
    b = build_sscc18("0200000", 555)
    assert a == b


def test_build_sscc18_different_serials_differ():
    a = build_sscc18("0200000", 1)
    b = build_sscc18("0200000", 2)
    assert a != b


def test_build_sscc18_custom_extension_digit():
    sscc = build_sscc18("0200000", 1, extension_digit="7")
    assert sscc.startswith("7" + "0200000")


def test_build_sscc18_max_serial_for_7digit_prefix():
    # 9 serial digits available -> max is 999999999.
    sscc = build_sscc18("0200000", 999999999)
    assert validate_sscc18(sscc) is True


def test_build_sscc18_serial_overflow_raises():
    with pytest.raises(GS1SSCCError):
        build_sscc18("0200000", 10 ** 9)  # 10 digits, one too many


def test_build_sscc18_rejects_non_numeric_prefix():
    with pytest.raises(GS1SSCCError):
        build_sscc18("94X9416", 1)


def test_build_sscc18_rejects_negative_serial():
    with pytest.raises(GS1SSCCError):
        build_sscc18("0200000", -1)


def test_build_sscc18_rejects_bad_extension_digit():
    with pytest.raises(GS1SSCCError):
        build_sscc18("0200000", 1, extension_digit="10")
    with pytest.raises(GS1SSCCError):
        build_sscc18("0200000", 1, extension_digit="")


def test_build_sscc18_rejects_prefix_too_long():
    # A 16-digit prefix leaves no room for extension + serial + check.
    with pytest.raises(GS1SSCCError):
        build_sscc18("9" * 16, 1)


# --- validate_sscc18 ---------------------------------------------------------

def test_validate_sscc18_rejects_wrong_length():
    assert validate_sscc18("12345") is False


def test_validate_sscc18_rejects_non_numeric():
    assert validate_sscc18("50200000004535011X") is False


def test_validate_sscc18_rejects_bad_check_digit():
    sscc = build_sscc18("0200000", 1)
    tampered = sscc[:-1] + str((int(sscc[-1]) + 1) % 10)
    assert validate_sscc18(tampered) is False


def test_validate_sscc18_empty_string():
    assert validate_sscc18("") is False


def test_validate_sscc18_none_safe():
    assert validate_sscc18(None) is False

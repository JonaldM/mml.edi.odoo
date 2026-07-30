"""GS1 SSCC-18 (Serial Shipping Container Code) construction — pure math, no Odoo.

Used by ``models/sscc_register.py`` (Odoo persistence/minting) and directly by
pure pytest (``tests/test_gs1_sscc.py``). Kept Odoo-free, like
``nimbrel_edifact.py``, so the check-digit/composition logic is testable
without a database and cannot accidentally depend on ORM state.

SSCC-18 structure (GS1 General Specifications):
    N1                  Extension digit (0-9, supplier's choice; default '0')
    N2..N8 (or N9/N10)  GS1 Company Prefix — 7 to 10 digits, allocated to the
                         deploying company by GS1. Account-specific
                         configuration, supplied by the caller: this module
                         hardcodes no prefix (see models/sscc_register.py and
                         ir.config_parameter ``mml_edi.gs1_company_prefix``).
    N9..N17             Serial reference — enough digits to fill 16 total
                         digits before the check digit (extension + prefix +
                         serial == 16 digits)
    N18                 Check digit (GS1 mod-10, computed over N1..N17)

For a 7-digit prefix: extension(1) + prefix(7) + serial(9) + check(1) = 18.
The serial component is therefore a 9-digit, zero-padded sequence value; a
longer prefix narrows the serial component by the same number of digits.

GS1 mod-10 check digit algorithm (applies to GTIN/SSCC/GLN alike): starting
from the RIGHTMOST digit of the payload (i.e. N17, working left to N1),
multiply alternating digits by 3 and 1 (rightmost digit x3), sum, and the
check digit is whatever makes the total a multiple of 10.
"""

GS1_PREFIX_LENGTH_DEFAULT = 7
SSCC_TOTAL_LENGTH = 18
SSCC_PAYLOAD_LENGTH = SSCC_TOTAL_LENGTH - 1  # 17 digits before the check digit


class GS1SSCCError(ValueError):
    pass


def gs1_mod10_check_digit(payload_digits: str) -> str:
    """Return the single GS1 mod-10 check digit for ``payload_digits``.

    ``payload_digits`` must be all-numeric. Multiplies digits by 3/1 starting
    from the RIGHTMOST digit (weight 3 on the last digit), per the GS1
    General Specifications algorithm (identical rule used for GTIN/SSCC/GLN).
    """
    if not payload_digits or not payload_digits.isdigit():
        raise GS1SSCCError(
            "gs1_mod10_check_digit: payload must be a non-empty numeric "
            "string, got %r" % (payload_digits,)
        )
    total = 0
    # Rightmost digit gets weight 3, alternating leftward.
    for i, ch in enumerate(reversed(payload_digits)):
        weight = 3 if i % 2 == 0 else 1
        total += int(ch) * weight
    return str((10 - (total % 10)) % 10)


def build_sscc18(gs1_prefix: str, serial: int, *, extension_digit: str = "0") -> str:
    """Compose a full 18-digit SSCC from a GS1 company prefix + serial number.

    Args:
        gs1_prefix: numeric GS1 Company Prefix, typically 7 digits. Comes from
            the deploying company's own GS1 allocation — see
            ``sscc.register._get_gs1_prefix()``; there is no default.
        serial: non-negative integer serial reference (from
            ``mml_edi.sscc.serial`` ir.sequence — contract C2).
        extension_digit: single digit 0-9 (default '0'); GS1 lets the
            supplier choose this per shipping-unit type if desired.

    Returns:
        An 18-digit numeric string: extension + prefix + zero-padded serial
        + mod-10 check digit.

    Raises:
        GS1SSCCError: invalid prefix/extension/serial, or the serial does not
            fit the remaining digit budget (17 - len(prefix) - 1).
    """
    if not gs1_prefix or not str(gs1_prefix).isdigit():
        raise GS1SSCCError("build_sscc18: gs1_prefix must be numeric, got %r" % (gs1_prefix,))
    if not (extension_digit and len(str(extension_digit)) == 1 and str(extension_digit).isdigit()):
        raise GS1SSCCError(
            "build_sscc18: extension_digit must be a single digit 0-9, got %r"
            % (extension_digit,)
        )
    if serial is None or int(serial) < 0:
        raise GS1SSCCError("build_sscc18: serial must be a non-negative integer, got %r" % (serial,))

    prefix = str(gs1_prefix)
    serial_width = SSCC_PAYLOAD_LENGTH - 1 - len(prefix)  # -1 for the extension digit
    if serial_width <= 0:
        raise GS1SSCCError(
            "build_sscc18: gs1_prefix %r leaves no room for a serial component "
            "(prefix must be at most %d digits)" % (gs1_prefix, SSCC_PAYLOAD_LENGTH - 2)
        )
    serial_str = str(int(serial))
    if len(serial_str) > serial_width:
        raise GS1SSCCError(
            "build_sscc18: serial %r does not fit in %d digits for prefix %r "
            "(12-month uniqueness window exhausted its numeric range — widen "
            "the prefix budget or investigate serial exhaustion)"
            % (serial, serial_width, gs1_prefix)
        )
    serial_padded = serial_str.zfill(serial_width)

    payload = str(extension_digit) + prefix + serial_padded
    if len(payload) != SSCC_PAYLOAD_LENGTH:
        # Defensive — should be unreachable given the checks above.
        raise GS1SSCCError(
            "build_sscc18: internal error, payload length %d != %d"
            % (len(payload), SSCC_PAYLOAD_LENGTH)
        )
    check = gs1_mod10_check_digit(payload)
    sscc = payload + check
    if len(sscc) != SSCC_TOTAL_LENGTH:
        raise GS1SSCCError(
            "build_sscc18: internal error, sscc length %d != %d" % (len(sscc), SSCC_TOTAL_LENGTH)
        )
    return sscc


def validate_sscc18(sscc: str) -> bool:
    """True if ``sscc`` is 18 numeric digits with a valid GS1 mod-10 check digit."""
    if not sscc or not str(sscc).isdigit() or len(str(sscc)) != SSCC_TOTAL_LENGTH:
        return False
    payload, check = sscc[:-1], sscc[-1]
    return gs1_mod10_check_digit(payload) == check

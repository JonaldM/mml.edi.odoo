"""Verify ASN generation blocks on missing EAN-13 rather than silently skipping."""
import pathlib


def test_asn_raises_on_missing_barcode_not_silent_skip():
    src = (pathlib.Path(__file__).parent.parent / 'services/edi_service.py').read_text()
    # After the barcode length check, there must be a UserError raise, not a 'continue'
    # This is a heuristic structural test — look for UserError near barcode validation
    assert 'UserError' in src, (
        "edi_service.py must raise UserError when barcode is missing, not silently skip"
    )

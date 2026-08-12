"""Guards on the SSCC label QWeb template.

wkhtmltopdf (0.12.6.1) renders with Qt WebKit from ~2012, which has NO
flexbox support. A `display:flex` two-column layout renders as a SINGLE
column and silently drops the second child — but the dropped text is still
written into the PDF's text layer, so pdf-to-text extraction looks perfectly
correct while the PRINTED label is missing fields.

That is exactly how mandatory fields (Ship To, Connote #, carton sequence,
Description) reached Animates missing. These tests fail loudly instead.
"""
import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).parent.parent / "report" / "sscc_label_report.xml"


@pytest.fixture(scope="module")
def xml():
    return TEMPLATE.read_text(encoding="utf-8")


def _style_attrs(xml):
    """Every style="..." value in the template (not comments)."""
    return re.findall(r'style="([^"]*)"', xml)


def test_no_flexbox_in_any_style_attribute(xml):
    offenders = [s for s in _style_attrs(xml) if "flex" in s]
    assert not offenders, (
        "wkhtmltopdf's Qt WebKit cannot lay out flexbox — the second column "
        "renders blank while its text still reaches the PDF text layer. "
        "Use a table. Offending style(s): %r" % offenders
    )


def test_two_column_zones_use_a_table(xml):
    assert "<table" in xml, "the multi-column zones must be a table"
    assert 'table-layout:fixed' in xml.replace(" ", ""), (
        "table-layout:fixed keeps the 50/50 split stable regardless of content"
    )


@pytest.mark.parametrize("field", [
    "Ship From:", "Ship To:", "Carrier:", "Connote #:",
    "SSCC:", "PO #:", "ISC:", "Carton Qty:", "Vendor Part #:", "Description:",
])
def test_mandatory_label_field_is_present(xml, field):
    """Every field Animates lists as mandatory/preferred must be on the label."""
    assert field in xml, "label is missing the %r field" % field


def test_unit_sequence_is_rendered(xml):
    """'Number of Pallets/Cartons' requires a sequential 'n of N'."""
    assert "unit_sequence" in xml, (
        "carton/pallet count must print a sequence ('1 of 3'), not a bare label"
    )


@pytest.mark.parametrize("src_key", ["sscc_barcode_src", "postcode_barcode_src"])
def test_barcodes_are_embedded_not_fetched_over_http(xml, src_key):
    assert src_key in xml, "barcode %r must come from the embedded data: URI" % src_key
    assert "/report/barcode/" not in xml, (
        "fetching barcodes over HTTP yields NO barcode whenever the renderer "
        "cannot reach a listener for this database (headless shell, cron)"
    )

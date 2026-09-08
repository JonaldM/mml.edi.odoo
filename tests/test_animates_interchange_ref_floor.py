"""The Animates interchange control-reference sequence must never collide with
the placeholder sentinels build_unb(require_real=True) rejects.

_PLACEHOLDER_CTRL_REFS are plain decimal strings (12341, 78401, 99101). The
sequence used to start at 1 with no prefix and increment by 1, so it would
eventually emit exactly those values and every ORDRSP/CONTRL/DESADV/INVOIC
minted on that interchange would hard-fail at send time.

Pure-Python: reads the shipped data file, no Odoo env.
"""
import pathlib
import xml.etree.ElementTree as ET

from mml_edi.parsers.animates_edifact import _PLACEHOLDER_CTRL_REFS

_SEQUENCE_XML = pathlib.Path(__file__).parent.parent / "data" / "ir_sequence.xml"
_SEQUENCE_CODE = "mml_edi.animates.interchange.ref"


def _interchange_sequence_fields():
    root = ET.parse(_SEQUENCE_XML).getroot()
    for record in root.iter("record"):
        fields = {f.get("name"): (f.text or "") for f in record.findall("field")}
        if fields.get("code") == _SEQUENCE_CODE:
            return fields
    raise AssertionError("sequence %s not declared in %s"
                         % (_SEQUENCE_CODE, _SEQUENCE_XML))


def test_sequence_starts_above_every_placeholder_sentinel():
    fields = _interchange_sequence_fields()
    number_next = int(fields["number_next"])
    prefix = fields.get("prefix", "").strip()
    if prefix:
        return  # a non-numeric prefix already makes a collision impossible
    for sentinel in _PLACEHOLDER_CTRL_REFS:
        assert number_next > int(sentinel), (
            "sequence %s starts at %d and would eventually emit the "
            "placeholder control ref %s, which build_unb(require_real=True) "
            "rejects" % (_SEQUENCE_CODE, number_next, sentinel)
        )


def test_sequence_increment_is_positive_so_the_floor_holds():
    fields = _interchange_sequence_fields()
    assert int(fields["number_increment"]) > 0

# mml_edi/tests/test_sscc_register_archived.py
"""Pure tests: an archived register row must still satisfy the idempotency
contract.

sscc.register carries active = fields.Boolean(default=True) and a
UNIQUE(picking_id, unit_key) constraint. An archived row is invisible to an
active_test-filtered search but still occupies the constraint, so get_or_create
saw nothing, created, hit the constraint, searched again, still saw nothing and
re-raised the raw psycopg IntegrityError at the caller - every label reprint
and every DESADV for that unit, forever, and a serial burnt from
mml_edi.sscc.serial on each attempt.

No Odoo needed: get_or_create's lookup path is driven through a fake model.
"""
import pytest

from mml_edi.models.sscc_register import SSCCRegister


class _Picking:
    id = 55


class _Row:
    def __init__(self, rid, unit_key, active=True):
        self.id = rid
        self.picking_id = _Picking()
        self.unit_key = unit_key
        self.active = active

    def write(self, values):
        for key, value in values.items():
            setattr(self, key, value)


class _Rows(list):
    """A one-record recordset forwards field reads and writes to its row."""

    @property
    def active(self):
        return self[0].active

    def write(self, values):
        for row in self:
            row.write(values)


class _ConfigParam:
    def sudo(self):
        return self

    def get_param(self, key, default=None):
        return default


class _Sequence:
    def sudo(self):
        return self

    def next_by_code(self, code):
        return "1"


class _Savepoint:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Cursor:
    def savepoint(self):
        return _Savepoint()


class _Env(dict):
    cr = _Cursor()


class _Register:
    """Fake sscc.register applying active_test the way the ORM does."""

    _get_gs1_prefix = SSCCRegister._get_gs1_prefix
    _registered_unit = SSCCRegister._registered_unit
    get_or_create = SSCCRegister.get_or_create

    def __init__(self, rows, active_test=True):
        self._rows = rows
        self._active_test = active_test
        self.created = []
        self.env = _Env({
            "ir.config_parameter": _ConfigParam(),
            "ir.sequence": _Sequence(),
        })

    def with_context(self, **kwargs):
        clone = _Register(self._rows, kwargs.get("active_test", self._active_test))
        clone.created = self.created
        return clone

    def search(self, domain, limit=None):
        wanted = dict((leaf[0], leaf[2]) for leaf in domain)
        rows = [
            r for r in self._rows
            if r.picking_id.id == wanted["picking_id"]
            and r.unit_key == wanted["unit_key"]
        ]
        if self._active_test:
            rows = [r for r in rows if r.active]
        return _Rows(rows[:limit] if limit else rows)

    def create(self, values):
        self.created.append(values)
        raise AssertionError(
            "get_or_create minted a new row while one already exists for this "
            "(picking, unit_key) - the unique constraint would reject it")


class TestArchivedRowIsReused:

    def test_archived_row_is_found_and_returned(self):
        row = _Row(1, "carton-3", active=False)
        reg = _Register([row])
        assert reg.get_or_create(_Picking(), "carton-3") == [row]
        assert reg.created == []

    def test_archived_row_is_brought_back_into_use(self):
        # The SSCC is on a physical label that is about to be reprinted or
        # re-sent, so leaving the register row archived would keep the register
        # disagreeing with what is on the pallet.
        row = _Row(1, "carton-3", active=False)
        _Register([row]).get_or_create(_Picking(), "carton-3")
        assert row.active is True

    def test_active_row_is_returned_untouched(self):
        row = _Row(2, "pallet-1")
        assert _Register([row]).get_or_create(_Picking(), "pallet-1") == [row]

    def test_a_different_unit_key_is_not_matched(self):
        row = _Row(3, "carton-3", active=False)
        with pytest.raises(AssertionError):
            _Register([row]).get_or_create(_Picking(), "carton-9")

# mml_edi/tests/test_partner_health_archived.py
"""Pure tests: the partner-health board must see archived partners.

edi.trading.partner carries active = fields.Boolean(default=True), so Odoo's
default active_test excludes archived rows from a plain search. The board then
computed scoped = len(partners) - len(partners.filtered("active")), which is
identically zero, so the subtitle could never render its "N scoped" clause and
_circuit's "N/A" branch (which requires ``not p.active``) was unreachable.

No Odoo needed: the partner search is driven through a fake model that applies
active_test the way the ORM does.
"""
from mml_edi.models.edi_partner_health import EdiPartnerHealth


class _Partner:
    def __init__(self, name, active=True):
        self.name = name
        self.active = active


class _Partners(list):
    def filtered(self, fn):
        if isinstance(fn, str):
            return _Partners([p for p in self if getattr(p, fn)])
        return _Partners([p for p in self if fn(p)])


class _PartnerModel:
    """Applies active_test exactly as the ORM does: on by default, off only
    when the caller says so."""

    def __init__(self, records, active_test=True):
        self._records = records
        self._active_test = active_test

    def with_context(self, **kwargs):
        return _PartnerModel(
            self._records, kwargs.get("active_test", self._active_test))

    def search(self, domain, order=None):
        rows = self._records
        if self._active_test:
            rows = [p for p in rows if p.active]
        return _Partners(sorted(rows, key=lambda p: p.name))


class _Health:
    _all_partners = EdiPartnerHealth._all_partners
    _subtitle = EdiPartnerHealth._subtitle

    def __init__(self, records):
        self.env = {"edi.trading.partner": _PartnerModel(records)}


ROSTER = [_Partner("Animates", active=False), _Partner("Briscoes")]


class TestAllPartners:

    def test_archived_partners_are_included(self):
        assert [p.name for p in _Health(ROSTER)._all_partners()] == [
            "Animates", "Briscoes"]

    def test_scoped_count_is_real(self):
        partners = _Health(ROSTER)._all_partners()
        active = len(partners.filtered("active"))
        assert (active, len(partners) - active) == (1, 1)


class TestSubtitle:

    def test_scoped_clause_renders(self):
        assert _Health(ROSTER)._subtitle(1, 1).startswith("1 active · 1 scoped")

    def test_no_scoped_clause_when_nothing_is_archived(self):
        assert "scoped" not in _Health(ROSTER)._subtitle(2, 0)

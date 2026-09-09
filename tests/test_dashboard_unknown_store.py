# mml_edi/tests/test_dashboard_unknown_store.py
"""Pure tests: an unknown store code must reach the operator as an unknown
store code.

edi.processor._create_review_and_so raises the unknown_store issue with
severity='blocking' unconditionally, so such a review always lands in the
triage's BLOCKING tier. The dedicated copy, the 'map_store' action and the
wall's "Unknown stores" alarm all hung off the WARNING tier instead, which
that issue never reaches: an unmapped store showed up as a generic blocking
row and the alarm tile sat at zero.

No Odoo needed: the triage and the alarm tiles are driven through fakes.
"""
from datetime import datetime, timedelta

from mml_edi.models.edi_dashboard import EdiDashboard
from mml_edi.models.edi_wall import EdiWall

NOW = datetime(2026, 3, 2, 9, 0)
WINDOW_START = NOW - timedelta(days=30)


class _Issue:
    def __init__(self, issue_type, severity, description=""):
        self.issue_type = issue_type
        self.severity = severity
        self.description = description


class _Issues(list):
    def filtered(self, fn):
        return _Issues([i for i in self if fn(i)])

    def __getitem__(self, item):
        result = list.__getitem__(self, item)
        return _Issues(result) if isinstance(item, slice) else result

    @property
    def description(self):
        """A one-record recordset forwards field reads, which is how
        _issue_summary reads its target."""
        return self[0].description if self else ""


class _Partner:
    code = "BRISCOES"


class _Review:
    def __init__(self, rid, po, store, issues, blocking, warnings):
        self.id = rid
        self.name = "REV%03d" % rid
        self.trading_partner_id = _Partner()
        self.customer_po_number = po
        self.store_code = store
        self.state = "pending_review"
        self.received_date = NOW - timedelta(hours=3)
        self.reviewed_date = False
        self.issue_ids = _Issues(issues)
        self.blocking_issue_count = blocking
        self.warning_count = warnings


class _ReviewModel:
    def __init__(self, records):
        self._records = records

    def search(self, domain, order=None, limit=None):
        wanted = dict(
            (leaf[0], leaf[2]) for leaf in domain
            if isinstance(leaf, (list, tuple)) and len(leaf) == 3
            and leaf[1] == "=")
        if wanted.get("state") != "pending_review":
            return []
        blocking = [r for r in self._records if r.blocking_issue_count > 0]
        if "blocking_issue_count" in wanted:  # the warnings source
            return [r for r in self._records if r.blocking_issue_count == 0
                    and r.warning_count > 0]
        return blocking


class _LogModel:
    def search(self, domain, order=None, limit=None):
        return []

    def search_count(self, domain):
        return 0


class _Dash:
    _attention_items = EdiDashboard._attention_items
    _partner_domain = EdiDashboard._partner_domain
    _item_blocking = EdiDashboard._item_blocking
    _item_warning = EdiDashboard._item_warning
    _item_ack_failed = EdiDashboard._item_ack_failed
    _unknown_store_issues = EdiDashboard._unknown_store_issues
    _partner_tag = EdiDashboard._partner_tag
    _review_title = EdiDashboard._review_title
    _issue_summary = EdiDashboard._issue_summary
    _age_hours = EdiDashboard._age_hours

    def __init__(self, reviews):
        self.env = {
            "edi.order.review": _ReviewModel(reviews),
            "edi.log": _LogModel(),
        }


class _Wall:
    _alarms = EdiWall._alarms
    _poll_freshness_minutes = staticmethod(lambda *a, **k: 3)


def _blocking_unknown_store():
    return _Review(
        1, "4500178971", "1099",
        [_Issue("unknown_store", "blocking", "Store code '1099' not found")],
        blocking=1, warnings=0)


def _blocking_other():
    return _Review(
        2, "4500178972", False,
        [_Issue("product_not_found", "blocking", "GTIN not in Odoo")],
        blocking=1, warnings=0)


class TestUnknownStoreReachesTheOperator:

    def test_blocking_unknown_store_offers_the_map_store_action(self):
        items = _Dash([_blocking_unknown_store()])._attention_items(NOW, WINDOW_START)
        assert len(items) == 1
        assert "map_store" in items[0]["actions"]

    def test_blocking_unknown_store_says_unknown_store_in_the_title(self):
        items = _Dash([_blocking_unknown_store()])._attention_items(NOW, WINDOW_START)
        assert "unknown store" in items[0]["title"]

    def test_blocking_unknown_store_stays_red(self):
        # The issue really is blocking - the whole PO's ORDRSP is held - so
        # surfacing the store code must not downgrade the tier.
        items = _Dash([_blocking_unknown_store()])._attention_items(NOW, WINDOW_START)
        assert items[0]["severity"] == "red"

    def test_other_blocking_issues_keep_the_generic_shape(self):
        items = _Dash([_blocking_other()])._attention_items(NOW, WINDOW_START)
        assert items[0]["actions"] == ["review"]
        assert "unknown store" not in items[0]["title"]


class TestWallUnknownStoresAlarm:

    def test_alarm_counts_a_blocking_unknown_store(self):
        attention = _Dash([_blocking_unknown_store()])._attention_items(
            NOW, WINDOW_START)
        alarms = _Wall()._alarms(NOW, attention)
        unknown = [a for a in alarms if a["key"] == "unknown"][0]
        assert unknown["value"] == "1"

    def test_alarm_ignores_other_blocking_issues(self):
        attention = _Dash([_blocking_other()])._attention_items(NOW, WINDOW_START)
        alarms = _Wall()._alarms(NOW, attention)
        unknown = [a for a in alarms if a["key"] == "unknown"][0]
        assert unknown["value"] == "0"

# mml_edi/tests/test_review_queue_ordrsp_panel.py
"""Pure tests: the ORDRSP panel must name the file that will actually be sent.

_supersede_sent_ack bumps ack_attempt on every sibling review when a manager
resets a review whose ORDRSP had already gone out (IDEM-4), and
_ack_exchange_filename then appends "_a<n>". A panel that re-derives
"ACK_<code>_<po>_<hash8>.edi" inline shows attempt 1's name next to a status
chip computed from attempt 2, so the operator looking for the file on the VAN
looks for the wrong one.

No Odoo needed: _ordrsp_panel only searches sibling reviews.
"""
from mml_edi.models.edi_order_review import _ack_filename
from mml_edi.models.edi_review_queue import EdiReviewQueue


class _Partner:
    id = 4
    code = "BRISCOES"


class _Review:
    def __init__(self, rid, attempt, state="approved", store="1017"):
        self.id = rid
        self.trading_partner_id = _Partner()
        self.customer_po_number = "4500178971"
        self.edi_file_hash = "ab12cd34ff"
        self.ack_attempt = attempt
        self.state = state
        self.store_code = store
        self.ack_status = "failed"

    def _ack_exchange_filename(self):
        return _ack_filename(
            self.trading_partner_id.code, self.customer_po_number,
            (self.edi_file_hash or str(self.id))[:8], self.ack_attempt or 1)


class _Siblings(list):
    def filtered(self, fn):
        return _Siblings([r for r in self if fn(r)])


class _ReviewModel:
    def __init__(self, records):
        self._records = records

    def search(self, domain, order=None):
        return _Siblings(self._records)


class _Queue:
    _ordrsp_panel = EdiReviewQueue._ordrsp_panel

    def __init__(self, records):
        self.env = {"edi.order.review": _ReviewModel(records)}


class TestOrdrspPanelFilename:

    def test_first_attempt_keeps_the_historical_name(self):
        rec = _Review(1, 1)
        panel = _Queue([rec])._ordrsp_panel(rec)
        assert panel["filename"] == "ACK_BRISCOES_4500178971_ab12cd34.edi"

    def test_after_a_reset_after_sent_the_name_carries_the_attempt(self):
        rec = _Review(2, 2)
        panel = _Queue([rec])._ordrsp_panel(rec)
        assert panel["filename"] == "ACK_BRISCOES_4500178971_ab12cd34_a2.edi"

    def test_the_panel_agrees_with_the_sender(self):
        rec = _Review(3, 3)
        panel = _Queue([rec])._ordrsp_panel(rec)
        assert panel["filename"] == rec._ack_exchange_filename()

    def test_sibling_progress_is_unchanged(self):
        pending = _Review(4, 2, state="pending_review", store="1099")
        approved = _Review(5, 2, state="approved", store="1017")
        panel = _Queue([pending, approved])._ordrsp_panel(pending)
        assert (panel["total_stores"], panel["resolved_stores"]) == (2, 1)
        assert panel["all_resolved"] is False

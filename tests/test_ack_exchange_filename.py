"""Pure tests for the per-exchange ACK filename (go-live FIX IDEM-4).

Attempt 1 must keep the historical ``ACK_<partner>_<po>_<key>.edi`` shape so
every ack_sent row already in production still matches its exchange; attempt
>= 2 (a reset AFTER a sent ACK) appends ``_a<n>`` so the corrected ORDRSP goes
out as a FRESH exchange instead of being silently suppressed by the previous
attempt's success row.

Run with: cd <module dir> && python -m pytest tests/test_ack_exchange_filename.py -q
"""
from mml_edi.models.edi_order_review import _ack_filename


class TestAckExchangeFilename:

    def test_attempt_one_keeps_historical_shape(self):
        # Prod ack_sent rows were written with this exact shape — attempt 1
        # must never change it or every already-sent ACK re-sends.
        assert _ack_filename("KESTRELBY", "4500178971", "ab12cd34") == (
            "ACK_KESTRELBY_4500178971_ab12cd34.edi"
        )

    def test_attempt_two_appends_suffix(self):
        assert _ack_filename("KESTRELBY", "4500178971", "ab12cd34", attempt=2) == (
            "ACK_KESTRELBY_4500178971_ab12cd34_a2.edi"
        )

    def test_falsy_attempt_treated_as_first(self):
        # Legacy rows predate the ack_attempt column (NULL -> falsy).
        assert _ack_filename("P", "PO1", "k", attempt=None) == "ACK_P_PO1_k.edi"
        assert _ack_filename("P", "PO1", "k", attempt=0) == "ACK_P_PO1_k.edi"
        assert _ack_filename("P", "PO1", "k", attempt=1) == "ACK_P_PO1_k.edi"

    def test_each_attempt_is_a_distinct_exchange(self):
        names = {_ack_filename("P", "PO1", "k", attempt=a) for a in (1, 2, 3)}
        assert len(names) == 3

"""Tests for EDIFACT ORDRSP ACK generation (_generate_ordrsp)."""
from unittest.mock import MagicMock


def _lin_action(line: str) -> str:
    """Extract the action code (position [2]) from a LIN segment string.

    LIN format: ``LIN+{line_no}+{action}+{barcode}:EN'``
    Splitting on '+' and taking index 2 gives the action field without
    risk of a false positive from a barcode that happens to contain '+N+'.
    """
    return line.rstrip("'").split("+")[2]


def _make_sol(line_number, barcode, default_code, qty, price, shortfall=0.0):
    sol = MagicMock()
    sol.edi_line_number = line_number
    sol.product_id.barcode = barcode
    sol.product_id.default_code = default_code
    sol.product_uom_qty = qty
    sol.price_unit = price
    sol.edi_qty_shortfall = shortfall
    return sol


def _make_so(lines, commitment_date=None):
    so = MagicMock()
    # order_line must be directly iterable (for the purpose-code any() check)
    # AND support .sorted(...) call (for the per-line loop)
    so.order_line.__iter__ = lambda self: iter(lines)
    so.order_line.sorted.return_value = lines
    so.commitment_date = commitment_date
    return so


def _make_review(state="approved", store_code="1005", po_number="4500038166", so=None):
    review = MagicMock()
    review.state = state
    review.store_code = store_code
    review.customer_po_number = po_number
    review.sale_order_id = so

    partner = MagicMock()
    partner.partner_id.ref = "300024"
    partner.partner_id.vat = None
    partner.partner_id.name = "Briscoe Group Ltd"
    review.trading_partner_id = partner
    return review


class TestOrdrspGeneration:

    def test_returns_bytes(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        result = _generate_ordrsp(review)
        assert isinstance(result, bytes)

    def test_contains_ordrsp_message_type(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        assert "ORDRSP" in text

    def test_contains_bgm_231(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        assert "BGM+231" in text

    def test_rejected_uses_purpose_27(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="rejected", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        bgm_lines = [l for l in text.split("\r\n") if l.startswith("BGM")]
        assert bgm_lines, "No BGM segment found"
        assert bgm_lines[0].endswith("+27'"), "Expected purpose 27 (cancelled) for rejected"

    def test_approved_clean_uses_purpose_29(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 10.0, 5.50, shortfall=0.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        bgm_lines = [l for l in text.split("\r\n") if l.startswith("BGM")]
        assert bgm_lines[0].endswith("+29'"), "Expected purpose 29 (accepted) for clean order"

    def test_approved_with_shortfall_uses_purpose_4(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 8.0, 5.50, shortfall=2.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        bgm_lines = [l for l in text.split("\r\n") if l.startswith("BGM")]
        assert bgm_lines[0].endswith("+4'"), "Expected purpose 4 (changed) for short supply"

    def test_po_reference_in_rff_on(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None, po_number="4500038166")
        text = _generate_ordrsp(review).decode("utf-8")
        assert "RFF+ON:4500038166" in text

    def test_accepted_line_action_5(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 10.0, 5.50, shortfall=0.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        lin_lines = [l for l in text.split("\r\n") if l.startswith("LIN")]
        assert any(_lin_action(l) == "5" for l in lin_lines), "Expected line action 5 (accepted)"

    def test_shortfall_line_action_3(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 8.0, 5.50, shortfall=2.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        lin_lines = [l for l in text.split("\r\n") if l.startswith("LIN")]
        assert any(_lin_action(l) == "3" for l in lin_lines), "Expected line action 3 (qty changed)"

    def test_rejected_line_action_7(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 10.0, 5.50, shortfall=0.0)
        so = _make_so([sol])
        review = _make_review(state="rejected", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        lin_lines = [l for l in text.split("\r\n") if l.startswith("LIN")]
        assert any(_lin_action(l) == "7" for l in lin_lines), "Expected line action 7 (rejected)"

    def test_fully_oos_line_action_7(self):
        """A line short-shipped to zero (nothing available) is a line rejection
        (action 7), not a qty-change (action 3) carrying a confirmed qty of 0."""
        from mml_edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 0.0, 5.50, shortfall=10.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        lin_lines = [l for l in text.split("\r\n") if l.startswith("LIN")]
        assert lin_lines, "No LIN segment"
        assert _lin_action(lin_lines[0]) == "7", \
            "Fully-OOS line should be rejected (action 7), got %r" % _lin_action(lin_lines[0])

    def test_partial_short_line_still_action_3(self):
        """Regression: a partially short line (some qty ships) stays a qty-change
        (action 3), not a rejection."""
        from mml_edi.parsers.briscoes import _generate_ordrsp
        sol = _make_sol(10, "9414844375629", "INT001", 8.0, 5.50, shortfall=2.0)
        so = _make_so([sol])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        lin_lines = [l for l in text.split("\r\n") if l.startswith("LIN")]
        assert _lin_action(lin_lines[0]) == "3", \
            "Partial short line should be qty-changed (action 3), got %r" % _lin_action(lin_lines[0])

    def test_zero_qty_line_with_bad_barcode_does_not_block_ordrsp(self):
        """A fully-OOS (action-7) line whose product lacks a valid EAN-13 must NOT
        block the ORDRSP — only shipping lines (qty > 0) need barcodes."""
        from mml_edi.parsers.briscoes import _generate_ordrsp
        bad = _make_sol(10, "NOTANEAN", "OOS001", 0.0, 5.50, shortfall=10.0)
        good = _make_sol(20, "9414844375629", "INT002", 4.0, 5.50, shortfall=0.0)
        so = _make_so([bad, good])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")  # must not raise UserError
        lin = [l for l in text.split("\r\n") if l.startswith("LIN")]
        self_actions = [_lin_action(l) for l in lin]
        assert "7" in self_actions, "OOS line should still emit action 7"
        assert "5" in self_actions, "Shipping line should emit action 5"

    def test_segment_terminators_present(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        for line in text.strip().split("\r\n"):
            if line.strip():
                assert line.endswith("'"), "Segment must end with \"'\": %r" % line

    def test_unt_segment_count_valid(self):
        """UNT segment count should be >= 6 (minimum viable ORDRSP)."""
        from mml_edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        unt_lines = [l for l in text.split("\r\n") if l.startswith("UNT")]
        assert unt_lines, "UNT segment missing"
        count_str = unt_lines[0].rstrip("'").split("+")[1]
        count = int(count_str)
        assert count >= 6, "UNT count seems too low: %d" % count

    def test_multiple_lines_mixed_actions(self):
        """One accepted + one shortfall line -> purpose 4, actions 5 and 3."""
        from mml_edi.parsers.briscoes import _generate_ordrsp
        sol_ok = _make_sol(10, "9414844375629", "INT001", 10.0, 5.50, shortfall=0.0)
        sol_short = _make_sol(20, "9414844375636", "INT002", 5.0, 0.55, shortfall=2.0)
        so = _make_so([sol_ok, sol_short])
        review = _make_review(state="approved", so=so)
        text = _generate_ordrsp(review).decode("utf-8")
        bgm_lines = [l for l in text.split("\r\n") if l.startswith("BGM")]
        lin_lines = [l for l in text.split("\r\n") if l.startswith("LIN")]
        assert bgm_lines[0].endswith("+4'"), "Mixed: purpose should be 4"
        assert any(_lin_action(l) == "5" for l in lin_lines), "Should have an accepted line"
        assert any(_lin_action(l) == "3" for l in lin_lines), "Should have a qty-changed line"

    def test_unb_and_unz_present(self):
        from mml_edi.parsers.briscoes import _generate_ordrsp
        review = _make_review(state="approved", so=None)
        text = _generate_ordrsp(review).decode("utf-8")
        assert any(l.startswith("UNB") for l in text.split("\r\n")), "UNB missing"
        assert any(l.startswith("UNZ") for l in text.split("\r\n")), "UNZ missing"

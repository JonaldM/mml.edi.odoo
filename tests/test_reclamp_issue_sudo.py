"""Pure tests for the approve-time re-clamp issue create.

security/ir.model.access.csv gives mml_edi.group_edi_user read+write but
NOT create on edi.order.issue, yet a plain EDI User is expected to approve
reviews. _reclamp_before_confirm created the system-generated qty_shortfall
issue in the operator's own environment, so a line re-clamped to zero raised
AccessError and rolled the whole approval back.
"""
from types import SimpleNamespace

from mml_edi.models.edi_order_review import EDIOrderReview


class _AccessError(Exception):
    """Stands in for odoo.exceptions.AccessError (absent from the stubs)."""


class _FakeIssueModel:
    """edi.order.issue with the production ACL: create is denied unless the
    caller elevated with sudo()."""

    def __init__(self, creates, elevated=False):
        self.creates = creates
        self._elevated = elevated

    def sudo(self):
        return _FakeIssueModel(self.creates, elevated=True)

    def create(self, vals):
        if not self._elevated:
            raise _AccessError(
                "You are not allowed to create Edi Order Issue records"
            )
        self.creates.append(vals)
        return SimpleNamespace(id=len(self.creates))


class _FakeProcessor:

    def __init__(self, changes):
        self._changes = changes

    def reclamp_order_lines(self, sale_order, partner, cap_to_current=False):
        return self._changes


class _FakeEnv:

    def __init__(self, changes, creates):
        self._models = {
            "edi.processor": _FakeProcessor(changes),
            "edi.order.issue": _FakeIssueModel(creates),
        }

    def __getitem__(self, name):
        return self._models[name]


class _Review(EDIOrderReview):

    def __init__(self, changes, creates):
        self.id = 5
        self.env = _FakeEnv(changes, creates)
        self.sale_order_id = SimpleNamespace(state="draft", name="S00042")
        self.trading_partner_id = SimpleNamespace(code="BRISCOES")
        self.posts = []

    def ensure_one(self):
        return self

    def message_post(self, **kwargs):
        self.posts.append(kwargs)


def _zeroed_change():
    line = SimpleNamespace(
        id=11,
        name="WIDGET-1",
        product_id=SimpleNamespace(display_name="Widget"),
        edi_ordered_qty=5.0,
    )
    return [{"line": line, "old_qty": 5.0, "new_qty": 0.0, "shortfall": 5.0}]


class TestReclampIssueSudo:

    def test_zero_clamp_issue_is_created_with_sudo(self):
        creates = []
        review = _Review(_zeroed_change(), creates)
        review._reclamp_before_confirm()
        assert len(creates) == 1, (
            "a line clamped to zero must still raise its qty_shortfall issue "
            "when the approver is a plain EDI User"
        )
        assert creates[0]["issue_type"] == "qty_shortfall"
        assert creates[0]["sale_order_line_id"] == 11

    def test_approve_is_not_aborted_by_the_issue_acl(self):
        creates = []
        review = _Review(_zeroed_change(), creates)
        changes = review._reclamp_before_confirm()
        assert changes, "the re-clamp result must reach the caller"
        assert review.posts, "the clamp summary is still posted on the review"

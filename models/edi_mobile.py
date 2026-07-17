# mml_edi/models/edi_mobile.py
"""EDI mobile-triage computation service.

The EDI approver's pocket view (README EDI screen 8, ``Mobile Triage.dc.html``)
— a responsive, one-tap variant of the review triage for Odoo mobile / PWA. The
iOS frame in the prototype is presentation-only; here it is a plain responsive
OWL client action (``edi_mobile_triage``) that themes with Odoo's native colour
scheme.

AbstractModel — no DB table. The list is one batched ``@api.model`` RPC,
``edi.mobile.triage.get_triage(partner_code)``, reusing
``edi.dashboard._attention_items`` (a single batched pass, no per-card query) so
the pocket cards and the desktop triage never diverge. Each card carries a
primary + secondary action wired to the REAL ``edi.order.review`` state machine
(``action_approve`` / ``action_reject`` / open form) and the ORDRSP retry — the
prototype's localStorage transport is dropped.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

from ..services.mobile_triage_format import card_actions

_logger = logging.getLogger(__name__)


class EdiMobileTriage(models.AbstractModel):
    """Mobile-triage computation service for the EDI OWL pocket view.

    AbstractModel (no table). Called from the frontend via
    ``this.orm.call('edi.mobile.triage', 'get_triage', [], {...})``.
    """

    _name = "edi.mobile.triage"
    _description = "EDI Mobile Triage"
    _table = "edi_mobile_triage"

    @api.model
    def get_triage(self, partner_code=None):
        """Return the pocket-triage payload, optionally scoped to a partner.

        Payload keys:
          - ``data_available`` — False before any inbound activity.
          - ``partners`` — the segmented-filter options (code + name).
          - ``pending`` — total pending reviews (the '9 pending' header).
          - ``red_count`` / ``amber_count`` — the blocking / warning tallies.
          - ``cards`` — the actionable triage cards, each with a primary +
            secondary action (red before amber, oldest first within a tier — the
            order ``_attention_items`` already guarantees).
        """
        dash = self.env["edi.dashboard"]
        now = fields.Datetime.now()
        window_start = now - timedelta(days=30)

        attention = dash._attention_items(now, window_start, partner_code)
        cards = [self._card(item) for item in attention]
        return {
            "data_available": dash._data_available(partner_code),
            "partners": dash._partner_options(),
            "pending": dash._pending_review_count(partner_code),
            "red_count": sum(1 for c in cards if c["severity"] == "red"),
            "amber_count": sum(1 for c in cards if c["severity"] == "amber"),
            "cards": cards,
        }

    # ---- card shaping --------------------------------------------------------

    @api.model
    def _card(self, item):
        """Shape one ``_attention_items`` row into a pocket card + its actions."""
        return {
            "id": item["res_id"],
            "kind": item["kind"],
            "severity": item["severity"],
            "partner_tag": item["partner_tag"],
            "title": item["title"],
            "summary": item["summary"],
            "age_hours": item["age_hours"],
            "actions": card_actions(item),
        }

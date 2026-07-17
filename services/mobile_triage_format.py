# mml_edi/services/mobile_triage_format.py
"""Pure card-action mapping for the EDI Mobile Triage pocket view.

No Odoo dependency — the primary/secondary one-tap action mapping is a pure
function over an attention-item dict, so it is unit-testable without a live
database (``tests/test_mobile_triage_format.py``). ``models/edi_mobile.py`` is a
thin adapter that reads the ORM (via ``edi.dashboard._attention_items``) and
calls this to shape each card.
"""


def card_actions(item):
    """The primary + secondary one-tap actions for a triage card, by kind.

    Only genuinely safe one-tap actions are offered:
      * blocking — Open (to resolve the block) / Reject the PO;
      * failed ORDRSP — Retry ACK (idempotent re-queue) / Open;
      * unknown store — Open (to seed the store map) / Reject;
      * other warning (stock shortfall etc.) — Approve (clamped) / Open.
    A blocking review is never one-tap 'approved' — it needs the block cleared
    first, so its primary is Open, not Approve.
    """
    kind = item["kind"]
    if kind == "blocking":
        return [
            {"action": "open", "label": "Open", "variant": "accent"},
            {"action": "reject", "label": "Reject", "variant": "danger"},
        ]
    if kind == "ack_failed":
        return [
            {"action": "retry_ack", "label": "Retry ACK", "variant": "accent"},
            {"action": "open", "label": "Open", "variant": "neutral"},
        ]
    # warning
    if "map_store" in item.get("actions", []):
        return [
            {"action": "open", "label": "Open", "variant": "accent"},
            {"action": "reject", "label": "Reject", "variant": "danger"},
        ]
    return [
        {"action": "approve", "label": "Approve", "variant": "approve"},
        {"action": "open", "label": "Open", "variant": "neutral"},
    ]

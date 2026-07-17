# mml_edi/models/edi_review_queue.py
"""EDI review-queue computation service.

Backs the two triage screens of the UI-refresh program:

  * ``edi_review_queue``  — master-detail queue of ``edi.order.review`` grouped
    Blocking / Warnings / Change orders / Auto-approved (README EDI screen 2).
  * ``edi_review_detail`` — the single-review page with the per-PO ORDRSP
    deferral panel, lifecycle timeline and message log (README EDI screen 3).

Both read from the same three EDI models (``edi.order.review`` /
``edi.order.issue`` / ``edi.log``) plus chatter, and mirror the batched,
read-mostly ``@api.model`` contract the landing dashboard (``edi.dashboard``)
established: one call returns a whole screen's payload, narrowed by an optional
trading-partner scope so the segmented "All / Briscoes / Animates" control
recomputes everything and persists per user.

Honesty caveats from the design copy survive to production: ONE ORDRSP covers
all store-orders of a PO and is deferred until every store leaves review; the
per-PO ORDRSP is held while any store on the PO is pending; clean orders
auto-approve and acknowledge in the same poll. No signal is fabricated — every
number, chip and timeline node comes from a real record.

AbstractModel — no DB table. State-mutating actions (approve / reject / reset /
per-issue accept-correct-reject) are the existing methods on
``edi.order.review`` / ``edi.order.issue``; this service only READS, and the
OWL screens call those real methods by id.
"""
import logging

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# ---- fixed chip palettes (explicit light-bg / dark-fg pairs, both schemes) ---
#
# Identical values in light and dark — the README "subtle chip pairs" — so they
# read correctly whichever colour scheme Odoo is in, exactly like the dashboard
# feed tags. Kept server-side so the OWL templates stay declarative.
_SEV_DOT = {"blocking": "#dc3545", "warning": "#ffc107", "info": "#0d6efd"}

# queue group_key -> master-row dot colour. Pinned to the Review Queue.dc.html
# sample data per group so the daily triage list encodes each group honestly:
# blocking=red, warning=amber, change=informational blue, auto=clean/audit green.
# Mapped straight off the group (NOT routed through the red/amber/green age tone,
# which collapses change+warning to amber and would mis-colour change/auto).
_GROUP_DOT = {
    "blocking": "#dc3545",
    "warning": "#ffc107",
    "change": "#0d6efd",
    "auto": "#198754",
}

_STATE_CHIP = {
    "pending_review": ("Pending review", "edi-chip-amber"),
    "auto_approved": ("Auto-approved", "edi-chip-green"),
    "approved": ("Approved", "edi-chip-green"),
    "rejected": ("Rejected", "edi-chip-red"),
}

_AGE_PILL = {
    "red": ("#f8d7da", "#dc3545"),
    "amber": ("#fff3cd", "#664d03"),
    "green": ("#d1e7dd", "#0a3622"),
}

# issue_type -> (badge label default, human type label)
_ISSUE_TYPE_LABEL = {
    "product_not_found": "Product not found",
    "price_discrepancy": "Price discrepancy",
    "qty_shortfall": "Stock shortfall",
    "unknown_store": "Unknown store",
    "uom_mismatch": "UOM mismatch",
    "product_matched_by_fallback": "Product matched by fallback",
    "other": "Other",
}
_SEV_BADGE = {
    "blocking": ("BLOCKING", "edi-chip-red"),
    "warning": ("WARNING", "edi-chip-amber"),
    "info": ("INFO", "edi-chip-blue"),
}
_RESOLUTION_CHIP = {
    "accepted": ("Accepted", "edi-chip-green"),
    "corrected": ("Corrected", "edi-chip-green"),
    "rejected": ("Rejected", "edi-chip-red"),
}

# hint tone -> (bg, fg) subtle pair
_HINT_TONE = {
    "red": ("#f8d7da", "#58151c"),
    "amber": ("#fff3cd", "#664d03"),
    "info": ("#cff4fc", "#055160"),
}

# lifecycle event_type -> (dot colour, human title) for the timeline
_TIMELINE_META = {
    "file_download": ("#fd7e14", "File downloaded"),
    "file_parse": ("#0d6efd", "File parsed"),
    "order_created": ("#0d6efd", "Store-order routed"),
    "duplicate_skipped": ("#6c757d", "Duplicate skipped"),
    "review_approved": ("#198754", "Review approved"),
    "review_rejected": ("#dc3545", "Review rejected"),
    "po_change_applied": ("#0dcaf0", "Change order applied"),
    "ack_sent": ("#714B67", "ORDRSP sent"),
    "error": ("#dc3545", "Error raised"),
    "ftp_connection": ("#fd7e14", "Transport"),
    "info": ("#6c757d", "Event"),
}


class EdiReviewQueue(models.AbstractModel):
    """Read-mostly queue/detail computation service for the EDI triage screens.

    AbstractModel (no table). All methods are ``@api.model``, invoked from the
    frontend via ``this.orm.call('edi.review.queue', ...)``.
    """

    _name = "edi.review.queue"
    _description = "EDI Review Queue"
    # AbstractModel creates no table, but Odoo still derives + validates
    # ``_table``; pin it for parity with the other edi.* service models.
    _table = "edi_review_queue"

    # =====================================================================
    #  partner scope helpers (mirror edi.dashboard's contract)
    # =====================================================================

    @api.model
    def _partner_for_code(self, partner_code):
        if not partner_code or partner_code == "all":
            return self.env["edi.trading.partner"].search([])
        return self.env["edi.trading.partner"].search([("code", "=", partner_code)])

    @api.model
    def _partner_domain(self, partner_code, field="trading_partner_id"):
        if not partner_code or partner_code == "all":
            return []
        partner = self._partner_for_code(partner_code)
        # A stale/unknown code scopes to nothing (id -1) rather than silently
        # widening to all — the queue honestly shows an empty scope.
        return [(field, "=", partner.id if partner else -1)]

    @api.model
    def _partner_options(self):
        partners = self.env["edi.trading.partner"].search([], order="name")
        return [{"code": "all", "name": "All partners"}] + [
            {"code": p.code, "name": p.name} for p in partners
        ]

    @api.model
    def _partner_tag(self, rec):
        return (rec.trading_partner_id.code or "").upper()

    @api.model
    def _age_hours(self, now, ts):
        if not ts:
            return 0.0
        return round((now - ts).total_seconds() / 3600.0, 1)

    @api.model
    def _age_label(self, hours):
        if hours is None:
            return "—"
        if hours < 1:
            return "%dm" % round(hours * 60)
        if hours >= 48:
            return "%dd" % round(hours / 24)
        return "%dh" % round(hours)

    # =====================================================================
    #  QUEUE — grouped master list
    # =====================================================================

    @api.model
    def get_queue(self, partner_code=None, auto_limit=25):
        """Return the grouped review queue, optionally scoped to one partner.

        Groups (README screen 2): Blocking (needs a decision) / Warnings only /
        Change orders / Auto-approved (clean, shown for audit). Every pending
        review appears in exactly one group; auto-approved rows are bounded to
        the most recent ``auto_limit`` so the audit tail never grows unbounded.

        Payload keys:
          - ``data_available`` — False before any inbound activity in scope
          - ``partners`` — segmented-filter options (code + name)
          - ``groups`` — ordered ``[{key,label,items:[...]}]`` (empty groups drop)
          - ``counts`` — ``{all,blocking,warning,change,auto}`` for the pills
          - ``clean_ids`` — pending reviews with no issues (Approve-all-clean)
          - ``summary_line`` — "N pending · oldest Xd · <scope>"
          - ``degraded`` — partner-degraded banner, or None
        """
        now = fields.Datetime.now()
        Review = self.env["edi.order.review"]
        pdom = self._partner_domain(partner_code)

        pending = Review.search(
            pdom + [("state", "=", "pending_review")], order="received_date asc")
        auto = Review.search(
            pdom + [("state", "=", "auto_approved")],
            order="received_date desc", limit=auto_limit)

        blocking, warning, change = [], [], []
        clean_ids = []
        for rec in pending:
            if rec.document_type == "change_order":
                change.append(rec)
            elif rec.blocking_issue_count > 0:
                blocking.append(rec)
            else:
                warning.append(rec)
                if rec.issue_count == 0:
                    # Truly clean but not auto-confirmed (partner has
                    # auto_confirm_clean off) — eligible for Approve-all-clean.
                    clean_ids.append(rec.id)

        group_defs = [
            ("blocking", "Blocking — needs a decision", blocking),
            ("warning", "Warnings only", warning),
            ("change", "Change orders", change),
            ("auto", "Auto-approved (clean)", auto),
        ]
        groups = []
        for key, label, recs in group_defs:
            if not recs:
                continue
            groups.append({
                "key": key,
                "label": "%s (%d)" % (label, len(recs)),
                "items": [self._queue_item(now, rec, key) for rec in recs],
            })

        counts = {
            "blocking": len(blocking),
            "warning": len(warning),
            "change": len(change),
            "auto": len(auto),
        }
        counts["all"] = counts["blocking"] + counts["warning"] + counts["change"] + counts["auto"]

        return {
            "data_available": self._data_available(partner_code),
            "partners": self._partner_options(),
            "groups": groups,
            "counts": counts,
            "clean_ids": clean_ids,
            "summary_line": self._summary_line(now, pending, partner_code),
            "degraded": self._degraded_banner(now, partner_code),
        }

    @api.model
    def _data_available(self, partner_code=None):
        return bool(self.env["edi.log"].search_count(
            self._partner_domain(partner_code) + [("direction", "=", "inbound")]))

    @api.model
    def _severity_tone(self, group_key, rec):
        if group_key == "blocking":
            return "red"
        if group_key == "auto":
            return "green"
        return "amber"

    @api.model
    def _queue_item(self, now, rec, group_key):
        tone = self._severity_tone(group_key, rec)
        age_bg, age_color = _AGE_PILL[tone]
        po = rec.customer_po_number or rec.name or ""
        ref = ("PO %s · %s" % (po, rec.store_code)) if rec.store_code else "PO %s" % po
        return {
            "id": rec.id,
            "group": group_key,
            "sev": _GROUP_DOT.get(group_key, "#ffc107"),
            "ref": ref,
            "customer": rec.trading_partner_id.name or "",
            "short": self._queue_short(rec, group_key),
            "partner_tag": self._partner_tag(rec),
            "age": self._age_label(self._age_hours(now, rec.received_date)),
            "age_bg": age_bg,
            "age_color": age_color,
            "state": rec.state,
        }

    @api.model
    def _issue_summary(self, rec):
        """First blocking issue's description, else first issue's, else ''."""
        issues = rec.issue_ids
        blocking = issues.filtered(lambda i: i.severity == "blocking")
        target = blocking[:1] or issues[:1]
        if not target or not target.description:
            return ""
        return target.description.splitlines()[0][:200]

    @api.model
    def _queue_short(self, rec, group_key):
        if group_key == "auto":
            return "Clean · auto-confirmed · shown for audit"
        if group_key == "change":
            return rec.change_summary and rec.change_summary.splitlines()[0][:120] \
                or "Change order (ORDCHG) · applies a diff to the existing SO"
        summary = self._issue_summary(rec)
        if summary:
            return summary
        if rec.blocking_issue_count:
            return "%d blocking issue(s) · ORDRSP held for the whole PO" % rec.blocking_issue_count
        if rec.warning_count:
            return "%d warning(s) · acknowledged in the ORDRSP" % rec.warning_count
        return "No issues — parsed clean, awaiting approval"

    @api.model
    def _summary_line(self, now, pending, partner_code):
        scope = "all partners"
        if partner_code and partner_code != "all":
            p = self._partner_for_code(partner_code)
            scope = p.name if p else partner_code
        if not pending:
            return "0 pending · %s" % scope
        oldest = min(pending.mapped("received_date"))
        return "%d pending · oldest %s · %s" % (
            len(pending), self._age_label(self._age_hours(now, oldest)), scope)

    @api.model
    def _degraded_banner(self, now, partner_code):
        """Name a partner whose ORDRSP uploads look degraded (>=2 failed ACK
        sends in 24h), else None. One batched search over ``edi.log``, grouped in
        Python (the scope is one or a few partners) — search-based to match the
        module's style and stay clear of read_group's shifting semantics.

        The copy is derived from real data only — the count of failed ORDRSP
        (``ack_sent``) log rows in scope, plus the first line of the most recent
        failure's own message. It never asserts a transport ("SFTP"), a route
        ("EDIS VAN") or a shared error signature the backend has not inspected:
        those would be a fabricated diagnosis (a Briscoes/Animates upload can be
        plain FTP, not SFTP). See the module bar at the top of this file.
        """
        from datetime import timedelta
        window = now - timedelta(hours=24)
        logs = self.env["edi.log"].search(
            self._partner_domain(partner_code) + [
                ("event_type", "=", "ack_sent"), ("status", "=", "error"),
                ("timestamp", ">=", window),
            ], order="timestamp desc")
        counts = {}
        latest = {}
        for log in logs:
            partner = log.trading_partner_id
            if partner:
                counts[partner] = counts.get(partner, 0) + 1
                # search is newest-first, so the first row seen per partner is
                # its most recent failure.
                latest.setdefault(partner, log)
        # Deterministic: the worst-offending partner first, then by name.
        for partner, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].name or "")):
            if count >= 2:
                text = ("%s ORDRSP upload looks degraded — %d failed sends in the "
                        "last 24h. Resolve reviews, but check the partner before "
                        "forcing ACK retries." % (partner.name, count))
                first_line = self._first_line(latest.get(partner))
                if first_line:
                    text += " Latest error: %s" % first_line
                return {"partner_id": partner.id, "text": text}
        return None

    @api.model
    def _first_line(self, log):
        """First non-empty line of a log's message, truncated — or '' if none.
        Real data only; used to colour the degraded banner with the actual
        failure rather than an assumed cause."""
        if not log or not log.message:
            return ""
        for line in log.message.splitlines():
            line = line.strip()
            if line:
                return line[:160]
        return ""

    # =====================================================================
    #  DETAIL — single review pane / page
    # =====================================================================

    @api.model
    def get_review_detail(self, review_id):
        """Full payload for one review — powers both the queue's detail pane and
        the standalone Order Review Detail page.

        Returns None if the id is gone (the frontend shows an empty pane). The
        payload is a superset: the queue pane renders header/hint/context/issues/
        payload/SO-lines; the detail page additionally renders the ORDRSP
        deferral panel, lifecycle timeline and message log.
        """
        rec = self.env["edi.order.review"].browse(review_id).exists()
        if not rec:
            return None
        now = fields.Datetime.now()
        tone = self._detail_tone(rec)
        age_bg, age_color = _AGE_PILL[tone]
        state_label, state_cls = _STATE_CHIP.get(rec.state, (rec.state, "edi-chip-grey"))
        hint_tone = self._hint_tone(rec)
        hint_bg, hint_fg = _HINT_TONE[hint_tone]
        po = rec.customer_po_number or rec.name or ""

        return {
            "id": rec.id,
            "ref": ("PO %s · store %s" % (po, rec.store_code)) if rec.store_code else "PO %s" % po,
            "name": rec.name or "",
            "state": rec.state,
            "state_label": state_label,
            "state_cls": state_cls,
            "partner_tag": self._partner_tag(rec),
            "doc_label": "CHANGE ORDER" if rec.document_type == "change_order" else "NEW ORDER",
            "doc_is_change": rec.document_type == "change_order",
            "age": self._age_label(self._age_hours(now, rec.received_date)),
            "age_bg": age_bg,
            "age_color": age_color,
            "received": self._fmt_dt(rec.received_date),
            "subtitle": self._detail_subtitle(rec),
            "hint": self._diagnosis_hint(rec),
            "hint_bg": hint_bg,
            "hint_fg": hint_fg,
            "ack": self._ack_chip(rec),
            "context": self._context_grid(rec),
            "issues": self._detail_issues(rec),
            "issue_count": rec.issue_count,
            "issue_summary": self._issue_count_summary(rec),
            "no_issues": rec.issue_count == 0,
            "mono_label": self._mono_label(rec),
            "mono": self._mono_lines(rec),
            "so_lines": self._so_lines(rec),
            "line_note": self._line_note(rec),
            "so": self._so_meta(rec),
            "actions": self._detail_actions(rec),
            "ordrsp": self._ordrsp_panel(rec),
            "timeline": self._timeline(now, rec),
            "chatter": self._chatter(now, rec),
        }

    @api.model
    def _detail_tone(self, rec):
        if rec.state == "auto_approved":
            return "green"
        if rec.state == "pending_review" and rec.blocking_issue_count:
            return "red"
        return "amber"

    @api.model
    def _hint_tone(self, rec):
        if rec.document_type == "change_order" or rec.state == "auto_approved":
            return "info"
        if rec.blocking_issue_count:
            return "red"
        return "amber"

    @api.model
    def _detail_subtitle(self, rec):
        fmt = dict(rec.trading_partner_id._fields["edi_format"].selection).get(
            rec.trading_partner_id.edi_format, rec.trading_partner_id.edi_format or "")
        if rec.state == "auto_approved":
            return ("%s · no blocking issues · auto_confirm_clean is on · "
                    "SO confirmed automatically" % fmt)
        if rec.document_type == "change_order":
            return "%s · change order (ORDCHG) · applies a diff to the existing SO" % fmt
        if rec.store_code:
            return ("%s · one store-order of PO %s · the per-PO ORDRSP is held "
                    "until every store resolves" % (fmt, rec.customer_po_number or ""))
        return "%s · single-SO partner · ORDRSP deferred until resolved" % fmt

    @api.model
    def _diagnosis_hint(self, rec):
        """A lightbulb 'what to do' line derived from the review's real state."""
        if rec.state == "auto_approved":
            return ("Auto-approved because every line matched and priced within "
                    "tolerance. Listed for audit only — no action needed. Its ORDRSP "
                    "goes out with the rest of the PO once every store is resolved.")
        if rec.document_type == "change_order":
            return ("This is a change order, not a new one. Approving applies the "
                    "parsed diff to the existing SO rather than creating a new one, "
                    "then sends a fresh per-exchange ORDRSP. Review the change "
                    "summary before applying.")
        types = set(rec.issue_ids.mapped("issue_type"))
        if "product_not_found" in types:
            return ("A product is not in Odoo on any match field (barcode → internal "
                    "ref → vendor code → supplier SKU). Create/activate it and "
                    "re-match, or reject the line — the per-PO ORDRSP cannot go out "
                    "while any store on this PO is pending.")
        if "unknown_store" in types:
            return ("The store code is not in the partner store map, so this "
                    "store-order has no ship-to. Seed the store partner "
                    "(Configuration → Trading Partner → Stores) then re-split — do "
                    "NOT deliver it to head office.")
        if "price_discrepancy" in types:
            return ("One or more lines are priced above tolerance versus the "
                    "pricelist. Accept the EDI prices as-is, correct each to the "
                    "system price, or flag the pricelist for update. The ORDRSP "
                    "echoes whatever you confirm.")
        if "qty_shortfall" in types:
            return ("This partner is short_ship: at approve the lines are re-clamped "
                    "to what the fulfilment DC can actually ship. Review the cuts — "
                    "the ORDRSP acknowledges the reduced quantities.")
        if rec.warning_count:
            return ("Warnings at approve time — they are acknowledged in the ORDRSP. "
                    "Review, then approve to confirm the SO.")
        return ("No issues — this order parsed clean and matched on every line. "
                "Approve to confirm the SO and acknowledge in the ORDRSP.")

    @api.model
    def _ack_chip(self, rec):
        """ORDRSP status chip from the review's computed ack_status."""
        meta = {
            "pending": ("Deferred", "var(--edi-muted)"),
            "queued": ("Queued", "var(--edi-muted)"),
            "sent": ("ORDRSP sent", "#198754"),
            "failed": ("ORDRSP failed", "#dc3545"),
        }
        label, color = meta.get(rec.ack_status, ("Deferred", "var(--edi-muted)"))
        return {"status": rec.ack_status, "label": label, "color": color}

    @api.model
    def _context_grid(self, rec):
        so = rec.sale_order_id
        so_label = "%s (%s)" % (so.name, so.state) if so else "— not created"
        return [
            {"k": "Partner", "v": rec.trading_partner_id.name or "—"},
            {"k": "PO number", "v": rec.customer_po_number or "—"},
            {"k": "Store", "v": rec.store_code or "— single"},
            {"k": "Sales order", "v": so_label},
            {"k": "Lines", "v": str(len(rec.sale_order_line_ids)) if so else "—"},
            {"k": "Received", "v": self._fmt_dt(rec.received_date)},
            {"k": "Document", "v": "Change order" if rec.document_type == "change_order" else "New order"},
            {"k": "ORDRSP", "v": self._ack_chip(rec)["label"]},
        ]

    @api.model
    def _issue_count_summary(self, rec):
        if not rec.issue_count:
            return ""
        return "%d blocking · %d warning" % (rec.blocking_issue_count, rec.warning_count)

    @api.model
    def _detail_issues(self, rec):
        rows = []
        for iss in rec.issue_ids:
            badge, badge_cls = _SEV_BADGE.get(iss.severity, ("INFO", "edi-chip-grey"))
            resolved = iss.resolution != "pending"
            res_label, res_cls = _RESOLUTION_CHIP.get(iss.resolution, (None, None))
            has_price = iss.issue_type == "price_discrepancy" and bool(iss.system_price)
            actions = self._issue_actions(iss)
            rows.append({
                "id": iss.id,
                "badge": badge,
                "badge_cls": badge_cls,
                "type_label": _ISSUE_TYPE_LABEL.get(iss.issue_type, iss.issue_type),
                "desc": (iss.description or "").strip(),
                "resolved": resolved,
                "res_label": res_label,
                "res_cls": res_cls,
                "has_price": has_price,
                "edi_price": ("%.2f" % iss.edi_price) if has_price else "",
                "sys_price": ("%.2f" % iss.system_price) if has_price else "",
                # SIGNED for display: edi.order.issue.price_difference_pct is
                # abs(), so it would render an EDI price BELOW system as "+9.9%"
                # — a false "priced above" signal in a price-decision UI. Recompute
                # the sign from the raw prices (has_price already guarantees
                # system_price is non-zero).
                "diff": ("%+.1f%%" % (
                    (iss.edi_price - iss.system_price) / iss.system_price * 100.0
                )) if has_price else "",
                "show_actions": (not resolved) and bool(actions),
                "actions": actions,
            })
        return rows

    @api.model
    def _issue_actions(self, iss):
        """Per-issue resolution actions keyed to real edi.order.issue methods.

        Only offered while the review is still pending (a resolved review's
        issues are read-only history) and the issue itself unresolved.
        """
        if iss.review_id.state != "pending_review":
            return []
        if iss.issue_type == "price_discrepancy":
            return [
                {"key": "accept", "label": "Accept EDI"},
                {"key": "correct", "label": "Correct to system"},
                {"key": "reject", "label": "Reject line"},
            ]
        if iss.issue_type == "product_not_found":
            return [{"key": "accept", "label": "Accept"}, {"key": "reject", "label": "Reject line"}]
        if iss.issue_type == "unknown_store":
            return [{"key": "reject", "label": "Reject"}]
        if iss.severity == "info":
            return [{"key": "accept", "label": "Accept"}]
        return [{"key": "accept", "label": "Accept"}, {"key": "reject", "label": "Reject line"}]

    # ---- raw payload block ---------------------------------------------------

    @api.model
    def _mono_label(self, rec):
        if rec.document_type == "change_order" and rec.change_summary:
            return "Change summary (diff)"
        fmt = rec.trading_partner_id.edi_format or ""
        if fmt.startswith("idoc"):
            return "Inbound iDOC (as received)"
        if fmt.startswith("edifact"):
            return "Inbound EDIFACT (as received)"
        return "Inbound payload (as received)"

    @api.model
    def _mono_lines(self, rec):
        """Raw inbound payload split into lines.

        ``edi_raw_data`` is manager-restricted (groups=group_edi_manager); a
        non-manager gets ``False`` for the field, so we show an honest note
        instead of the payload rather than leaking it. Change orders show the
        parsed diff (change_summary), which is not manager-gated.
        """
        if rec.document_type == "change_order" and rec.change_summary:
            return [{"text": ln, "warn": False} for ln in rec.change_summary.splitlines()]
        # Check the group BEFORE touching the manager-restricted field, so a
        # non-manager RPC never even reads it.
        if not self.env.user.has_group("mml_edi.group_edi_manager"):
            return [{"text": "Raw payload is visible to EDI managers only.", "warn": False}]
        raw = rec.edi_raw_data
        if not raw:
            return [{"text": "No raw payload stored for this review.", "warn": False}]
        # Cap to a sane number of lines for the block; the "view full · download"
        # link (frontend) reaches the record for the complete file.
        lines = raw.splitlines()[:80]
        return [{"text": ln, "warn": False} for ln in lines]

    # ---- SO lines with matched-by cascade badge ------------------------------

    @api.model
    def _so_lines(self, rec):
        """SO lines with a best-effort matched-by badge + a status chip.

        Matched-by is derived from what the product actually carries and any
        issue attached to the line — honest, not fabricated: a line whose
        product is missing (product_not_found issue points at it, or it has no
        product) reads 'not found'; a fallback-matched line reads 'vendor code';
        otherwise barcode / internal ref by what the product has.
        """
        so = rec.sale_order_id
        if not so:
            return []
        # Map sale_order_line_id -> issue for the line-status/ match overlay
        # (one pass, no per-line search).
        issue_by_line = {}
        for iss in rec.issue_ids:
            if iss.sale_order_line_id:
                issue_by_line.setdefault(iss.sale_order_line_id.id, iss)
        rows = []
        for idx, line in enumerate(rec.sale_order_line_ids, start=1):
            iss = issue_by_line.get(line.id)
            match_by, match_cls = self._match_badge(line, iss)
            status, status_cls = self._line_status(line, iss)
            rows.append({
                "no": "%05d" % (idx * 10),
                "product": line.product_id.display_name or line.name or "—",
                "match_by": match_by,
                "match_cls": match_cls,
                "qty": "%g %s" % (line.product_uom_qty, self._uom_code(line)),
                "edi_price": "%.2f" % line.price_unit,
                "sys_price": self._system_price(rec, line),
                "status": status,
                "status_cls": status_cls,
            })
        return rows

    @api.model
    def _uom_code(self, line):
        name = (line.product_uom_id.name or "").strip() if line.product_uom_id else ""
        return "EA" if name.lower() in ("units", "unit", "each", "ea", "") else name

    @api.model
    def _match_badge(self, line, iss):
        if iss and iss.issue_type == "product_not_found":
            return "not found", "edi-chip-red"
        if iss and iss.issue_type == "product_matched_by_fallback":
            return "vendor code", "edi-chip-amber"
        product = line.product_id
        if not product:
            return "not found", "edi-chip-red"
        if product.barcode:
            return "barcode", "edi-chip-green"
        if product.default_code:
            return "internal ref", "edi-chip-green"
        return "matched", "edi-chip-grey"

    @api.model
    def _line_status(self, line, iss):
        if iss:
            if iss.issue_type in ("product_not_found", "unknown_store"):
                return "Blocked", "edi-pill-red"
            if iss.issue_type == "price_discrepancy":
                return "Price", "edi-pill-amber"
            if iss.issue_type == "qty_shortfall":
                return "Short", "edi-pill-amber"
            if iss.issue_type == "uom_mismatch":
                return "UOM", "edi-pill-amber"
        return "OK", "edi-pill-green"

    @api.model
    def _system_price(self, rec, line):
        """The pricelist price for this line's product, for the EDI-vs-system
        column. '—' when there is no product or pricelist to compare against.

        Uses the same 2-arg ``_get_product_price(product, quantity)`` call
        edi.processor._get_pricelist_price relies on (Odoo 17+ dropped the
        partner/uom positional args); any failure degrades to '—' rather than
        breaking the pane.
        """
        pricelist = rec.trading_partner_id.pricelist_id
        if not line.product_id or not pricelist:
            return "—"
        try:
            price = pricelist._get_product_price(line.product_id, line.product_uom_qty or 1.0)
        except Exception:  # noqa: BLE001 — display path, never break the pane
            return "—"
        return "%.2f" % price

    @api.model
    def _line_note(self, rec):
        fmt = rec.trading_partner_id.edi_format or ""
        base = "Prices are ex-GST"
        if rec.trading_partner_id.pricelist_id:
            base += ' and compared against the "%s" pricelist' % rec.trading_partner_id.pricelist_id.name
        if fmt.startswith("idoc"):
            base += ". iDOC ships EA on this PO; a carton line is converted to eaches before matching."
        else:
            base += "."
        return base

    @api.model
    def _so_meta(self, rec):
        so = rec.sale_order_id
        if not so:
            return {"id": False, "name": "", "label": "— not created"}
        return {
            "id": so.id,
            "name": so.name,
            "label": "%s · %s · %d lines" % (so.name, so.state, len(rec.sale_order_line_ids)),
        }

    # ---- header actions ------------------------------------------------------

    @api.model
    def _detail_actions(self, rec):
        """Header action buttons keyed to real edi.order.review methods.

        Keys map (frontend) to ORM calls: approve -> action_approve,
        approve_corrected -> action_approve_corrected, reject -> action_reject,
        reset -> action_reset_to_review (manager), open_so -> open the SO form.
        """
        is_manager = self.env.user.has_group("mml_edi.group_edi_manager")
        actions = []
        if rec.state == "pending_review":
            if rec.document_type == "change_order":
                actions.append({"key": "approve", "label": "Apply change", "kind": "primary"})
            else:
                primary = "Approve"
                if rec.blocking_issue_count and "product_not_found" in rec.issue_ids.mapped("issue_type"):
                    primary = "Match & approve"
                actions.append({"key": "approve", "label": primary, "kind": "primary"})
                if rec.issue_count:
                    actions.append({"key": "approve_corrected", "label": "Approve with corrections", "kind": "secondary"})
            actions.append({"key": "reject", "label": "Reject", "kind": "secondary"})
            if is_manager:
                actions.append({"key": "reset", "label": "Reset", "kind": "secondary"})
        else:
            if rec.sale_order_id:
                actions.append({"key": "open_so", "label": "Open SO", "kind": "secondary"})
            if is_manager:
                actions.append({"key": "reset", "label": "Reset to review", "kind": "secondary"})
        return actions

    # ---- ORDRSP per-PO deferral panel ---------------------------------------

    @api.model
    def _ordrsp_panel(self, rec):
        """Per-PO ORDRSP progress: sibling store-reviews of the same PO, how many
        are resolved, and which one holds the ORDRSP. Mirrors the deferral logic
        in ``edi.order.review._queue_ack`` — one search over siblings.
        """
        siblings = self.env["edi.order.review"].search([
            ("trading_partner_id", "=", rec.trading_partner_id.id),
            ("customer_po_number", "=", rec.customer_po_number),
        ], order="store_code")
        total = len(siblings)
        resolved = siblings.filtered(lambda r: r.state != "pending_review")
        stores = []
        for sib in siblings:
            store = ("store %s" % sib.store_code) if sib.store_code else "single SO"
            if sib.id == rec.id and sib.state == "pending_review":
                label, cls = "%s · this review (pending) ⟵ holds the ORDRSP" % store, "edi-chip-amber"
            elif sib.state == "pending_review":
                label, cls = "%s · pending" % store, "edi-chip-amber"
            elif sib.state == "rejected":
                label, cls = "%s · rejected" % store, "edi-chip-red"
            else:
                label, cls = "%s · approved" % store, "edi-chip-green"
            stores.append({"label": label, "cls": cls})
        exchange_key = (rec.edi_file_hash or str(rec.id))[:8]
        filename = "ACK_%s_%s_%s.edi" % (
            rec.trading_partner_id.code, rec.customer_po_number, exchange_key)
        return {
            "filename": filename,
            "total_stores": total,
            "resolved_stores": len(resolved),
            "pct": round(len(resolved) / total * 100.0, 1) if total else 0.0,
            "all_resolved": len(resolved) == total and total > 0,
            "stores": stores,
            "status": rec.ack_status,
        }

    # ---- lifecycle timeline (from edi.log) -----------------------------------

    @api.model
    def _timeline(self, now, rec):
        """Lifecycle nodes from this review's own edi.log rows, oldest first,
        plus a synthetic trailing node for the current standing state."""
        logs = self.env["edi.log"].search(
            [("review_id", "=", rec.id)], order="timestamp asc")
        rows = []
        for log in logs:
            color, title = _TIMELINE_META.get(log.event_type, ("#6c757d", "Event"))
            if log.status == "error" and log.event_type != "error":
                color = "#dc3545"
            rows.append({
                "color": color,
                "title": title,
                "t": self._fmt_dt(log.timestamp),
                "sub": (log.message or "").splitlines()[0][:180] if log.message else "",
            })
        if rec.state == "pending_review":
            rows.append({
                "color": "var(--edi-faint)",
                "title": "Awaiting review",
                "t": "—",
                "sub": "ORDRSP for the PO is deferred until this store resolves",
            })
        return rows

    # ---- message log (chatter) ----------------------------------------------

    @api.model
    def _chatter(self, now, rec):
        """Recent chatter messages (newest last, capped) for the message log."""
        messages = rec.message_ids.sorted("date")[-8:]
        rows = []
        for msg in messages:
            author = msg.author_id
            is_system = not author
            who = self._initials(author.name if author else "System")
            body = self._plain_body(msg.body)
            if not body:
                continue
            rows.append({
                "who": who,
                "av_bg": "var(--edi-chip-bg)" if is_system else "#714B67",
                "av_color": "var(--edi-muted)" if is_system else "#fff",
                "body": body,
                "t": self._fmt_dt(msg.date),
            })
        return rows

    @api.model
    def _initials(self, name):
        parts = [p for p in (name or "").split() if p]
        if not parts:
            return "SY"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    @api.model
    def _plain_body(self, html):
        if not html:
            return ""
        from odoo.tools import html2plaintext
        return html2plaintext(html).strip()[:400]

    # ---- shared datetime formatting -----------------------------------------

    @api.model
    def _fmt_dt(self, ts):
        if not ts:
            return "—"
        import pytz
        tz = pytz.timezone(self.env.user.tz or "Pacific/Auckland")
        local = pytz.utc.localize(ts).astimezone(tz)
        return local.strftime("%d %b %H:%M")

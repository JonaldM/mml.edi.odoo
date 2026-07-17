# mml_edi/models/edi_processing_log.py
"""Processing-log browser computation service.

Backs the design-handoff "Processing Log" screen (OWL client action
``edi_processing_log``): a day-grouped, append-only view of ``edi.log`` with
direction/status segmented filters + free-text search, plus a per-file
**exchange chain** rail (SHA-256 + dedup status + manager-only technical detail)
built from the sibling log rows that share a file hash.

Two batched, read-mostly ``@api.model`` methods:
  - ``get_log_page(direction, status, query, limit)`` — the filtered row list
  - ``get_exchange_chain(log_id)`` — the right-rail bundle for one log row

AbstractModel — no DB table. The append-only ``edi.log`` (``_log_access=False``)
is never written here; this service only reads it.
"""
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# event_type -> (tag label, colour). White foreground on the coloured pill.
_TAG_META = {
    "file_download": ("POLL", "#fd7e14"),
    "file_parse": ("PARSE", "#0d6efd"),
    "order_created": ("ORDER", "#198754"),
    "duplicate_skipped": ("DUP", "#6c757d"),
    "ack_sent": ("ACK", "#714B67"),
    "review_approved": ("APPROVED", "#198754"),
    "review_rejected": ("REJECTED", "#dc3545"),
    "po_change_applied": ("ORDCHG", "#0d6efd"),
    "error": ("ERROR", "#dc3545"),
    "ftp_connection": ("FTP", "#6c757d"),
    "info": ("INFO", "#6c757d"),
}
# status -> (label, subtle bg, subtle fg) — README subtle chip pairs.
_STATUS_META = {
    "success": ("OK", "#d1e7dd", "#0a3622"),
    "warning": ("Warn", "#fff3cd", "#664d03"),
    "error": ("Error", "#f8d7da", "#58151c"),
}

# chain-node colours by (event_type, status).
_CHAIN_COLOR = {
    "file_download": "#fd7e14",
    "file_parse": "#0d6efd",
    "order_created": "#198754",
    "duplicate_skipped": "#6c757d",
    "review_approved": "#198754",
    "review_rejected": "#dc3545",
    "po_change_applied": "#0d6efd",
    "ftp_connection": "#6c757d",
    "info": "#6c757d",
    "error": "#dc3545",
}
_CHAIN_TITLE = {
    "file_download": "File downloaded",
    "file_parse": "Parsed & split",
    "order_created": "Order created",
    "duplicate_skipped": "Duplicate skipped",
    "review_approved": "Review approved",
    "review_rejected": "Review rejected",
    "po_change_applied": "PO change applied",
    "ftp_connection": "Connection",
    "info": "Note",
    "error": "Error",
}


class EdiProcessingLog(models.AbstractModel):
    """Processing-log browser + exchange-chain computation service (no table)."""

    _name = "edi.processing.log"
    _description = "EDI Processing Log Browser"
    _table = "edi_processing_log"

    # ---- row list ------------------------------------------------------------

    @api.model
    def get_log_page(self, direction="all", status="all", query=None, limit=200):
        """Return the filtered, day-labelled log rows (newest first).

        ``direction`` / ``status`` map to the segmented filters ('all' = no
        constraint); ``query`` free-text-matches filename + message (ILIKE, ORed).
        The result is capped at ``limit`` rows (append-only history is unbounded);
        each row carries a ``day`` label so the frontend inserts day dividers, and
        a ``selectable`` flag (a file-bearing row opens the exchange rail).
        """
        now = fields.Datetime.now()
        domain = []
        if direction in ("inbound", "outbound", "internal"):
            domain.append(("direction", "=", direction))
        if status in ("success", "warning", "error"):
            domain.append(("status", "=", status))
        q = (query or "").strip()
        if q:
            domain += ["|", ("filename", "ilike", q), ("message", "ilike", q)]

        logs = self.env["edi.log"].search(domain, order="timestamp desc", limit=limit)
        rows = [self._row(now, log) for log in logs]
        first_selectable = next((r["id"] for r in rows if r["selectable"]), None)
        return {"rows": rows, "count": len(rows), "first_selectable": first_selectable}

    @api.model
    def _row(self, now, log):
        tag, tag_color = _TAG_META.get(log.event_type, ("—", "#6c757d"))
        st_label, st_bg, st_fg = _STATUS_META.get(
            log.status, (log.status or "", "#e2e3e5", "#41464b"))
        return {
            "id": log.id,
            "day": self._day_label(now, log.timestamp),
            "t": self._fmt_hm(log.timestamp),
            "tag": tag,
            "tag_color": tag_color,
            "message": (log.message or "").splitlines()[0][:200] if log.message else "",
            "filename": log.filename or "",
            "partner_tag": log.trading_partner_id.name or "",
            "has_file": bool(log.filename),
            "status": st_label,
            "status_bg": st_bg,
            "status_fg": st_fg,
            "selectable": bool(log.filename or log.file_hash),
        }

    # ---- exchange chain (right rail) ----------------------------------------

    @api.model
    def get_exchange_chain(self, log_id):
        """Return the file-exchange bundle for one log row.

        Gathers the sibling log rows that share this row's file hash (or, absent a
        hash, its filename) into a timestamp-ordered lifecycle chain, and derives
        the dedup status + SHA-256 descriptor. The manager-only technical detail
        (``edi.log.detail``, a ``group_edi_manager`` field) is included only for
        managers — mirroring the record-level restriction.
        """
        log = self.env["edi.log"].browse(log_id)
        if not log.exists():
            return None

        siblings = self._siblings(log)
        is_manager = self.env.user.has_group("mml_edi.group_edi_manager")

        detail = ""
        if is_manager:
            err = siblings.filtered(lambda l: l.status == "error" and l.detail)
            if err:
                detail = err.sorted("timestamp")[-1].detail or ""

        return {
            "id": log.id,
            "filename": log.filename or (log.trading_partner_id.code or "exchange"),
            "partner": (log.trading_partner_id.name or "").upper(),
            "dedup": self._dedup_status(siblings),
            "hash": self._hash_descriptor(log, siblings),
            "chain": [self._chain_node(position, node, siblings)
                      for position, node in enumerate(siblings.sorted("timestamp"))],
            "has_detail": bool(detail),
            "detail": detail,
        }

    @api.model
    def _siblings(self, log):
        """The log rows belonging to the same file exchange."""
        Log = self.env["edi.log"]
        if log.file_hash:
            return Log.search([("file_hash", "=", log.file_hash)])
        if log.filename:
            dom = [("filename", "=", log.filename)]
            if log.trading_partner_id:
                dom.append(("trading_partner_id", "=", log.trading_partner_id.id))
            return Log.search(dom)
        return log

    @api.model
    def _dedup_status(self, siblings):
        """Green/amber dedup chip derived from the exchange's real log rows."""
        has_dup = any(s.event_type == "duplicate_skipped" for s in siblings)
        ack = siblings.filtered(lambda s: s.event_type == "ack_sent")
        ack_ok = any(s.status == "success" for s in ack)
        ack_err = any(s.status == "error" for s in ack)
        if has_dup:
            return {"label": "duplicate — skipped", "ok": False}
        if ack_err and not ack_ok:
            return {"label": "retry armed", "ok": False}
        if ack_ok:
            return {"label": "sent once · idempotent", "ok": True}
        return {"label": "processed once", "ok": True}

    @api.model
    def _hash_descriptor(self, log, siblings):
        """SHA-256 short form + a plain-language descriptor of what it keys."""
        h = log.file_hash or ""
        if not h:
            return "— no hash recorded"
        short = "%s…%s" % (h[:12], h[-6:]) if len(h) > 20 else h
        has_dup = any(s.event_type == "duplicate_skipped" for s in siblings)
        note = "matched a processed file" if has_dup else "exchange key"
        return "%s (%s)" % (short, note)

    @api.model
    def _chain_node(self, position, log, siblings):
        color = _CHAIN_COLOR.get(log.event_type, "#6c757d")
        title = _CHAIN_TITLE.get(log.event_type, log.event_type or "Event")
        if log.event_type == "ack_sent":
            if log.status == "success":
                title, color = "ORDRSP sent", "#714B67"
            elif log.status == "error":
                title, color = "ORDRSP upload failed", "#dc3545"
            else:
                title, color = "Upload claimed", "#714B67"
        elif log.status == "error":
            color = "#dc3545"
        return {
            "title": title,
            "t": self._fmt_stamp(log.timestamp),
            "color": color,
            "sub": (log.message or "").splitlines()[0][:160] if log.message else "",
            "line": position < len(siblings) - 1,
        }

    # ---- time formatting -----------------------------------------------------

    @api.model
    def _local(self, ts):
        import pytz
        tz = pytz.timezone(self.env.user.tz or "Pacific/Auckland")
        return pytz.utc.localize(ts).astimezone(tz)

    @api.model
    def _fmt_hm(self, ts):
        return self._local(ts).strftime("%H:%M") if ts else "--:--"

    @api.model
    def _fmt_stamp(self, ts):
        # Non-padded day, portably (Windows has no %-d / %#d parity with POSIX).
        if not ts:
            return ""
        local = self._local(ts)
        return "%d %s %s" % (local.day, local.strftime("%b"), local.strftime("%H:%M"))

    @api.model
    def _day_label(self, now, ts):
        """'Today · 16 Jul' / 'Yesterday · 15 Jul' / '12 Jul' (operator-local)."""
        if not ts:
            return "—"
        local = self._local(ts)
        today = self._local(now).date()
        d = local.date()
        # Non-padded day + month, built without platform-specific strftime codes.
        stamp = "%d %s" % (d.day, local.strftime("%b"))
        if d == today:
            return "Today · " + stamp
        if d == today - timedelta(days=1):
            return "Yesterday · " + stamp
        if d.year != today.year:
            return "%s %d" % (stamp, d.year)
        return stamp

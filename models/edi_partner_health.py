# mml_edi/models/edi_partner_health.py
"""Trading-partner health computation service.

Aggregates the design-handoff "Trading Partner Health" screen payload from the
three EDI models (``edi.trading.partner``, ``edi.log``, ``edi.order.review``)
for the OWL client action ``edi_partner_health``. Mirrors the batched,
read-mostly ``@api.model`` contract established by ``edi.dashboard``: ONE call
returns the whole board — the 24h exchange-queue strip, the partner rail, and a
per-partner detail bundle (health chip + diagnosis + stats + circuit breaker +
transport + 7-day throughput + recent exchanges) — so the frontend swaps the
selected partner client-side with no extra RPC.

Trading partners are inherently few (one config record per retail partner), so
the payload embeds every partner's detail in a single response. All aggregation
is done with batched ``_read_group`` passes (no per-partner search loop) to hold
to the "no N+1 per card" rule.

AbstractModel — no DB table. Called from the frontend via
``this.orm.call('edi.partner.health', 'get_health_summary', [])``.
"""
import logging
from datetime import datetime, timedelta

from odoo import api, fields, models

from .edi_trading_partner import circuit_is_open

_logger = logging.getLogger(__name__)


# ---- health_state -> presentation (fixed hexes, both themes) ----------------
_HEALTH_LABELS = {
    "ok": "Healthy",
    "review": "Awaiting review",
    "error": "Recent errors",
    "circuit_open": "Circuit open",
    "never": "Not connected",
}
_HEALTH_DOTS = {
    "ok": "#198754",
    "review": "#ffc107",
    "error": "#dc3545",
    "circuit_open": "#dc3545",
    "never": "#adb5bd",
}
# Health chip subtle bg/fg pairs (README "Subtle chip pairs").
_HEALTH_CHIP = {
    "ok": ("#d1e7dd", "#0a3622"),
    "review": ("#fff3cd", "#664d03"),
    "error": ("#f8d7da", "#58151c"),
    "circuit_open": ("#f8d7da", "#58151c"),
    "never": ("#e2e3e5", "#41464b"),
}
# Diagnosis-hint tone bg/fg pairs.
_HINT_TONES = {
    "amber": ("#fff3cd", "#664d03"),
    "red": ("#f8d7da", "#58151c"),
    "info": ("#cff4fc", "#055160"),
    "green": ("#d1e7dd", "#0a3622"),
}
_HINT_TONE_FOR = {
    "ok": "green",
    "review": "amber",
    "error": "red",
    "circuit_open": "red",
    "never": "info",
}

# Circuit-breaker chip bg/fg per label.
_CIRCUIT_CHIP = {
    "CLOSED": ("#d1e7dd", "#0a3622"),
    "OPEN": ("#f8d7da", "#58151c"),
    "HALF-OPEN": ("#fff3cd", "#664d03"),
    "N/A": ("#e2e3e5", "#41464b"),
}

# recent-exchanges feed tag -> label + colour (shared with the dashboard feed).
_FEED_TAGS = {
    "file_download": ("POLL", "#fd7e14"),
    "file_parse": ("PARSE", "#0d6efd"),
    "order_created": ("ORDER", "#198754"),
    "ack_sent": ("ACK", "#714B67"),
    "duplicate_skipped": ("DUP", "#6c757d"),
    "review_approved": ("REVIEW", "#198754"),
    "review_rejected": ("REVIEW", "#dc3545"),
    "po_change_applied": ("CHANGE", "#0dcaf0"),
    "error": ("EXCEPTION", "#dc3545"),
    "ftp_connection": ("POLL", "#fd7e14"),
    "info": ("INFO", "#6c757d"),
}


class EdiPartnerHealth(models.AbstractModel):
    """Trading-partner health/diagnosis computation service (no DB table)."""

    _name = "edi.partner.health"
    _description = "EDI Trading Partner Health"
    _table = "edi_partner_health"

    # ---- main entry point ----------------------------------------------------

    @api.model
    def get_health_summary(self):
        """Return the full Trading-Partner-Health payload.

        Payload keys:
          - ``queue`` — the 24h exchange-queue strip (Polled → Failed)
          - ``partners`` — the left-rail rows (dot + name + fmt + health + sub)
          - ``details`` — {partner_id: per-partner detail bundle}
          - ``subtitle`` — the sub-header count line
          - ``settings_action`` — xmlid the "Open settings" button opens
        """
        now = fields.Datetime.now()
        partners = self.env["edi.trading.partner"].search([], order="name")

        health = self._batched_health(now, partners)
        on_time = self._batched_on_time(now, partners)
        throughput = self._batched_throughput(now, partners)
        exchanges = self._batched_exchanges(now, partners)

        rows = []
        details = {}
        for p in partners:
            h = health[p.id]
            rows.append(self._partner_row(p, h))
            details[str(p.id)] = self._partner_detail(
                now, p, h, on_time.get(p.id), throughput[p.id], exchanges.get(p.id, []))

        active = len(partners.filtered("active"))
        scoped = len(partners) - active
        return {
            "queue": self._exchange_queue(now),
            "partners": rows,
            "details": details,
            "subtitle": self._subtitle(active, scoped),
            "settings_action": "mml_edi.action_edi_trading_partner",
        }

    # ---- 24h exchange-queue strip -------------------------------------------

    @api.model
    def _exchange_queue(self, now):
        """The six exchange-queue lanes over the last 24h (all partners).

        Every lane is a single ``search_count`` over ``edi.log`` (six counts,
        not per-partner) so the strip is a cheap radiator. "ACK queued" is the
        pre-upload claim marker (an ``ack_sent`` row logged at ``warning`` status
        before the upload is confirmed); "ACK sent" is a successful upload;
        "Failed" is an errored upload. The ramp colours are the README pipeline
        tokens (light-bg + dark-fg, read in both schemes).
        """
        Log = self.env["edi.log"]
        cutoff = now - timedelta(hours=24)

        def _c(event_type, status=None):
            dom = [("event_type", "=", event_type), ("timestamp", ">=", cutoff)]
            if status:
                dom.append(("status", "=", status))
            return Log.search_count(dom)

        lanes = [
            {"label": "Polled", "n": _c("file_download"), "bg": "#E6F1FB", "fg": "#0C447C"},
            {"label": "Parsed", "n": _c("file_parse"), "bg": "#E6F1FB", "fg": "#0C447C"},
            {"label": "Orders", "n": _c("order_created"), "bg": "#85B7EB", "fg": "#042C53"},
            {"label": "ACK queued", "n": _c("ack_sent", "warning"), "bg": "#FAEEDA", "fg": "#854F0B"},
            {"label": "ACK sent", "n": _c("ack_sent", "success"), "bg": "#378ADD", "fg": "#fff"},
            {"label": "Failed", "n": _c("ack_sent", "error"), "bg": "#FCEBEB", "fg": "#A32D2D"},
        ]
        for i, lane in enumerate(lanes):
            lane["arrow"] = i < len(lanes) - 1
        return lanes

    # ---- batched health aggregates ------------------------------------------

    @api.model
    def _batched_health(self, now, partners):
        """{partner_id: {state, last_poll, pending, errors24h}} in 3 read_groups.

        Replaces the per-record ``edi.trading.partner._compute_health`` loop with
        three grouped passes over the whole partner set, then derives the
        worst-first ``health_state`` (same precedence as the model compute:
        circuit_open > error > review > ok, with never when nothing was ever
        polled).
        """
        Log = self.env["edi.log"]
        Review = self.env["edi.order.review"]
        cutoff24 = now - timedelta(hours=24)
        ids = partners.ids

        last_poll = {}
        for pid, last in Log._read_group(
            [("trading_partner_id", "in", ids), ("direction", "=", "inbound")],
            ["trading_partner_id"], ["timestamp:max"]):
            last_poll[pid.id] = last

        pending = {}
        for pid, cnt in Review._read_group(
            [("trading_partner_id", "in", ids), ("state", "=", "pending_review")],
            ["trading_partner_id"], ["__count"]):
            pending[pid.id] = cnt

        errors = {}
        for pid, cnt in Log._read_group(
            [("trading_partner_id", "in", ids), ("status", "=", "error"),
             ("timestamp", ">=", cutoff24)],
            ["trading_partner_id"], ["__count"]):
            errors[pid.id] = cnt

        out = {}
        for p in partners:
            lp = last_poll.get(p.id)
            pend = pending.get(p.id, 0)
            err = errors.get(p.id, 0)
            if p.circuit_open_since:
                state = "circuit_open"
            elif not lp:
                state = "never"
            elif err:
                state = "error"
            elif pend:
                state = "review"
            else:
                state = "ok"
            out[p.id] = {"state": state, "last_poll": lp, "pending": pend, "errors24h": err}
        return out

    @api.model
    def _batched_on_time(self, now, partners):
        """{partner_id: on_time_pct|None} — 30d ORDRSP success rate, 1 read_group."""
        Log = self.env["edi.log"]
        cutoff = now - timedelta(days=30)
        ok = {}
        bad = {}
        for pid, status, cnt in Log._read_group(
            [("trading_partner_id", "in", partners.ids), ("event_type", "=", "ack_sent"),
             ("timestamp", ">=", cutoff)],
            ["trading_partner_id", "status"], ["__count"]):
            if not pid:
                continue
            if status == "success":
                ok[pid.id] = ok.get(pid.id, 0) + cnt
            elif status == "error":
                bad[pid.id] = bad.get(pid.id, 0) + cnt
        out = {}
        for p in partners:
            total = ok.get(p.id, 0) + bad.get(p.id, 0)
            out[p.id] = round(ok.get(p.id, 0) / total * 100.0, 1) if total else None
        return out

    @api.model
    def _batched_throughput(self, now, partners):
        """{partner_id: [{day, files, orders} x7]} — 2 read_groups, bucketed.

        Two grouped passes (file_download, order_created) over the 7-day window,
        grouped by partner + day, then zero-filled into a Mon-anchored 7-day
        array per partner (never a per-partner-per-day query).
        """
        # Bucket in UTC on BOTH sides: the day list and the read_group day
        # granularity share tz='UTC', so a log's bucket is deterministic
        # regardless of the operator's timezone (this is a rough 7-day
        # histogram, not a calendar-local report).
        Log = self.env["edi.log"].with_context(tz="UTC")
        days = [(now - timedelta(days=6 - i)).date() for i in range(7)]
        day_labels = [d.strftime("%a") for d in days]
        idx = {d: i for i, d in enumerate(days)}
        cutoff = datetime(days[0].year, days[0].month, days[0].day)

        base = {p.id: {"files": [0] * 7, "orders": [0] * 7} for p in partners}

        def _fill(event_type, key):
            for pid, day, cnt in Log._read_group(
                [("trading_partner_id", "in", partners.ids),
                 ("event_type", "=", event_type), ("timestamp", ">=", cutoff)],
                ["trading_partner_id", "timestamp:day"], ["__count"]):
                if not pid or not day:
                    continue
                d = day.date() if hasattr(day, "date") else day
                if d in idx and pid.id in base:
                    base[pid.id][key][idx[d]] += cnt

        _fill("file_download", "files")
        _fill("order_created", "orders")

        out = {}
        for p in partners:
            out[p.id] = [
                {"day": day_labels[i], "files": base[p.id]["files"][i],
                 "orders": base[p.id]["orders"][i]}
                for i in range(7)
            ]
        return out

    @api.model
    def _batched_exchanges(self, now, partners, per_partner=6):
        """{partner_id: [recent exchange rows]} bucketed from ONE ordered search.

        A single timestamp-desc search over the partner set (bounded), bucketed
        to the newest ``per_partner`` rows each — not a search per card.
        """
        if not partners:
            return {}
        limit = max(40, per_partner * len(partners) * 4)
        logs = self.env["edi.log"].search(
            [("trading_partner_id", "in", partners.ids)],
            order="timestamp desc", limit=limit)
        out = {p.id: [] for p in partners}
        for log in logs:
            pid = log.trading_partner_id.id
            if pid not in out or len(out[pid]) >= per_partner:
                continue
            tag, color = _FEED_TAGS.get(log.event_type, _FEED_TAGS["info"])
            if log.status == "error" and log.event_type != "error":
                tag, color = "EXCEPTION", "#dc3545"
            out[pid].append({
                "t": self._fmt_time(now, log.timestamp),
                "tag": tag,
                "color": color,
                "text": (log.message or "").splitlines()[0][:120] if log.message else "",
            })
        return out

    # ---- per-partner presentation -------------------------------------------

    @api.model
    def _partner_row(self, p, h):
        """Left-rail row: dot + name + format tag + health line + transport sub."""
        state = h["state"]
        proto = (p.ftp_protocol or "").upper()
        split = "per-store" if p.order_split_mode == "per_store" else "single"
        last = self._fmt_time(fields.Datetime.now(), h["last_poll"]) if h["last_poll"] else "never"
        return {
            "id": p.id,
            "code": p.code,
            "name": p.name,
            "fmt": self._fmt_label(p),
            "dot": _HEALTH_DOTS.get(state, "#adb5bd"),
            "health": _HEALTH_LABELS.get(state, state),
            "health_color": _HEALTH_DOTS.get(state, "#6c757d"),
            "sub": "%s · last poll %s · %d in review" % (proto or "—", last, h["pending"]),
            "split": split,
        }

    @api.model
    def _partner_detail(self, now, p, h, on_time, throughput, exchanges):
        state = h["state"]
        chip_bg, chip_fg = _HEALTH_CHIP.get(state, _HEALTH_CHIP["never"])
        hint_tone = _HINT_TONE_FOR.get(state, "info")
        hint_bg, hint_fg = _HINT_TONES[hint_tone]
        last = self._fmt_time(now, h["last_poll"]) if h["last_poll"] else "never"
        return {
            "id": p.id,
            "name": p.name,
            "health_label": _HEALTH_LABELS.get(state, state),
            "health_chip_bg": chip_bg,
            "health_chip_fg": chip_fg,
            "env_label": (p.environment or "").upper(),
            "subtitle": self._subtitle_for(p),
            "hint": self._hint(state, h, on_time),
            "hint_bg": hint_bg,
            "hint_fg": hint_fg,
            "stats": self._stats(last, h, on_time),
            "circuit": self._circuit(p),
            "transport": self._transport(p),
            "throughput": throughput,
            "exchanges": exchanges,
        }

    @api.model
    def _fmt_label(self, p):
        return dict(p._fields["edi_format"].selection).get(p.edi_format, p.edi_format or "")

    @api.model
    def _subtitle(self, active, scoped):
        parts = ["%d active" % active]
        if scoped:
            parts.append("%d scoped" % scoped)
        return " · ".join(parts) + " · retail order exchange"

    @api.model
    def _subtitle_for(self, p):
        """One honest line built from real config — no fabricated store names."""
        proto = (p.ftp_protocol or "").upper()
        split = "per-store split" if p.order_split_mode == "per_store" else "single order"
        bits = [self._fmt_label(p), "%s transport" % proto if proto else "", split]
        return " · ".join(b for b in bits if b)

    @api.model
    def _hint(self, state, h, on_time):
        """Diagnosis lightbulb copy, derived honestly from the health signals."""
        if state == "circuit_open":
            return ("The circuit breaker is open after repeated poll failures — "
                    "polling is paused for the cooldown. Check the transport host "
                    "before forcing a retry.")
        if state == "error":
            return ("%d error(s) in the last 24h. Review the most recent exchanges "
                    "below before the next poll — a run of the same signature usually "
                    "points at the transport or an unmapped store." % h["errors24h"])
        if state == "review":
            note = ""
            if on_time is not None and on_time < 100:
                note = (" ORDRSP on-time is %.1f%% — some acknowledgements have not "
                        "uploaded cleanly." % on_time)
            return ("Inbound polling is healthy; %d order(s) are waiting on a review "
                    "decision. Clear the queue to release each PO's ORDRSP.%s"
                    % (h["pending"], note))
        if state == "never":
            return ("Scoped but never polled: no successful inbound activity on record. "
                    "Finish the onboarding checklist (transport, parser, pricelist, test "
                    "poll) before enabling the cron.")
        return ("Polling, parsing and acknowledgement are all clean. Nothing to action — "
                "any open reviews are ordinary price/stock decisions, not connection "
                "problems.")

    @api.model
    def _stats(self, last, h, on_time):
        pend = h["pending"]
        err = h["errors24h"]
        return [
            {"k": "Last poll", "v": last, "color": "var(--edi-text)"},
            {"k": "Pending review", "v": str(pend),
             "color": "#664d03" if pend else "var(--edi-muted)"},
            {"k": "Errors (24h)", "v": str(err),
             "color": "#dc3545" if err else "#198754"},
            {"k": "ORDRSP on-time",
             "v": ("%.1f%%" % on_time) if on_time is not None else "—",
             "color": ("#198754" if on_time == 100 else "var(--edi-text)")
                      if on_time is not None else "var(--edi-muted)"},
        ]

    @api.model
    def _circuit(self, p):
        """Circuit-breaker panel — closed / open / half-open, dots + copy."""
        threshold = p.circuit_failure_threshold or 5
        fails = p.circuit_failure_count or 0
        if not p.last_poll_date and not p.circuit_open_since and fails == 0 and not p.active:
            label = "N/A"
        elif fails >= threshold:
            label = "OPEN" if circuit_is_open(p) else "HALF-OPEN"
        else:
            label = "CLOSED"
        chip_bg, chip_fg = _CIRCUIT_CHIP.get(label, _CIRCUIT_CHIP["N/A"])
        filled = min(fails, threshold)
        dots = ["#dc3545" if i < filled else "var(--edi-line)" for i in range(threshold)]
        if label == "OPEN":
            detail = ("Polling is paused for a %d-min cooldown, then one probe poll is "
                      "allowed (half-open)." % (p.circuit_cooldown_minutes or 60))
        elif label == "HALF-OPEN":
            detail = ("Cooldown elapsed — the next poll is a single probe. Success closes "
                      "the breaker; failure re-opens it.")
        elif label == "N/A":
            detail = "The circuit breaker activates once polling begins."
        else:
            detail = ("Opens after %d consecutive poll failures, then pauses polling for a "
                      "%d-min cooldown." % (threshold, p.circuit_cooldown_minutes or 60))
        return {
            "label": label,
            "chip_bg": chip_bg,
            "chip_fg": chip_fg,
            "text": "%d of %d consecutive poll failures" % (fails, threshold),
            "dots": dots,
            "detail": detail,
        }

    @api.model
    def _transport(self, p):
        """Transport panel rows. Credential presence is read via sudo (the value
        is never returned — only a pinned/not-set badge), so an EDI manager
        without base.group_system still sees connection health."""
        sudo = p.sudo()
        proto = (p.ftp_protocol or "").upper() or "—"
        host = ("%s:%s" % (p.ftp_host, p.ftp_port)) if p.ftp_host else "— not set"
        rows = [
            {"k": "Protocol", "v": proto, "badge": "ok" if p.ftp_host else ""},
            {"k": "Host", "v": host, "badge": "" if p.ftp_host else "warn"},
            {"k": "Inbox", "v": p.get_active_inbox_path() or "— not set", "badge": ""},
            {"k": "Outbox", "v": p.get_active_outbox_path() or "— not set", "badge": ""},
        ]
        if p.ftp_protocol == "sftp":
            has_key = bool(sudo.sftp_host_key)
            rows.append({
                "k": "Host key",
                "v": "pinned · strict verification" if has_key else "not set · rejects SFTP",
                "badge": "ok" if has_key else "warn",
            })
        return rows

    # ---- shared time formatting ---------------------------------------------

    @api.model
    def _fmt_time(self, now, ts):
        if not ts:
            return "never"
        import pytz
        tz = pytz.timezone(self.env.user.tz or "Pacific/Auckland")
        local = pytz.utc.localize(ts).astimezone(tz)
        age_h = (now - ts).total_seconds() / 3600.0
        return local.strftime("%H:%M" if age_h < 24 else "%a %H:%M")

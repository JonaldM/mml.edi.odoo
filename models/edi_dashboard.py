# mml_edi/models/edi_dashboard.py
"""EDI operations dashboard computation service.

Aggregates the landing-dashboard payload from the three EDI models
(``edi.order.review``, ``edi.log``, ``edi.trading.partner``) for the OWL
client action ``edi_dashboard``. Mirrors the carrier-agnostic 3PL dashboard
contract (``3pl.kpi.dashboard.get_kpi_summary``): one batched, read-mostly
``@api.model`` method returns the whole board, narrowed by an optional
trading-partner scope so the segmented "All / Briscoes / Animates" control
recomputes every number, list and feed on the screen.

Honesty caveats baked into the design copy (README "Fidelity") survive to
production: clean orders auto-approve AND are acknowledged in the same poll;
ONE ORDRSP covers all store-orders of a PO and is deferred until every
store-order is resolved. The metrics are scoped to what the EDI models
actually record — no fabricated signal.

AbstractModel — no DB table. Every method is called from the frontend via
``this.orm.call('edi.dashboard', 'get_dashboard_summary', [], {...})``.
"""
import logging
import statistics
from datetime import datetime, timedelta

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


# ---- KPI RAG helpers (higher- and lower-is-better) --------------------------

def _rag_higher_better(value, target, amber_floor):
    """'green' / 'amber' / 'red' for a higher-is-better KPI (None -> 'none')."""
    if value is None:
        return "none"
    if value >= target:
        return "green"
    if value >= amber_floor:
        return "amber"
    return "red"


def _rag_lower_better(value, target, amber_ceiling):
    """'green' / 'amber' / 'red' for a lower-is-better KPI (None -> 'none')."""
    if value is None:
        return "none"
    if value <= target:
        return "green"
    if value <= amber_ceiling:
        return "amber"
    return "red"


def _pct(part, whole):
    """part/whole as a 1dp percentage; None when there is no denominator."""
    if not whole:
        return None
    return round(part / whole * 100.0, 1)


# ---- live-feed event-type -> typed tag --------------------------------------
#
# The five/seven fixed feed-tag colours from the design tokens. Explicit hexes
# are allowed for feed tags (same convention as the 3PL live feed) — they read
# identically in light and dark, and map an ``edi.log.event_type`` (plus the
# row's status, so an errored ACK reads EXCEPTION not ACK) to a label + colour.
_FEED_TAGS = {
    "file_download": {"label": "POLL", "color": "#fd7e14"},
    "file_parse": {"label": "PARSE", "color": "#0d6efd"},
    "order_created": {"label": "ORDER", "color": "#198754"},
    "ack_sent": {"label": "ACK", "color": "#714B67"},
    "duplicate_skipped": {"label": "DUP", "color": "#6c757d"},
    "review_approved": {"label": "REVIEW", "color": "#198754"},
    "review_rejected": {"label": "REVIEW", "color": "#dc3545"},
    "po_change_applied": {"label": "CHANGE", "color": "#0dcaf0"},
    "error": {"label": "EXCEPTION", "color": "#dc3545"},
    "ftp_connection": {"label": "POLL", "color": "#fd7e14"},
    "info": {"label": "INFO", "color": "#6c757d"},
}
_EXCEPTION_TAG = {"label": "EXCEPTION", "color": "#dc3545"}

# Human labels for the partner health_state selection (edi.trading.partner).
_HEALTH_LABELS = {
    "ok": "Healthy",
    "review": "Awaiting review",
    "error": "Errors (24h)",
    "circuit_open": "Circuit open",
    "never": "Never polled",
}
# RAG dot colour per health_state (fixed hexes, both themes).
_HEALTH_DOTS = {
    "ok": "#198754",
    "review": "#ffc107",
    "error": "#dc3545",
    "circuit_open": "#dc3545",
    "never": "#adb5bd",
}
_HEALTH_TONES = {
    "ok": "green",
    "review": "amber",
    "error": "red",
    "circuit_open": "red",
    "never": "none",
}


class EdiDashboard(models.AbstractModel):
    """KPI/triage/feed computation service for the EDI OWL dashboard.

    AbstractModel (no table). All methods are ``@api.model``, called from the
    frontend via ``this.orm.call('edi.dashboard', 'get_dashboard_summary', [])``.
    """

    _name = "edi.dashboard"
    _description = "EDI Operations Dashboard"
    # AbstractModel creates no table, but Odoo still derives + validates the
    # _table name; Postgres identifiers can't start with a digit and the
    # default here is fine ('edi_dashboard'), pinned explicitly for parity with
    # the other edi.* models.
    _table = "edi_dashboard"

    # ---- KPI targets (ir.config_parameter, with sane defaults) --------------

    @api.model
    def get_kpi_targets(self):
        """Configured KPI targets, read from the ``mml_edi.*`` config namespace.

        Defaults match the prototype copy: auto-approval >= 75%, ORDRSP on-time
        100%, review turnaround < 4h, exception rate a soft 8% ceiling.
        """
        ICP = self.env["ir.config_parameter"].sudo()

        def _f(key, default):
            return float(ICP.get_param("mml_edi." + key) or default)

        return {
            "auto_approval_target": _f("kpi_auto_approval_target", "75"),
            "auto_approval_amber": _f("kpi_auto_approval_amber", "60"),
            "ordrsp_on_time_target": _f("kpi_ordrsp_on_time_target", "100"),
            "ordrsp_on_time_amber": _f("kpi_ordrsp_on_time_amber", "95"),
            "review_turnaround_target_h": _f("kpi_review_turnaround_target_h", "4"),
            "review_turnaround_amber_h": _f("kpi_review_turnaround_amber_h", "8"),
            "exception_rate_target": _f("kpi_exception_rate_target", "8"),
            "exception_rate_amber": _f("kpi_exception_rate_amber", "15"),
        }

    # ---- partner scope helpers ----------------------------------------------

    @api.model
    def _partner_for_code(self, partner_code):
        """Resolve a segmented-filter code to a trading-partner recordset.

        ``None`` / '' / 'all' -> every active partner (empty domain fragment);
        a specific code -> that one partner (empty recordset if it's gone, which
        naturally yields zero rows rather than raising).
        """
        if not partner_code or partner_code == "all":
            return self.env["edi.trading.partner"].search([])
        return self.env["edi.trading.partner"].search([("code", "=", partner_code)])

    @api.model
    def _partner_domain(self, partner_code, field="trading_partner_id"):
        """Domain fragment scoping a model to one partner, or [] for all."""
        if not partner_code or partner_code == "all":
            return []
        partner = self._partner_for_code(partner_code)
        # A stale/unknown code scopes to no partner (id -1) rather than all, so
        # the board honestly shows nothing instead of silently widening.
        return [(field, "=", partner.id if partner else -1)]

    # ---- main entry point ----------------------------------------------------

    @api.model
    def get_dashboard_summary(self, partner_code=None):
        """Return the full EDI dashboard payload, optionally scoped to a partner.

        ``partner_code`` (a ``edi.trading.partner.code``, or ``None``/'all')
        narrows every KPI, triage row, pipeline stage, feed row and partner-rail
        entry to that trading partner. The no-arg call is unchanged (all
        partners).

        Payload keys:
          - ``data_available`` — False on fresh install (prevents all-green
            confusion when nothing has been polled yet)
          - ``partners`` — the segmented-filter options (code + name)
          - ``attention_items`` — actionable triage rows (blocking reviews,
            failed ORDRSP, unknown store, stock shortfall) with per-row actions
          - ``pipeline`` / ``today`` / ``pending_review`` — inbound ramp
          - ``kpis`` — the four KPI cards (value + rag + sub)
          - ``live_feed`` — recent typed exchange events
          - ``partner_health`` — the partner-health rail
          - ``feed`` — poll-freshness dot (``last_push`` / ``last_push_fresh``)
          - ``auto_line`` / ``pending_banner`` — honesty-footer copy
          - ``targets`` — the configured KPI targets
        """
        now = fields.Datetime.now()
        today_start = self._local_today_start(now)
        window_start = now - timedelta(days=30)
        targets = self.get_kpi_targets()

        feed_meta = self._feed_freshness(now, partner_code)
        return {
            "data_available": self._data_available(partner_code),
            "partners": self._partner_options(),
            "attention_items": self._attention_items(now, window_start, partner_code),
            "pipeline": self._pipeline(today_start, partner_code),
            "today": self._today_totals(today_start, partner_code),
            "pending_review": self._pending_review_count(partner_code),
            "kpis": self._kpis(now, window_start, targets, partner_code),
            "live_feed": self._live_feed(now, partner_code),
            "partner_health": self._partner_health(now, partner_code),
            "feed": feed_meta,
            "auto_line": self._auto_line(today_start, partner_code),
            "pending_banner": self._pending_banner(now, partner_code),
            "targets": targets,
        }

    # ---- fresh-install guard -------------------------------------------------

    @api.model
    def _data_available(self, partner_code=None):
        """True once any inbound EDI activity has been logged.

        Guards against the fresh-install all-green illusion: with no partner
        polled the frontend shows the Empty state, not a falsely healthy board.
        """
        return bool(self.env["edi.log"].search_count(
            self._partner_domain(partner_code) + [("direction", "=", "inbound")]
        ))

    @api.model
    def _partner_options(self):
        """Segmented-filter options: 'All partners' + one per active partner."""
        partners = self.env["edi.trading.partner"].search([], order="name")
        return [{"code": "all", "name": "All partners"}] + [
            {"code": p.code, "name": p.name} for p in partners
        ]

    # ---- local-day boundary --------------------------------------------------

    @api.model
    def _local_today_start(self, now):
        """Local (NZ) midnight as a naive-UTC datetime, so 'today' tracks the
        operator's calendar rather than rolling over at local noon."""
        import pytz
        tz = pytz.timezone(self.env.user.tz or "Pacific/Auckland")
        local = pytz.utc.localize(now).astimezone(tz)
        naive_midnight = datetime(local.year, local.month, local.day)
        return tz.localize(naive_midnight).astimezone(pytz.utc).replace(tzinfo=None)

    # ---- inbound pipeline ----------------------------------------------------

    @api.model
    def _pipeline(self, today_start, partner_code=None):
        """The five inbound-pipeline stages with today's counts + a note each.

        Received (files polled) -> Parsed (files parsed) -> Orders created
        (SOs) -> In review (currently pending) -> Acknowledged (ORDRSP sent).
        'In review' is the live pending count (not a today-scoped event) because
        it is a standing queue depth, matching the prototype. Every count is
        zero-filled so the ramp always renders all five boxes.
        """
        Log = self.env["edi.log"]
        pdom = self._partner_domain(partner_code)

        def _count(event_type, status=None, since=today_start):
            dom = pdom + [("event_type", "=", event_type), ("timestamp", ">=", since)]
            if status:
                dom.append(("status", "=", status))
            return Log.search_count(dom)

        received = _count("file_download")
        parsed = _count("file_parse")
        orders_created = _count("order_created")
        in_review = self._pending_review_count(partner_code)
        acknowledged = _count("ack_sent", status="success")
        # All error events today (parse / ftp / ack) — not scoped to parsing, so
        # the note reads "error(s) today", never the false "parse error(s)".
        errors_today = _count("error")
        return {
            "received": received,
            "parsed": parsed,
            "orders_created": orders_created,
            "in_review": in_review,
            "acknowledged": acknowledged,
            "notes": {
                "received": "files polled today",
                "parsed": "no errors today" if not errors_today else "%d error(s) today" % errors_today,
                "orders_created": "SOs created today",
                "in_review": "awaiting a decision",
                "acknowledged": "ORDRSP + CONTRL",
            },
        }

    @api.model
    def _today_totals(self, today_start, partner_code=None):
        """The pipeline header line: files / POs / store-orders / ORDRSP today."""
        Log = self.env["edi.log"]
        Review = self.env["edi.order.review"]
        pdom = self._partner_domain(partner_code)
        files = Log.search_count(
            pdom + [("event_type", "=", "file_download"), ("timestamp", ">=", today_start)])
        # Distinct POs seen today (reviews received today), a truer "POs" count
        # than a raw parse-event tally (one file can carry several POs).
        reviews_today = Review.search(
            self._partner_domain(partner_code) + [("received_date", ">=", today_start)])
        pos = len(set(reviews_today.mapped("customer_po_number")))
        store_orders = len(reviews_today)
        acks = Log.search_count(
            pdom + [("event_type", "=", "ack_sent"), ("status", "=", "success"),
                    ("timestamp", ">=", today_start)])
        return {"files": files, "pos": pos, "store_orders": store_orders, "acks": acks}

    @api.model
    def _pending_review_count(self, partner_code=None):
        return self.env["edi.order.review"].search_count(
            self._partner_domain(partner_code) + [("state", "=", "pending_review")])

    # ---- KPI cards -----------------------------------------------------------

    @api.model
    def _kpis(self, now, window_start, targets, partner_code=None):
        """The four KPI cards over a rolling 30-day window (partner-scoped)."""
        Review = self.env["edi.order.review"]
        Log = self.env["edi.log"]
        pdom = self._partner_domain(partner_code)

        resolved_dom = pdom + [
            ("state", "in", ("approved", "rejected", "auto_approved")),
            ("received_date", ">=", window_start),
        ]
        resolved = Review.search(resolved_dom)
        total_resolved = len(resolved)
        auto = len(resolved.filtered(lambda r: r.state == "auto_approved"))
        # Exception = an order that needed a human decision (approved/rejected
        # after review), i.e. did NOT auto-approve.
        manual = total_resolved - auto

        auto_val = _pct(auto, total_resolved)
        exc_val = _pct(manual, total_resolved)

        # ORDRSP on-time: successful ACK sends vs all ACK attempts (30d). A
        # failed upload (SFTP timeout to the VAN) drags this off 100%.
        ack_ok = Log.search_count(
            pdom + [("event_type", "=", "ack_sent"), ("status", "=", "success"),
                    ("timestamp", ">=", window_start)])
        ack_fail = Log.search_count(
            pdom + [("event_type", "=", "ack_sent"), ("status", "=", "error"),
                    ("timestamp", ">=", window_start)])
        ack_val = _pct(ack_ok, ack_ok + ack_fail)

        turnaround = self._median_turnaround_h(pdom, window_start)

        return {
            "auto_approval": {
                "value": auto_val,
                "rag": _rag_higher_better(auto_val, targets["auto_approval_target"],
                                          targets["auto_approval_amber"]),
                "label": "Auto-approval rate",
                "window": "30d",
                "sub": "clean orders confirmed with no human · target ≥%d"
                       % int(targets["auto_approval_target"]),
            },
            "ordrsp_on_time": {
                "value": ack_val,
                "rag": _rag_higher_better(ack_val, targets["ordrsp_on_time_target"],
                                          targets["ordrsp_on_time_amber"]),
                "label": "ORDRSP on-time",
                "window": "30d",
                "sub": "per-PO ORDRSP uploaded successfully · target %d"
                       % int(targets["ordrsp_on_time_target"]),
            },
            "review_turnaround": {
                "value": turnaround,
                "rag": _rag_lower_better(turnaround, targets["review_turnaround_target_h"],
                                         targets["review_turnaround_amber_h"]),
                "label": "Review turnaround",
                "window": "median",
                "unit": "h",
                "sub": "time pending → resolved · target <%dh"
                       % int(targets["review_turnaround_target_h"]),
            },
            "exception_rate": {
                "value": exc_val,
                "rag": _rag_lower_better(exc_val, targets["exception_rate_target"],
                                         targets["exception_rate_amber"]),
                "label": "Exception rate",
                "window": "30d",
                "sub": "orders needing a decision · price + unknown SKU",
            },
        }

    @api.model
    def _median_turnaround_h(self, pdom, window_start):
        """Median hours from received -> resolved over reviewed orders (30d).

        Only human-reviewed orders count: the domain requires a real
        ``reviewed_date``, and auto-approval never sets one (edi.processor
        writes only ``state='auto_approved'``). Auto-approved orders are
        therefore excluded — the KPI stays an honest measure of the manual
        review turnaround, not diluted to ~0h by the clean auto-flow. None
        when nothing resolved.
        """
        reviews = self.env["edi.order.review"].search(
            pdom + [("reviewed_date", "!=", False),
                    ("received_date", ">=", window_start)])
        deltas = [
            (r.reviewed_date - r.received_date).total_seconds() / 3600.0
            for r in reviews if r.reviewed_date and r.received_date
            and r.reviewed_date >= r.received_date
        ]
        if not deltas:
            return None
        return round(statistics.median(deltas), 1)

    # ---- needs-attention triage ---------------------------------------------

    @api.model
    def _attention_items(self, now, window_start, partner_code=None):
        """Actionable triage rows, red-before-amber, oldest first within a tier.

        Four honest sources, all from the EDI models:
          * blocking reviews (red) — pending_review with a blocking issue
          * failed ORDRSP (red) — a review whose per-PO ACK upload errored
          * unknown store (amber) — pending_review carrying an unknown_store issue
          * stock shortfall / warnings (amber) — pending_review with only warnings
        A review is only ever emitted once, at its highest-severity source.

        The failed-ORDRSP source is bounded to the same rolling 30d window the
        KPIs use and resolves ACK status in ONE batched ``edi.log`` pass — the
        deterministic per-PO ACK filenames of the whole candidate set, searched
        once with ``filename in ...`` and grouped in Python — instead of touching
        the per-record ``ack_status`` compute (which fires one ``edi.log`` search
        per review and, unbounded, grows without limit as ORDRSP history piles
        up, re-run on every 5-min refresh and partner-pill click).
        """
        Review = self.env["edi.order.review"]
        Log = self.env["edi.log"]
        pdom = self._partner_domain(partner_code)
        seen = set()
        items = []

        # 1. Failed ORDRSP (red) — resolved reviews (last 30d) whose per-PO ACK
        #    upload only ever errored (a log exists for its ACK filename, none
        #    succeeded — mirrors edi.order.review._compute_ack_status).
        resolved = Review.search(
            pdom + [
                ("state", "in", ("approved", "rejected", "auto_approved")),
                "|",
                ("reviewed_date", ">=", window_start),
                "&", ("reviewed_date", "=", False),
                ("received_date", ">=", window_start),
            ],
            order="reviewed_date desc")
        if resolved:
            fname_of = {
                rec.id: "ACK_%s_%s_%s.edi" % (
                    rec.trading_partner_id.code, rec.customer_po_number,
                    (rec.edi_file_hash or str(rec.id))[:8])
                for rec in resolved
            }
            ack_logs = Log.search([
                ("event_type", "=", "ack_sent"),
                ("filename", "in", list(set(fname_of.values()))),
            ])
            # filename -> {"any": at least one ACK log, "success": one succeeded}
            by_file = {}
            for log in ack_logs:
                slot = by_file.setdefault(log.filename, {"any": False, "success": False})
                slot["any"] = True
                if log.status == "success":
                    slot["success"] = True
            # Dedupe by filename: all store-review siblings of one inbound file
            # share edi_file_hash -> identical ACK filename -> one exchange, one
            # row. ``resolved`` is reviewed_date-desc, so the newest sibling wins.
            emitted = set()
            for rec in resolved:
                fname = fname_of[rec.id]
                slot = by_file.get(fname)
                if not (slot and slot["any"] and not slot["success"]):
                    continue
                if fname in emitted:
                    seen.add(rec.id)
                    continue
                items.append(self._item_ack_failed(now, rec))
                seen.add(rec.id)
                emitted.add(fname)

        # 2. Blocking reviews (red).
        blocking = Review.search(
            pdom + [("state", "=", "pending_review"), ("blocking_issue_count", ">", 0)],
            order="received_date asc")
        for rec in blocking:
            if rec.id in seen:
                continue
            items.append(self._item_blocking(now, rec))
            seen.add(rec.id)

        # 3. Warnings only (amber) — unknown store / stock shortfall etc.
        warnings = Review.search(
            pdom + [("state", "=", "pending_review"), ("blocking_issue_count", "=", 0),
                    ("warning_count", ">", 0)],
            order="received_date asc")
        for rec in warnings:
            if rec.id in seen:
                continue
            items.append(self._item_warning(now, rec))
            seen.add(rec.id)

        return items

    @api.model
    def _age_hours(self, now, ts):
        if not ts:
            return 0.0
        return round((now - ts).total_seconds() / 3600.0, 1)

    @api.model
    def _partner_tag(self, rec):
        code = rec.trading_partner_id.code or ""
        return code.upper()

    @api.model
    def _review_title(self, rec, suffix=""):
        po = rec.customer_po_number or rec.name or ""
        store = (" · store %s" % rec.store_code) if rec.store_code else ""
        return "PO %s%s%s" % (po, store, suffix)

    @api.model
    def _issue_summary(self, rec):
        """First blocking issue's description, else first issue's, else ''."""
        issues = rec.issue_ids
        blocking = issues.filtered(lambda i: i.severity == "blocking")
        target = blocking[:1] or issues[:1]
        if not target:
            return ""
        return (target.description or "").splitlines()[0][:200] if target.description else ""

    @api.model
    def _item_blocking(self, now, rec):
        return {
            "id": rec.id,
            "kind": "blocking",
            "severity": "red",
            "ref": rec.name or "",
            "res_model": "edi.order.review",
            "res_id": rec.id,
            "partner_tag": self._partner_tag(rec),
            "title": self._review_title(rec),
            "summary": (self._issue_summary(rec)
                        or "Blocking issue — ORDRSP held for the whole PO"),
            "age_hours": self._age_hours(now, rec.received_date),
            "actions": ["review"],
        }

    @api.model
    def _item_ack_failed(self, now, rec):
        return {
            "id": rec.id,
            "kind": "ack_failed",
            "severity": "red",
            "ref": rec.name or "",
            "res_model": "edi.order.review",
            "res_id": rec.id,
            "partner_tag": self._partner_tag(rec),
            "title": "ORDRSP for PO %s — upload failed" % (rec.customer_po_number or ""),
            "summary": "ORDRSP upload to the EDIS VAN failed · Retry ACK re-queues it",
            "age_hours": self._age_hours(now, rec.reviewed_date or rec.received_date),
            "actions": ["retry_ack", "log"],
        }

    @api.model
    def _item_warning(self, now, rec):
        unknown_store = rec.issue_ids.filtered(
            lambda i: i.issue_type == "unknown_store")
        if unknown_store:
            title = self._review_title(rec, " — unknown store code")
            summary = (self._issue_summary(rec)
                       or "Store not in the partner store map · seed then re-split")
            action = "map_store"
        else:
            title = self._review_title(rec)
            summary = self._issue_summary(rec) or "Warning at approve · acknowledge in ORDRSP"
            action = "review"
        return {
            "id": rec.id,
            "kind": "warning",
            "severity": "amber",
            "ref": rec.name or "",
            "res_model": "edi.order.review",
            "res_id": rec.id,
            "partner_tag": self._partner_tag(rec),
            "title": title,
            "summary": summary,
            "age_hours": self._age_hours(now, rec.received_date),
            "actions": [action],
        }

    # ---- live feed -----------------------------------------------------------

    @api.model
    def _live_feed(self, now, partner_code=None, limit=10):
        """Recent typed exchange events (newest first), capped for the card."""
        logs = self.env["edi.log"].search(
            self._partner_domain(partner_code), order="timestamp desc", limit=limit)
        rows = []
        for log in logs:
            tag = _FEED_TAGS.get(log.event_type, _FEED_TAGS["info"])
            if log.status == "error" and log.event_type != "error":
                tag = _EXCEPTION_TAG
            rows.append({
                "id": log.id,
                "time": self._fmt_time(now, log.timestamp),
                "tag": tag["label"],
                "color": tag["color"],
                "text": (log.message or "").splitlines()[0][:160] if log.message else "",
                "res_model": "edi.order.review" if log.review_id else (
                    "sale.order" if log.sale_order_id else None),
                "res_id": (log.review_id.id if log.review_id else (
                    log.sale_order_id.id if log.sale_order_id else None)),
            })
        return rows

    @api.model
    def _fmt_time(self, now, ts):
        if not ts:
            return "--:--"
        import pytz
        tz = pytz.timezone(self.env.user.tz or "Pacific/Auckland")
        local = pytz.utc.localize(ts).astimezone(tz)
        age_h = (now - ts).total_seconds() / 3600.0
        return local.strftime("%H:%M" if age_h < 24 else "%a %H:%M")

    # ---- partner-health rail -------------------------------------------------

    @api.model
    def _partner_health(self, now, partner_code=None):
        """Per-partner health rail (dot + label + one-line detail)."""
        partners = self._partner_for_code(partner_code).sorted("name")
        rows = []
        for p in partners:
            health = p.health_state or "never"
            last = self._fmt_time(now, p.last_poll_date) if p.last_poll_date else "never"
            circuit = "circuit open" if p.circuit_open_since else "circuit closed (%d/%d)" % (
                p.circuit_failure_count, p.circuit_failure_threshold)
            detail = "Last poll %s · %s · %d in review" % (
                last, circuit, p.pending_review_count)
            rows.append({
                "id": p.id,
                "code": p.code,
                "name": p.name,
                "fmt": dict(p._fields["edi_format"].selection).get(p.edi_format, p.edi_format or ""),
                "dot": _HEALTH_DOTS.get(health, "#adb5bd"),
                "tone": _HEALTH_TONES.get(health, "none"),
                "health": _HEALTH_LABELS.get(health, health),
                "detail": detail,
            })
        return rows

    # ---- poll-freshness dot --------------------------------------------------

    @api.model
    def _feed_freshness(self, now, partner_code=None):
        """Newest inbound poll timestamp + a fresh flag (< 2h) for the live dot."""
        last = self.env["edi.log"].search(
            self._partner_domain(partner_code) + [("direction", "=", "inbound")],
            order="timestamp desc", limit=1)
        if not last or not last.timestamp:
            return {"last_push": None, "last_push_fresh": False}
        age_h = (now - last.timestamp).total_seconds() / 3600.0
        return {
            "last_push": self._fmt_time(now, last.timestamp),
            "last_push_fresh": age_h < 2,
        }

    # ---- honesty-footer copy -------------------------------------------------

    @api.model
    def _auto_line(self, today_start, partner_code=None):
        """'N orders auto-approved clean today · acknowledged same poll'."""
        auto = self.env["edi.order.review"].search_count(
            self._partner_domain(partner_code) + [
                ("state", "=", "auto_approved"),
                ("received_date", ">=", today_start),
            ])
        return ("%d orders auto-approved clean today · acknowledged same poll · "
                "no action needed" % auto)

    @api.model
    def _pending_banner(self, now, partner_code=None):
        """'N orders pending review — oldest Xh · open the queue', or '' if none."""
        Review = self.env["edi.order.review"]
        pdom = self._partner_domain(partner_code) + [("state", "=", "pending_review")]
        count = Review.search_count(pdom)
        if not count:
            return ""
        oldest = Review.search(pdom, order="received_date asc", limit=1)
        age_h = self._age_hours(now, oldest.received_date) if oldest else 0.0
        if age_h >= 48:
            age = "%dd" % round(age_h / 24)
        elif age_h >= 1:
            age = "%dh" % round(age_h)
        else:
            age = "%dm" % round(age_h * 60)
        return "%d orders pending review — oldest %s · open the queue" % (count, age)

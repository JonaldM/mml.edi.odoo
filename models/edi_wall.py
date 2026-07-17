# mml_edi/models/edi_wall.py
"""EDI operations wall-display computation service.

A 1920x1080 dark ops radiator for a TV browser session (README EDI screen 7,
``Wall Display.dc.html``). The prototype is recreated as a read-only OWL client
action (``edi_wall``) that auto-refreshes on the module's 5-minute cadence — no
interaction.

AbstractModel — no DB table. The whole board is one batched ``@api.model`` RPC,
``edi.wall.get_wall_summary(partner_code)``, composed largely from the
``edi.dashboard`` helpers (pipeline, today totals, KPI window, live feed,
partner health, attention triage) so the wall and the landing dashboard never
drift. The wall adds only its own shaping: the four alarm tiles (calm/amber/red
by count), the three service-level bars with a target marker, the partner
strip, and the poll-freshness reading.

Honesty: the wall reports only what the EDI models record. The alarm counts and
service levels come straight from ``edi.order.review`` / ``edi.log`` — nothing
is fabricated. The FIXED dark radiator palette (README shared-theme "Wall
Display" row) is applied in the frontend in BOTH Odoo colour schemes, so the
board reads the same on a TV regardless of the operator's theme.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

from ..services.wall_format import alarm_count_tone, poll_tone, service_bar
from .edi_dashboard import _HEALTH_DOTS, _pct

_logger = logging.getLogger(__name__)


class EdiWall(models.AbstractModel):
    """Wall-display computation service for the EDI OWL radiator.

    AbstractModel (no table). Every method is ``@api.model``, called from the
    frontend via ``this.orm.call('edi.wall', 'get_wall_summary', [], {...})``.
    """

    _name = "edi.wall"
    _description = "EDI Operations Wall Display"
    _table = "edi_wall"

    # ---- main entry point ----------------------------------------------------

    @api.model
    def get_wall_summary(self, partner_code=None):
        """Return the full wall-display payload, optionally scoped to a partner.

        Payload keys:
          - ``data_available`` — False before any inbound activity (the wall
            then shows a "waiting for the first poll" state, not a false all-green
            radiator).
          - ``alarms`` — the four alarm tiles (blocking reviews, failed ORDRSP,
            unknown stores, poll freshness) with a ``tone`` driving the glow.
          - ``pipeline`` / ``today`` — the inbound ramp + its header line.
          - ``service_levels`` — the three service-level bars (auto-approval,
            ORDRSP on-time, clean-parse) with value / target / marker / tone.
          - ``partners`` — the partner strip (dot + format + in-review count).
          - ``feed`` — the recent typed exchange events.
          - ``poll`` — the poll-freshness dot (``last_push`` / ``last_push_fresh``).
        """
        dash = self.env["edi.dashboard"]
        now = fields.Datetime.now()
        today_start = dash._local_today_start(now)
        window_start = now - timedelta(days=30)
        targets = dash.get_kpi_targets()

        # One batched attention pass feeds every alarm count (no per-alarm search).
        attention = dash._attention_items(now, window_start, partner_code)
        kpis = dash._kpis(now, window_start, targets, partner_code)

        return {
            "data_available": dash._data_available(partner_code),
            "alarms": self._alarms(now, attention, partner_code),
            "pipeline": dash._pipeline(today_start, partner_code),
            "today": dash._today_totals(today_start, partner_code),
            "service_levels": self._service_levels(kpis, targets, window_start, partner_code),
            "partners": self._partner_strip(partner_code),
            "feed": dash._live_feed(now, partner_code, limit=10),
            "poll": dash._feed_freshness(now, partner_code),
        }

    # ---- alarm tiles ---------------------------------------------------------

    @api.model
    def _alarms(self, now, attention, partner_code=None):
        """The four wall alarm tiles, derived from the batched attention pass +
        one poll-freshness read.

        ``attention`` (from ``edi.dashboard._attention_items``) already groups the
        actionable triage rows, so the blocking / failed-ORDRSP / unknown-store
        counts come from it with no extra query.
        """
        blocking = sum(1 for i in attention if i["kind"] == "blocking")
        failed_ack = sum(1 for i in attention if i["kind"] == "ack_failed")
        unknown = sum(
            1 for i in attention
            if i["kind"] == "warning" and "map_store" in i.get("actions", [])
        )

        fresh_min = self._poll_freshness_minutes(now, partner_code)
        poll_value = "—" if fresh_min is None else str(fresh_min)
        poll_sub = "no poll yet" if fresh_min is None else "min since last poll"

        return [
            {"key": "blocking", "label": "Blocking reviews", "value": str(blocking),
             "sub": "need a decision", "tone": alarm_count_tone(blocking)},
            {"key": "failed_ack", "label": "Failed ORDRSP", "value": str(failed_ack),
             "sub": "upload failed · retry armed", "tone": alarm_count_tone(failed_ack)},
            {"key": "unknown", "label": "Unknown stores", "value": str(unknown),
             "sub": "unmapped store code", "tone": alarm_count_tone(unknown)},
            {"key": "poll", "label": "Poll freshness", "value": poll_value,
             "sub": poll_sub, "tone": poll_tone(fresh_min)},
        ]

    @api.model
    def _poll_freshness_minutes(self, now, partner_code=None):
        """Whole minutes since the newest inbound poll, or None if never polled."""
        dash = self.env["edi.dashboard"]
        last = self.env["edi.log"].search(
            dash._partner_domain(partner_code) + [("direction", "=", "inbound")],
            order="timestamp desc", limit=1)
        if not last or not last.timestamp:
            return None
        return int((now - last.timestamp).total_seconds() // 60)

    # ---- service-level bars --------------------------------------------------

    @api.model
    def _clean_parse_targets(self):
        """Clean-parse-rate target + amber floor (config-overridable defaults)."""
        ICP = self.env["ir.config_parameter"].sudo()

        def _f(key, default):
            return float(ICP.get_param("mml_edi." + key) or default)

        return (_f("kpi_clean_parse_target", "98"), _f("kpi_clean_parse_amber", "95"))

    @api.model
    def _clean_parse_rate(self, window_start, partner_code=None):
        """Successful file parses / all parse attempts over the 30d window.

        A parse error (a malformed inbound file that could not be read) drags
        this off 100% — an honest quality signal, not a fabricated one. None when
        nothing has parsed.
        """
        Log = self.env["edi.log"]
        pdom = self.env["edi.dashboard"]._partner_domain(partner_code)
        ok = Log.search_count(
            pdom + [("event_type", "=", "file_parse"), ("status", "=", "success"),
                    ("timestamp", ">=", window_start)])
        err = Log.search_count(
            pdom + [("event_type", "=", "file_parse"), ("status", "=", "error"),
                    ("timestamp", ">=", window_start)])
        return _pct(ok, ok + err)

    @api.model
    def _service_levels(self, kpis, targets, window_start, partner_code=None):
        """The three wall service-level bars (auto-approval, ORDRSP on-time,
        clean-parse). Auto-approval and ORDRSP on-time reuse the dashboard KPI
        window; clean-parse is computed here."""
        clean_target, clean_amber = self._clean_parse_targets()
        clean = self._clean_parse_rate(window_start, partner_code)
        return [
            service_bar("Auto-approval rate", kpis["auto_approval"]["value"],
                        targets["auto_approval_target"], targets["auto_approval_amber"]),
            service_bar("ORDRSP on-time", kpis["ordrsp_on_time"]["value"],
                        targets["ordrsp_on_time_target"], targets["ordrsp_on_time_amber"]),
            service_bar("Clean-parse rate", clean, clean_target, clean_amber),
        ]

    # ---- partner strip -------------------------------------------------------

    @api.model
    def _partner_strip(self, partner_code=None):
        """Partner strip: name + health dot + 'format · N in review' per partner."""
        partners = self.env["edi.dashboard"]._partner_for_code(partner_code).sorted("name")
        rows = []
        for p in partners:
            health = p.health_state or "never"
            fmt = dict(p._fields["edi_format"].selection).get(p.edi_format, p.edi_format or "")
            rows.append({
                "id": p.id,
                "name": p.name,
                "dot": _HEALTH_DOTS.get(health, "#adb5bd"),
                "sub": "%s · %d in review" % (fmt, p.pending_review_count),
            })
        return rows

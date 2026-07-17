/** @odoo-module **/
// mml_edi/static/src/js/edi_wall.js
//
// EDI operations WALL DISPLAY — a 1920x1080 dark ops radiator for a TV browser
// session. Recreates design_handoff "Wall Display.dc.html" as a production OWL
// 2 client action (tag `edi_wall`), continuing the 3PL / ROQ visual language.
// Read-only: no interaction, no filter — it just refreshes on the module's
// 5-minute cadence (README EDI screen 7). Layout:
//   - header: app mark + "MML EDI · order exchange · operations", live dot, clock
//   - 4 alarm tiles (blocking reviews / failed ORDRSP / unknown stores / poll
//     freshness) that GLOW when non-zero (calm green -> amber -> red by count)
//   - big inbound pipeline ramp (Received -> Parsed -> Orders -> In review ->
//     Acknowledged) with today's header line
//   - service-level bars (Auto-approval / ORDRSP on-time / Clean-parse) with a
//     target marker, + a partner strip
//   - live exchange feed ticker
//
// One batched RPC — edi.wall.get_wall_summary() — returns the whole board. The
// FIXED dark radiator palette is applied in BOTH Odoo colour schemes (the wall
// reads the same on a TV regardless of the operator's theme), so this screen
// uses its own hardcoded palette rather than the --edi-* token bridge.

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const REFRESH_MS = 5 * 60 * 1000; // keep the module's 5-min live-refresh cadence
const CLOCK_MS = 15 * 1000;       // wall clock tick

// Alarm tone -> tile colour / inner glow / border (fixed, both schemes).
const ALARM_TONES = {
    calm: { color: "#46A758", glow: "transparent", border: "#212B42" },
    amber: { color: "#F5A524", glow: "rgba(245,165,36,.14)", border: "#5A4620" },
    red: { color: "#E5484D", glow: "rgba(229,72,77,.16)", border: "#5A2327" },
};

// Inbound-pipeline ramp: the wall's dark-radiator stage palette (README
// "Pipeline / stage ramp" + the prototype's dark boxes). Position-ordered.
const STAGE_STYLES = [
    { bg: "#12305A", fg: "#BCD6F5" },   // Received
    { bg: "#1C4A86", fg: "#DCEAFB" },   // Parsed
    { bg: "#2E7CD6", fg: "#fff" },      // Orders created
    { bg: "#4A3512", fg: "#F5C97A" },   // In review (dwell amber)
    { bg: "#173A22", fg: "#7FD79A" },   // Acknowledged
];

// Service-level bar tone -> stroke colour.
const BAR_COLORS = { green: "#46A758", amber: "#F5A524", red: "#E5484D", none: "#5C6784" };

function nowClock() {
    const d = new Date();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    return hh + ":" + mm;
}

class EdiWall extends Component {
    static template = "mml_edi.EdiWall";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.state = useState({
            loading: true,
            error: null,
            summary: null,
            clock: nowClock(),
        });
        this._refreshInterval = null;
        this._clockInterval = null;

        onWillStart(async () => {
            await this._load();
        });
        onMounted(() => {
            this._refreshInterval = setInterval(() => this._load(), REFRESH_MS);
            this._clockInterval = setInterval(() => { this.state.clock = nowClock(); }, CLOCK_MS);
        });
        onWillUnmount(() => {
            if (this._refreshInterval) clearInterval(this._refreshInterval);
            if (this._clockInterval) clearInterval(this._clockInterval);
        });
    }

    async _load() {
        try {
            const summary = await this.orm.call("edi.wall", "get_wall_summary", []);
            Object.assign(this.state, { summary, loading: false, error: null });
        } catch (e) {
            Object.assign(this.state, { error: "Couldn't load the wall", loading: false });
        }
    }

    get dataAvailable() {
        return !!(this.state.summary && this.state.summary.data_available);
    }

    // ---- alarm tiles ---------------------------------------------------------

    alarmTiles() {
        const alarms = (this.state.summary && this.state.summary.alarms) || [];
        return alarms.map((a) => {
            const tone = ALARM_TONES[a.tone] || ALARM_TONES.calm;
            return {
                key: a.key,
                label: a.label,
                value: a.value,
                sub: a.sub,
                valueStyle: "color:" + tone.color,
                tileStyle:
                    "border:1px solid " + tone.border +
                    ";box-shadow:inset 0 0 40px " + tone.glow,
            };
        });
    }

    // ---- inbound pipeline ----------------------------------------------------

    get todayLine() {
        const t = (this.state.summary && this.state.summary.today) || {};
        return [
            (t.files || 0) + " files",
            (t.pos || 0) + " POs",
            (t.store_orders || 0) + " store-orders",
            (t.acks || 0) + " ORDRSP sent",
        ].join(" · ");
    }

    pipelineStages() {
        const p = (this.state.summary && this.state.summary.pipeline) || {};
        const notes = p.notes || {};
        const defs = [
            { key: "received", label: "Received", value: p.received },
            { key: "parsed", label: "Parsed", value: p.parsed },
            { key: "orders_created", label: "Orders created", value: p.orders_created },
            { key: "in_review", label: "In review", value: p.in_review },
            { key: "acknowledged", label: "Acknowledged", value: p.acknowledged },
        ];
        return defs.map((d, idx) => {
            const st = STAGE_STYLES[idx];
            return {
                key: d.key,
                label: d.label,
                value: typeof d.value === "number" ? d.value : 0,
                note: notes[d.key] || "",
                style: "background:" + st.bg + ";color:" + st.fg,
                arrow: idx < defs.length - 1,
            };
        });
    }

    // ---- service-level bars --------------------------------------------------

    serviceBars() {
        const bars = (this.state.summary && this.state.summary.service_levels) || [];
        return bars.map((b, idx) => {
            const color = BAR_COLORS[b.tone] || BAR_COLORS.none;
            return {
                key: "bar" + idx,
                label: b.label,
                value: b.value,
                target: b.target,
                valueStyle: "color:" + color,
                fillStyle: "width:" + b.pct + ";background:" + color,
                markStyle: "left:" + b.mark,
            };
        });
    }

    // ---- partner strip -------------------------------------------------------

    partnerStrip() {
        const partners = (this.state.summary && this.state.summary.partners) || [];
        return partners.map((p) => ({
            id: p.id,
            name: p.name,
            sub: p.sub,
            dotStyle: "background:" + p.dot,
        }));
    }

    // ---- live feed -----------------------------------------------------------

    feedRows() {
        const feed = (this.state.summary && this.state.summary.feed) || [];
        return feed.map((ev) => ({
            key: "f" + ev.id,
            time: ev.time,
            tag: ev.tag,
            tagStyle: "background:" + ev.color,
            text: ev.text,
        }));
    }

    // ---- poll dot ------------------------------------------------------------

    get pollFresh() {
        return !!(this.state.summary && this.state.summary.poll && this.state.summary.poll.last_push_fresh);
    }
}

registry.category("actions").add("edi_wall", EdiWall);

export { EdiWall };

/** @odoo-module **/
// mml_edi/static/src/js/edi_dashboard.js
//
// EDI operations dashboard (landing). Recreates design_handoff "EDI
// Dashboard.dc.html" as a production OWL 2 client action, continuing the 3PL /
// ROQ visual language. Layout (README EDI screen 1):
//   - control bar: title, partner segmented filter, order search, live-poll
//     indicator, Run poll now
//   - body grid 1fr / 322px:
//       left  -> Needs-attention triage (or All-clear banner), Inbound pipeline
//                ramp, 4 KPI cards (RAG left border + hand-rolled sparkline)
//       right -> Live feed, Trading-partner health rail
//
// One batched RPC — edi.dashboard.get_dashboard_summary(partner_code) — returns
// the whole board; the partner segmented control recomputes everything by
// re-calling it scoped. The prototype's localStorage dark-mode transport is
// dropped: Odoo's native colour scheme drives light/dark (edi_shared.dark.scss).

import { registry } from "@web/core/registry";
import { Component, useState, useRef, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { formatAge, formatPct, formatHours, formatCount } from "@mml_edi/utils/edi_format";
import { usePartnerFilter, EdiPartnerFilter } from "@mml_edi/utils/use_partner_filter";

const STORAGE_KEY = "edi_dashboard_partner";
const SEARCH_DEBOUNCE_MS = 300;
const REFRESH_MS = 5 * 60 * 1000; // keep the module's 5-min live-refresh cadence

// Inbound-pipeline ramp: blue by position (early -> dispatched) with the "in
// review" stage overridden to the dwell-amber pair, and the terminal
// "acknowledged" stage a dashed surface box. Explicit fg/bg pairs from the
// README pipeline-ramp tokens (light-bg + dark-text read in both schemes).
const STAGE_STYLES = [
    { bg: "#E6F1FB", fg: "#0C447C" },   // Received
    { bg: "#85B7EB", fg: "#042C53" },   // Parsed
    { bg: "#378ADD", fg: "#fff" },      // Orders created
    { bg: "#FAEEDA", fg: "#854F0B" },   // In review (dwell amber)
    { bg: "var(--edi-surface)", fg: "var(--edi-muted)", dashed: true }, // Acknowledged
];

// Triage-action key -> button label. `primary` (red row, first action) gets the
// accent outline; the rest are neutral.
const TRIAGE_ACTIONS = {
    review: "Review",
    retry_ack: "Retry ACK",
    log: "Log",
    map_store: "Map store",
};

class EdiDashboard extends Component {
    static components = { EdiPartnerFilter };
    static template = "mml_edi.EdiDashboard";

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.notification = useService("notification");
        this.searchInputRef = useRef("searchInput");
        this.partnerFilter = usePartnerFilter(STORAGE_KEY);

        this.state = useState({
            loading: true,
            refreshing: false,
            error: null,
            summary: null,
            query: "",
            results: [],
            noResults: false,
        });

        this._refreshInterval = null;
        this._searchTimer = null;
        this._searchSeq = 0;

        onWillStart(async () => {
            await this._load();
        });
        onMounted(() => {
            this._refreshInterval = setInterval(() => this._load(), REFRESH_MS);
        });
        onWillUnmount(() => {
            if (this._refreshInterval) clearInterval(this._refreshInterval);
            if (this._searchTimer) clearTimeout(this._searchTimer);
        });
    }

    // ---- data loading --------------------------------------------------------

    get partnerParam() {
        return this.partnerFilter.partnerParam;
    }

    async _load() {
        try {
            const summary = await this.orm.call(
                "edi.dashboard", "get_dashboard_summary", [],
                { partner_code: this.partnerParam });
            this.partnerFilter.reconcilePartners(summary.partners);
            Object.assign(this.state, { summary, loading: false, error: null });
        } catch (e) {
            Object.assign(this.state, {
                error: "Couldn't load the dashboard",
                loading: false,
            });
        }
    }

    async onPartnerChange() {
        // A filter change is an in-place refresh: keep the loaded board (and the
        // control bar under the user's cursor) mounted, dim it while the scoped
        // payload arrives. The full skeleton is reserved for the initial mount.
        this.state.refreshing = true;
        try {
            await this._load();
        } finally {
            this.state.refreshing = false;
        }
    }

    async onRetry() {
        this.state.loading = true;
        this.state.error = null;
        await this._load();
    }

    // ---- order search (debounced, client-side) -------------------------------

    onSearch(ev) {
        const value = ev.target.value;
        this.state.query = value;
        if (this._searchTimer) clearTimeout(this._searchTimer);
        const q = value.trim();
        if (!q) {
            this.state.results = [];
            this.state.noResults = false;
            return;
        }
        this._searchTimer = setTimeout(() => this._runSearch(q), SEARCH_DEBOUNCE_MS);
    }

    async _runSearch(q) {
        const seq = ++this._searchSeq;
        const partnerDomain = this.partnerParam
            ? [["trading_partner_id.code", "=", this.partnerParam]]
            : [];
        try {
            const reviews = await this.orm.searchRead(
                "edi.order.review",
                partnerDomain.concat([
                    "|", "|", "|",
                    ["customer_po_number", "ilike", q],
                    ["store_code", "ilike", q],
                    ["name", "ilike", q],
                    ["sale_order_id.name", "ilike", q],
                ]),
                ["name", "customer_po_number", "store_code", "state", "trading_partner_id"],
                { limit: 5 });
            if (seq !== this._searchSeq) return; // a newer keystroke already fired
            const rows = reviews.map((r) => this._reviewResult(r));
            this.state.results = rows;
            this.state.noResults = rows.length === 0;
        } catch (e) {
            if (seq !== this._searchSeq) return;
            this.state.results = [];
            this.state.noResults = true;
        }
    }

    _reviewResult(r) {
        const chip = this._stateChip(r.state);
        const store = r.store_code ? " · store " + r.store_code : "";
        return {
            key: "r" + r.id,
            id: r.id,
            ref: "PO " + (r.customer_po_number || r.name) + store,
            customer: r.trading_partner_id ? r.trading_partner_id[1] : "",
            sub: r.name,
            status: chip.label,
            chipCls: chip.cls,
        };
    }

    _stateChip(state) {
        return {
            pending_review: { label: "Pending", cls: "edi-chip-amber" },
            approved: { label: "Approved", cls: "edi-chip-green" },
            auto_approved: { label: "Auto-approved", cls: "edi-chip-blue" },
            rejected: { label: "Rejected", cls: "edi-chip-red" },
        }[state] || { label: state, cls: "edi-chip-grey" };
    }

    openResult(row) {
        this.state.query = "";
        this.state.results = [];
        this.state.noResults = false;
        if (this.searchInputRef.el) this.searchInputRef.el.value = "";
        this._openRecord("edi.order.review", row.id);
    }

    // ---- needs-attention triage ---------------------------------------------

    get attentionItems() {
        return (this.state.summary && this.state.summary.attention_items) || [];
    }
    get redCount() {
        return this.attentionItems.filter((i) => i.severity === "red").length;
    }
    get amberCount() {
        return this.attentionItems.filter((i) => i.severity === "amber").length;
    }
    get allClear() {
        return this.attentionItems.length === 0;
    }

    triageRows() {
        return this.attentionItems.map((item) => {
            const red = item.severity === "red";
            const actions = (item.actions || []).map((key, idx) => ({
                key: item.id + "-" + key,
                actionKey: key,
                label: TRIAGE_ACTIONS[key] || key,
                btnClass: "btn btn-sm " + (red && idx === 0 ? "btn-outline-primary" : "btn-outline-secondary"),
            }));
            return {
                id: item.id,
                dotStyle: "background:" + (red ? "#dc3545" : "#ffc107"),
                title: item.title,
                partnerTag: item.partner_tag,
                sub: item.summary,
                age: formatAge(item.age_hours),
                ageClass: red ? "edi-pill edi-pill-red" : "edi-pill edi-pill-amber",
                actions,
                _item: item,
            };
        });
    }

    async onTriageAction(row, action) {
        const item = row._item;
        switch (action.actionKey) {
            case "retry_ack":
                // Re-queue every failed ORDRSP for fully-resolved POs via the
                // idempotent cron entrypoint, then refresh the board and confirm
                // — the button now genuinely retries rather than just opening a log.
                await this.orm.call("edi.processor", "retry_pending_acks", []);
                await this._load();
                this.notification.add("Pending ACKs re-queued", { type: "success" });
                break;
            case "log":
                this.actionService.doAction("mml_edi.action_edi_log");
                break;
            case "map_store":
                this.actionService.doAction("mml_edi.action_edi_trading_partner");
                break;
            default:
                this._openRecord(item.res_model, item.res_id);
        }
    }

    get autoLine() {
        return (this.state.summary && this.state.summary.auto_line) || "";
    }

    // ---- inbound pipeline ----------------------------------------------------

    get pipeline() {
        return (this.state.summary && this.state.summary.pipeline) || {};
    }
    get todayTotals() {
        return (this.state.summary && this.state.summary.today) || { files: 0, pos: 0, store_orders: 0, acks: 0 };
    }
    get pendingReview() {
        return (this.state.summary && this.state.summary.pending_review) || 0;
    }

    pipelineStages() {
        const p = this.pipeline;
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
            const base = "background:" + st.bg + ";color:" + st.fg;
            return {
                key: d.key,
                label: d.label,
                value: formatCount(d.value),
                note: notes[d.key] || "",
                style: base,
                dashed: !!st.dashed,
            };
        });
    }

    // ---- KPI cards -----------------------------------------------------------

    get kpis() {
        return (this.state.summary && this.state.summary.kpis) || {};
    }

    kpiCards() {
        const k = this.kpis;
        const order = ["auto_approval", "ordrsp_on_time", "review_turnaround", "exception_rate"];
        return order
            .filter((key) => k[key])
            .map((key) => {
                const c = k[key];
                const value = c.unit === "h" ? formatHours(c.value) : formatPct(c.value);
                return {
                    key,
                    label: c.label,
                    window: c.window,
                    value,
                    sub: c.sub,
                    ragClass: "edi-kpi-" + (c.rag || "none"),
                    spark: this._spark(c.rag),
                };
            });
    }

    // A static, RAG-coloured 72x24 sparkline. The EDI models carry no weekly
    // KPI trend series (unlike the 3PL on-time series), so — rather than
    // fabricate a trend — every card shows the SAME flat illustrative baseline,
    // coloured by the card's RAG. Honest: it is decorative, not a data claim,
    // matching the prototype's fixed polylines.
    _spark(rag) {
        const stroke = { green: "#198754", amber: "#ffc107", red: "#dc3545" }[rag] || "#adb5bd";
        return { points: "0,17 12,15 24,16 36,13 48,14 60,11 72,12", stroke };
    }

    // ---- live feed -----------------------------------------------------------

    liveFeed() {
        const feed = (this.state.summary && this.state.summary.live_feed) || [];
        return feed.map((ev) => ({
            key: "f" + ev.id,
            time: ev.time,
            tag: ev.tag,
            tagStyle: "color:" + ev.color,
            text: ev.text,
            model: ev.res_model || null,
            id: ev.res_id || null,
        }));
    }
    openFeedRow(row) {
        if (row.model && row.id) this._openRecord(row.model, row.id);
    }
    openAllEvents() {
        this.actionService.doAction("mml_edi.action_edi_log");
    }

    // ---- partner-health rail -------------------------------------------------

    get partnerHealth() {
        return (this.state.summary && this.state.summary.partner_health) || [];
    }
    partnerRows() {
        return this.partnerHealth.map((p) => ({
            id: p.id,
            name: p.name,
            fmt: p.fmt,
            dotStyle: "background:" + p.dot,
            healthStyle: "color:" + p.dot,
            health: p.health,
            detail: p.detail,
        }));
    }
    get pendingBanner() {
        return (this.state.summary && this.state.summary.pending_banner) || "";
    }
    openReviewQueue() {
        this.actionService.doAction("mml_edi.action_edi_order_review_pending");
    }
    openPartnerHealth() {
        this.actionService.doAction("mml_edi.action_edi_trading_partner");
    }

    // ---- header poll indicator ----------------------------------------------

    get feed() {
        return (this.state.summary && this.state.summary.feed) || { last_push: null, last_push_fresh: false };
    }
    feedDotStyle() {
        return "font-size:8px;color:" + (this.feed.last_push_fresh ? "#198754" : "var(--edi-muted)");
    }
    feedText() {
        const f = this.feed;
        if (!f.last_push) return "no poll yet";
        return (f.last_push_fresh ? "Live · " : "") + "last poll " + f.last_push;
    }

    async onRunPoll() {
        // A specific partner is scoped -> run its poll now; "all" -> open the
        // Trading Partners list where the per-partner Run Poll lives (there is
        // no single target to poll across every partner at once).
        if (!this.partnerParam) {
            this.actionService.doAction("mml_edi.action_edi_trading_partner");
            return;
        }
        try {
            const partners = await this.orm.searchRead(
                "edi.trading.partner", [["code", "=", this.partnerParam]], ["id"], { limit: 1 });
            if (!partners.length) return;
            await this.orm.call("edi.trading.partner", "action_run_poll_now", [[partners[0].id]]);
            this.notification.add("Poll started — check the live feed.", { type: "success" });
            await this._load();
        } catch (e) {
            this.notification.add(
                "Poll failed: " + (e.data ? e.data.message : e.message), { type: "danger" });
        }
    }

    // ---- formatting / navigation --------------------------------------------

    get partners() {
        return (this.state.summary && this.state.summary.partners) || [];
    }

    _openRecord(model, resId) {
        if (!model || !resId) return;
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: model,
            res_id: resId,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("edi_dashboard", EdiDashboard);

export { EdiDashboard };

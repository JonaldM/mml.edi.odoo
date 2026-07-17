/** @odoo-module **/
// mml_edi/static/src/js/edi_partner_health.js
//
// Trading-partner health screen. Recreates design_handoff "Trading Partner
// Health.dc.html" as a production OWL 2 client action, continuing the 3PL / ROQ
// visual language and the shared EDI theme (edi_shared.scss). Layout (README EDI
// screen 4):
//   - control bar: back-to-dashboard, title, subtitle, Open settings
//   - 24h exchange-queue strip (Polled → Failed)
//   - master-detail: left partner rail + selected-partner detail (health chip +
//     diagnosis hint + 4 stats + circuit breaker + transport + 7-day throughput
//     + recent exchanges)
//
// ONE batched RPC — edi.partner.health.get_health_summary() — returns every
// partner's detail bundle, so selecting a partner in the rail is a client-side
// swap (no extra round-trip). The selected partner persists per user
// (localStorage, by partner CODE so it survives a re-install that renumbers ids).
// Odoo's native colour scheme drives light/dark; no custom toggle.

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const STORAGE_KEY = "edi_health_partner";
const REFRESH_MS = 5 * 60 * 1000;

class EdiPartnerHealth extends Component {
    static template = "mml_edi.EdiPartnerHealth";

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.notification = useService("notification");

        this.state = useState({
            loading: true,
            error: null,
            summary: null,
            sel: this._readStored(),
        });

        this._refreshInterval = null;

        onWillStart(async () => {
            await this._load();
        });
        onMounted(() => {
            this._refreshInterval = setInterval(() => this._load(), REFRESH_MS);
        });
        onWillUnmount(() => {
            if (this._refreshInterval) clearInterval(this._refreshInterval);
        });
    }

    // ---- data ----------------------------------------------------------------

    async _load() {
        try {
            const summary = await this.orm.call(
                "edi.partner.health", "get_health_summary", []);
            this._reconcileSelection(summary);
            Object.assign(this.state, { summary, loading: false, error: null });
        } catch (e) {
            Object.assign(this.state, {
                error: "Couldn't load trading-partner health",
                loading: false,
            });
        }
    }

    async onRetry() {
        this.state.loading = true;
        this.state.error = null;
        await this._load();
    }

    // Keep the stored selection valid: fall back to the first partner when the
    // stored code is gone (or nothing stored yet).
    _reconcileSelection(summary) {
        const partners = summary.partners || [];
        const codes = partners.map((p) => p.code);
        if (this.state.sel && codes.includes(this.state.sel)) return;
        this.state.sel = partners.length ? partners[0].code : null;
    }

    _readStored() {
        try {
            return window.localStorage.getItem(STORAGE_KEY) || null;
        } catch (e) {
            return null;
        }
    }
    _writeStored(code) {
        try {
            window.localStorage.setItem(STORAGE_KEY, String(code));
        } catch (e) {
            // storage unavailable (private mode) — selection just won't persist.
        }
    }

    // ---- selection -----------------------------------------------------------

    get partners() {
        return (this.state.summary && this.state.summary.partners) || [];
    }
    get queue() {
        return (this.state.summary && this.state.summary.queue) || [];
    }

    selectPartner(code) {
        this.state.sel = code;
        this._writeStored(code);
    }
    isSelected(code) {
        return this.state.sel === code;
    }

    get selectedRow() {
        return this.partners.find((p) => p.code === this.state.sel) || null;
    }
    get selected() {
        const row = this.selectedRow;
        const details = (this.state.summary && this.state.summary.details) || {};
        return row ? details[String(row.id)] || null : null;
    }

    partnerRowStyle(code) {
        const active = this.isSelected(code);
        return (
            "border-left-color:" + (active ? "var(--edi-accent)" : "transparent") +
            (active ? ";background:var(--edi-accent-bg)" : "")
        );
    }

    // ---- throughput bar scaling ---------------------------------------------
    // Scale every bar against the max stacked (files+orders) value across ALL
    // partners, so the 7-day chart is comparable between partners (matches the
    // prototype's single global max).
    get _maxThroughput() {
        const details = (this.state.summary && this.state.summary.details) || {};
        let max = 1;
        for (const key in details) {
            for (const b of details[key].throughput || []) {
                max = Math.max(max, (b.files || 0) + (b.orders || 0));
            }
        }
        return max;
    }
    throughputBars() {
        const s = this.selected;
        if (!s) return [];
        const max = this._maxThroughput;
        return (s.throughput || []).map((b) => ({
            day: b.day,
            ordersH: ((b.orders || 0) / max * 100).toFixed(1) + "%",
            filesH: ((b.files || 0) / max * 100).toFixed(1) + "%",
        }));
    }

    // ---- transport badge -----------------------------------------------------

    transportBadge(badge) {
        if (badge === "ok") {
            return { text: "✓", style: "background:#d1e7dd;color:#0a3622" };
        }
        if (badge === "warn") {
            return { text: "!", style: "background:#fff3cd;color:#664d03" };
        }
        return null;
    }

    // ---- navigation / actions ------------------------------------------------

    openDashboard() {
        this.actionService.doAction("mml_edi.action_edi_dashboard");
    }
    openSettings() {
        const action =
            (this.state.summary && this.state.summary.settings_action) ||
            "mml_edi.action_edi_trading_partner";
        this.actionService.doAction(action);
    }
    openFullLog() {
        this.actionService.doAction("mml_edi.action_edi_processing_log");
    }

    async onRunPoll() {
        const row = this.selectedRow;
        if (!row) return;
        try {
            await this.orm.call("edi.trading.partner", "action_run_poll_now", [[row.id]]);
            this.notification.add("Poll started — check the recent exchanges.", {
                type: "success",
            });
            await this._load();
        } catch (e) {
            this.notification.add(
                "Poll failed: " + (e.data ? e.data.message : e.message),
                { type: "danger" });
        }
    }
}

registry.category("actions").add("edi_partner_health", EdiPartnerHealth);

export { EdiPartnerHealth };

/** @odoo-module **/
// mml_edi/static/src/js/edi_processing_log.js
//
// Processing-log browser. Recreates design_handoff "Processing Log.dc.html" as a
// production OWL 2 client action, sharing the EDI theme (edi_shared.scss).
// Layout (README EDI screen 6):
//   - control bar: back-to-dashboard, title, visible count, direction segmented
//     filter, status segmented filter, free-text search
//   - master-detail: day-grouped append-only edi.log rows (left) + per-file
//     exchange-chain rail (right) with SHA-256, dedup status and manager-only
//     technical detail
//
// Two batched RPCs: get_log_page (the filtered row list) and get_exchange_chain
// (the rail for the selected file row). The direction + status filters persist
// per user (localStorage); the log is append-only, so nothing is written here.
// Odoo's native colour scheme drives light/dark; no custom toggle.

import { registry } from "@web/core/registry";
import { Component, useState, useRef, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";

const DIR_KEY = "edi_log_dir";
const STATUS_KEY = "edi_log_status";
const SEARCH_DEBOUNCE_MS = 300;
const REFRESH_MS = 5 * 60 * 1000;

const DIR_PILLS = [
    ["all", "All"], ["inbound", "Inbound"], ["outbound", "Outbound"], ["internal", "Internal"],
];
const STATUS_PILLS = [
    ["all", "Any"], ["success", "OK"], ["warning", "Warn"], ["error", "Error"],
];

function readStored(key, fallback) {
    try {
        return window.localStorage.getItem(key) || fallback;
    } catch (e) {
        return fallback;
    }
}
function writeStored(key, value) {
    try {
        window.localStorage.setItem(key, String(value));
    } catch (e) {
        // storage unavailable (private mode) — the filter just won't persist.
    }
}

class EdiProcessingLog extends Component {
    static template = "mml_edi.EdiProcessingLog";

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.searchInputRef = useRef("searchInput");

        this.dirPillDefs = DIR_PILLS;
        this.statusPillDefs = STATUS_PILLS;

        this.state = useState({
            loading: true,
            error: null,
            page: null,
            dir: readStored(DIR_KEY, "all"),
            status: readStored(STATUS_KEY, "all"),
            query: "",
            sel: null,
            exchange: null,
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

    // ---- data ----------------------------------------------------------------

    async _load() {
        try {
            const page = await this.orm.call(
                "edi.processing.log", "get_log_page", [],
                { direction: this.state.dir, status: this.state.status, query: this.state.query });
            this.state.page = page;
            this.state.loading = false;
            this.state.error = null;
            this._reconcileSelection(page);
            await this._loadExchange();
        } catch (e) {
            this.state.error = "Couldn't load the processing log";
            this.state.loading = false;
        }
    }

    async onRetry() {
        this.state.loading = true;
        this.state.error = null;
        await this._load();
    }

    // Keep the selected row valid under the current filter: if the stored
    // selection dropped out of the page, fall back to the first file-bearing row.
    _reconcileSelection(page) {
        const ids = (page.rows || []).filter((r) => r.selectable).map((r) => r.id);
        if (this.state.sel && ids.includes(this.state.sel)) return;
        this.state.sel = page.first_selectable || null;
    }

    async _loadExchange() {
        if (!this.state.sel) {
            this.state.exchange = null;
            return;
        }
        try {
            this.state.exchange = await this.orm.call(
                "edi.processing.log", "get_exchange_chain", [this.state.sel]);
        } catch (e) {
            this.state.exchange = null;
        }
    }

    // ---- filters -------------------------------------------------------------

    async setDir(key) {
        if (this.state.dir === key) return;
        this.state.dir = key;
        writeStored(DIR_KEY, key);
        this.state.loading = true;
        await this._load();
    }
    async setStatus(key) {
        if (this.state.status === key) return;
        this.state.status = key;
        writeStored(STATUS_KEY, key);
        this.state.loading = true;
        await this._load();
    }
    dirPillClass(key) {
        return "edi-seg-pill" + (this.state.dir === key ? " active" : "");
    }
    statusPillClass(key) {
        return "edi-seg-pill" + (this.state.status === key ? " active" : "");
    }

    onSearch(ev) {
        this.state.query = ev.target.value;
        if (this._searchTimer) clearTimeout(this._searchTimer);
        this._searchTimer = setTimeout(() => this._load(), SEARCH_DEBOUNCE_MS);
    }

    // ---- row list with day dividers -----------------------------------------

    get visibleCount() {
        return (this.state.page && this.state.page.count) || 0;
    }

    displayRows() {
        const rows = (this.state.page && this.state.page.rows) || [];
        const out = [];
        let lastDay = null;
        for (const r of rows) {
            if (r.day !== lastDay) {
                out.push({ isDay: true, key: "day-" + r.day + "-" + r.id, day: r.day });
                lastDay = r.day;
            }
            out.push({ isDay: false, key: "row-" + r.id, row: r });
        }
        return out;
    }

    rowClass(row) {
        return (
            "edi-log-row" +
            (row.selectable ? " edi-log-row--selectable" : "") +
            (this.state.sel === row.id ? " edi-log-row--selected" : "")
        );
    }

    async selectRow(row) {
        if (!row.selectable) return;
        this.state.sel = row.id;
        await this._loadExchange();
    }

    // ---- navigation ----------------------------------------------------------

    openDashboard() {
        this.actionService.doAction("mml_edi.action_edi_dashboard");
    }
}

registry.category("actions").add("edi_processing_log", EdiProcessingLog);

export { EdiProcessingLog };

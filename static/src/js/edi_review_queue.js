/** @odoo-module **/
// mml_edi/static/src/js/edi_review_queue.js
//
// EDI Review Queue (master-detail triage). Recreates design_handoff "Review
// Queue.dc.html" as a production OWL 2 client action, continuing the 3PL / ROQ
// visual language and the EDI dashboard's shared theme + partner-filter hook.
//
// Layout (README EDI screen 2):
//   - control bar: back-to-dashboard, title, summary line, trading-partner
//     segmented filter (persisted), group-filter pills (All/Blocking/Warnings/
//     Change/Auto-approved, persisted), Approve-all-clean + Re-poll actions
//   - degraded-partner banner (when a partner's ORDRSP uploads look degraded)
//   - grid 410px / 1fr: grouped master list (left) + detail pane (right)
//
// Two batched RPCs — edi.review.queue.get_queue(partner_code) for the list and
// edi.review.queue.get_review_detail(id) for the selected pane. The segmented
// partner control recomputes the whole board by re-calling get_queue scoped;
// the group pills filter the already-fetched groups client-side. State-mutating
// actions (approve / reject / reset / per-issue accept-correct-reject) call the
// REAL edi.order.review / edi.order.issue methods by id, then reload.

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart, onMounted, onWillUnmount } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { usePartnerFilter, EdiPartnerFilter } from "@mml_edi/utils/use_partner_filter";
import { groupVisible, filterPillLabel } from "@mml_edi/utils/review_format";

const PARTNER_KEY = "edi_review_queue_partner";
const GROUP_KEY = "edi_review_queue_group";
const REFRESH_MS = 5 * 60 * 1000;

const GROUP_PILLS = [
    ["all", "All"],
    ["blocking", "Blocking"],
    ["warning", "Warnings"],
    ["change", "Change"],
    ["auto", "Auto-approved"],
];

function readGroup() {
    try {
        return window.localStorage.getItem(GROUP_KEY) || "all";
    } catch (e) {
        return "all";
    }
}
function writeGroup(value) {
    try {
        window.localStorage.setItem(GROUP_KEY, String(value));
    } catch (e) {
        // storage unavailable — the filter simply won't persist.
    }
}

class EdiReviewQueue extends Component {
    static components = { EdiPartnerFilter };
    static template = "mml_edi.EdiReviewQueue";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.notification = useService("notification");
        this.partnerFilter = usePartnerFilter(PARTNER_KEY);

        this.state = useState({
            loading: true,
            refreshing: false,
            error: null,
            queue: null,
            group: readGroup(),
            selId: null,
            detail: null,
            detailLoading: false,
            acting: false,
        });

        this._refreshInterval = null;

        onWillStart(async () => {
            await this._load();
        });
        onMounted(() => {
            this._refreshInterval = setInterval(() => this._reload(), REFRESH_MS);
        });
        onWillUnmount(() => {
            if (this._refreshInterval) clearInterval(this._refreshInterval);
        });
    }

    get partnerParam() {
        return this.partnerFilter.partnerParam;
    }

    // ---- loading -------------------------------------------------------------

    async _load() {
        try {
            const queue = await this.orm.call(
                "edi.review.queue", "get_queue", [], { partner_code: this.partnerParam });
            this.partnerFilter.reconcilePartners(queue.partners);
            Object.assign(this.state, { queue, loading: false, error: null });
            this._selectFirst();
        } catch (e) {
            Object.assign(this.state, { error: "Couldn't load the review queue", loading: false });
        }
    }

    // A background/refresh reload keeps the current selection if it still exists.
    async _reload() {
        try {
            const queue = await this.orm.call(
                "edi.review.queue", "get_queue", [], { partner_code: this.partnerParam });
            this.partnerFilter.reconcilePartners(queue.partners);
            this.state.queue = queue;
            if (!this._selectionStillPresent()) {
                this._selectFirst();
            }
        } catch (e) {
            // keep the last good board on a transient refresh failure
        }
    }

    async onPartnerChange() {
        this.state.refreshing = true;
        try {
            const queue = await this.orm.call(
                "edi.review.queue", "get_queue", [], { partner_code: this.partnerParam });
            this.partnerFilter.reconcilePartners(queue.partners);
            this.state.queue = queue;
            this._selectFirst();
        } catch (e) {
            // Route a failed partner-switch RPC to the screen's own error state
            // (as _load does) instead of letting it escape as an unhandled OWL
            // crash dialog.
            this.state.error = "Couldn't load the review queue";
        } finally {
            this.state.refreshing = false;
        }
    }

    async onRetry() {
        this.state.loading = true;
        this.state.error = null;
        await this._load();
    }

    // ---- group filter (persisted) -------------------------------------------

    filterPills() {
        const counts = (this.state.queue && this.state.queue.counts) || {};
        return GROUP_PILLS.map(([key, label]) => ({
            key,
            label: filterPillLabel(label, counts[key]),
            cls: "edi-seg-pill" + (this.state.group === key ? " active" : ""),
        }));
    }
    setGroup(key) {
        if (key === this.state.group) return;
        this.state.group = key;
        writeGroup(key);
        // Selection is an index into the FILTERED list, so it must follow the
        // filter: if the currently-selected review is no longer visible under
        // the new group pill, reselect the first visible row (README master-
        // detail rule). Otherwise the detail pane would keep showing a review
        // with no highlighted row on the left.
        if (!this._visibleItems().some((it) => it.id === this.state.selId)) {
            this._selectFirst();
        }
    }

    get visibleGroups() {
        const groups = (this.state.queue && this.state.queue.groups) || [];
        return groups.filter((g) => groupVisible(g.key, this.state.group));
    }

    // ---- master selection ----------------------------------------------------

    _allItems() {
        const groups = (this.state.queue && this.state.queue.groups) || [];
        return groups.flatMap((g) => g.items);
    }
    _visibleItems() {
        return this.visibleGroups.flatMap((g) => g.items);
    }
    _selectionStillPresent() {
        return this._allItems().some((it) => it.id === this.state.selId);
    }
    _selectFirst() {
        const items = this._visibleItems();
        if (items.length) {
            this.selectRow(items[0].id);
        } else {
            this.state.selId = null;
            this.state.detail = null;
        }
    }

    async selectRow(id) {
        this.state.selId = id;
        this.state.detailLoading = true;
        try {
            this.state.detail = await this.orm.call(
                "edi.review.queue", "get_review_detail", [id]);
        } catch (e) {
            this.state.detail = null;
        } finally {
            this.state.detailLoading = false;
        }
    }

    rowClass(id) {
        return "edi-row edi-queue-row" + (id === this.state.selId ? " edi-row--selected" : "");
    }

    // ---- header actions ------------------------------------------------------

    openDashboard() {
        this.actionService.doAction("mml_edi.action_edi_dashboard");
    }
    openHistory() {
        this.actionService.doAction("mml_edi.action_edi_log");
    }
    openPartner(partnerId) {
        this.actionService.doAction("mml_edi.action_edi_trading_partner");
    }

    async onApproveAllClean() {
        const ids = (this.state.queue && this.state.queue.clean_ids) || [];
        if (!ids.length) return;
        this.state.acting = true;
        try {
            await this.orm.call("edi.order.review", "action_approve", [ids]);
            this.notification.add(`${ids.length} clean order(s) approved`, { type: "success" });
            await this._load();
        } catch (e) {
            this.notification.add(
                "Approve-all-clean failed: " + (e.data ? e.data.message : e.message),
                { type: "danger" });
        } finally {
            this.state.acting = false;
        }
    }

    onRepoll() {
        // No single target polls every partner at once — open Trading Partners,
        // where the per-partner Run Poll lives (same honesty as the dashboard).
        this.actionService.doAction("mml_edi.action_edi_trading_partner");
    }

    // ---- detail-pane record actions (write REAL state) -----------------------

    get detail() {
        return this.state.detail;
    }

    async _reviewAction(method) {
        const id = this.state.selId;
        if (!id) return;
        this.state.acting = true;
        try {
            await this.orm.call("edi.order.review", method, [[id]]);
            await this._load(); // the row may leave its group; reselect fresh
            this.notification.add("Done", { type: "success" });
        } catch (e) {
            this.notification.add(
                (e.data ? e.data.message : e.message) || "Action failed", { type: "danger" });
        } finally {
            this.state.acting = false;
        }
    }

    async onHeaderAction(action) {
        switch (action.key) {
            case "approve":
                return this._reviewAction("action_approve");
            case "approve_corrected":
                return this._reviewAction("action_approve_corrected");
            case "reject":
                return this._reviewAction("action_reject");
            case "reset":
                return this._reviewAction("action_reset_to_review");
            case "open_so":
                if (this.detail && this.detail.so && this.detail.so.id) {
                    this._openRecord("sale.order", this.detail.so.id);
                }
                return;
        }
    }
    headerBtnClass(kind) {
        return "btn btn-sm " + (kind === "primary" ? "btn-primary" : "btn-outline-secondary");
    }

    async onIssueAction(issue, action) {
        this.state.acting = true;
        try {
            const method = {
                accept: "action_accept",
                correct: "action_correct",
                reject: "action_reject_issue",
            }[action.key];
            await this.orm.call("edi.order.issue", method, [[issue.id]]);
            // Refresh just the detail pane — the review is still selected.
            await this.selectRow(this.state.selId);
        } catch (e) {
            this.notification.add(
                (e.data ? e.data.message : e.message) || "Resolution failed", { type: "danger" });
        } finally {
            this.state.acting = false;
        }
    }

    openFullDetail() {
        if (!this.state.selId) return;
        this.actionService.doAction({
            type: "ir.actions.client",
            tag: "edi_review_detail",
            name: "Order review",
            params: { review_id: this.state.selId },
        });
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

registry.category("actions").add("edi_review_queue", EdiReviewQueue);

export { EdiReviewQueue };

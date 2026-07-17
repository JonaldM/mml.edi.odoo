/** @odoo-module **/
// mml_edi/static/src/js/edi_review_detail.js
//
// EDI Order Review Detail (single record). Recreates design_handoff "Order
// Review Detail.dc.html" as a production OWL 2 client action: the standalone
// page for ONE edi.order.review, with the per-PO ORDRSP deferral panel
// (sibling-store progress), the issues + SO-lines, a lifecycle timeline and the
// message log (chatter). Opened from the Review Queue's "Open full detail →"
// with { review_id } in the action params.
//
// Reads the same batched edi.review.queue.get_review_detail(id) payload the
// queue's inline pane uses (a superset — this page renders the ORDRSP panel,
// timeline and chatter the pane omits). Header actions (approve / reject /
// reset / open SO) call the REAL edi.order.review methods by id.

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { storeProgressPct } from "@mml_edi/utils/review_format";

class EdiReviewDetail extends Component {
    static template = "mml_edi.EdiReviewDetail";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.notification = useService("notification");

        const action = this.props.action || {};
        const params = action.params || {};
        const ctx = action.context || {};
        this.reviewId = params.review_id || ctx.review_id || ctx.active_id || null;

        this.state = useState({
            loading: true,
            error: null,
            detail: null,
            acting: false,
        });

        onWillStart(async () => {
            await this._load();
        });
    }

    async _load() {
        if (!this.reviewId) {
            Object.assign(this.state, { loading: false, detail: null });
            return;
        }
        try {
            const detail = await this.orm.call(
                "edi.review.queue", "get_review_detail", [this.reviewId]);
            Object.assign(this.state, { detail, loading: false, error: null });
        } catch (e) {
            Object.assign(this.state, { error: "Couldn't load the review", loading: false });
        }
    }

    get detail() {
        return this.state.detail;
    }

    ordrspPct() {
        const o = this.detail && this.detail.ordrsp;
        if (!o) return 0;
        return storeProgressPct(o.resolved_stores, o.total_stores);
    }
    ordrspBarStyle() {
        return "width:" + this.ordrspPct() + "%;background:#198754";
    }

    // ---- record actions ------------------------------------------------------

    async _reviewAction(method) {
        if (!this.reviewId) return;
        this.state.acting = true;
        try {
            await this.orm.call("edi.order.review", method, [[this.reviewId]]);
            await this._load();
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
            await this._load();
        } catch (e) {
            this.notification.add(
                (e.data ? e.data.message : e.message) || "Resolution failed", { type: "danger" });
        } finally {
            this.state.acting = false;
        }
    }

    backToQueue() {
        this.actionService.doAction("mml_edi.action_edi_review_queue");
    }
    openSO() {
        if (this.detail && this.detail.so && this.detail.so.id) {
            this._openRecord("sale.order", this.detail.so.id);
        }
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

registry.category("actions").add("edi_review_detail", EdiReviewDetail);

export { EdiReviewDetail };

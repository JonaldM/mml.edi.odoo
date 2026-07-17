/** @odoo-module **/
// mml_edi/static/src/js/edi_mobile_triage.js
//
// EDI MOBILE TRIAGE — the approver's pocket view. Recreates design_handoff
// "Mobile Triage.dc.html" as a production OWL 2 client action (tag
// `edi_mobile_triage`): a responsive, one-tap variant of the review triage for
// Odoo mobile / PWA (the prototype's iOS frame is presentation-only). Layout
// (README EDI screen 8): a partner segmented filter, a "N pending · R blocking ·
// A warning" header, and a stack of triage cards each with a severity dot,
// partner tag, age pill, summary, and a primary + secondary one-tap action.
//
// One batched RPC — edi.mobile.triage.get_triage(partner_code) — returns the
// cards. Each action calls the REAL edi.order.review state machine
// (action_approve / action_reject), the idempotent ORDRSP retry, or opens the
// record form — no localStorage. Themes via Odoo's native colour scheme (the
// shared --edi-* token bridge), no custom toggle. Hit targets are >=46px.

import { registry } from "@web/core/registry";
import { Component, useState, onWillStart } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { formatAge } from "@mml_edi/utils/edi_format";
import { usePartnerFilter, EdiPartnerFilter } from "@mml_edi/utils/use_partner_filter";

const STORAGE_KEY = "edi_mobile_partner";

// Action variant -> button class (all >=46px tall via the SCSS min-height).
const VARIANT_CLASS = {
    accent: "edi-m-btn edi-m-btn--accent",
    approve: "edi-m-btn edi-m-btn--approve",
    danger: "edi-m-btn edi-m-btn--danger",
    neutral: "edi-m-btn edi-m-btn--neutral",
};

class EdiMobileTriage extends Component {
    static components = { EdiPartnerFilter };
    static template = "mml_edi.EdiMobileTriage";
    static props = ["*"];

    setup() {
        this.orm = useService("orm");
        this.actionService = useService("action");
        this.notification = useService("notification");
        this.partnerFilter = usePartnerFilter(STORAGE_KEY);

        this.state = useState({
            loading: true,
            refreshing: false,
            error: null,
            summary: null,
            busyId: null, // card whose action is in flight (locks its buttons)
        });

        onWillStart(async () => {
            await this._load();
        });
    }

    get partnerParam() {
        return this.partnerFilter.partnerParam;
    }

    async _load() {
        try {
            const summary = await this.orm.call(
                "edi.mobile.triage", "get_triage", [], { partner_code: this.partnerParam });
            this.partnerFilter.reconcilePartners(summary.partners);
            Object.assign(this.state, { summary, loading: false, error: null });
        } catch (e) {
            Object.assign(this.state, { error: "Couldn't load triage", loading: false });
        }
    }

    async onPartnerChange() {
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

    // ---- derived getters -----------------------------------------------------

    get partners() {
        return (this.state.summary && this.state.summary.partners) || [];
    }
    get dataAvailable() {
        return !!(this.state.summary && this.state.summary.data_available);
    }
    get pending() {
        return (this.state.summary && this.state.summary.pending) || 0;
    }
    get redCount() {
        return (this.state.summary && this.state.summary.red_count) || 0;
    }
    get amberCount() {
        return (this.state.summary && this.state.summary.amber_count) || 0;
    }
    get allClear() {
        return this.dataAvailable && !this.cards().length;
    }

    cards() {
        const cards = (this.state.summary && this.state.summary.cards) || [];
        return cards.map((c) => {
            const red = c.severity === "red";
            return {
                id: c.id,
                dotStyle: "background:" + (red ? "#dc3545" : "#ffc107"),
                partnerTag: c.partner_tag,
                title: c.title,
                summary: c.summary,
                age: formatAge(c.age_hours),
                ageClass: red ? "edi-m-age edi-m-age--red" : "edi-m-age edi-m-age--amber",
                actions: (c.actions || []).map((a, idx) => ({
                    key: c.id + "-" + a.action + "-" + idx,
                    action: a.action,
                    label: a.label,
                    cls: VARIANT_CLASS[a.variant] || VARIANT_CLASS.neutral,
                })),
                _card: c,
            };
        });
    }

    // ---- one-tap actions -----------------------------------------------------

    async onAction(card, action) {
        if (this.state.busyId) return; // one action at a time
        switch (action.action) {
            case "open":
                this._openReview(card.id);
                return;
            case "approve":
                await this._runReviewAction(card, "action_approve", "Approved");
                return;
            case "reject":
                await this._runReviewAction(card, "action_reject", "Rejected");
                return;
            case "retry_ack":
                await this._retryAck(card);
                return;
        }
    }

    async _runReviewAction(card, method, verb) {
        this.state.busyId = card.id;
        try {
            await this.orm.call("edi.order.review", method, [[card.id]]);
            this.notification.add(verb + " · " + card.title, { type: "success" });
            await this._load();
        } catch (e) {
            this.notification.add(
                verb + " failed: " + (e.data ? e.data.message : e.message),
                { type: "danger" });
        } finally {
            this.state.busyId = null;
        }
    }

    async _retryAck(card) {
        this.state.busyId = card.id;
        try {
            await this.orm.call("edi.processor", "retry_pending_acks", []);
            this.notification.add("Pending ACKs re-queued", { type: "success" });
            await this._load();
        } catch (e) {
            this.notification.add(
                "Retry failed: " + (e.data ? e.data.message : e.message), { type: "danger" });
        } finally {
            this.state.busyId = null;
        }
    }

    _openReview(resId) {
        this.actionService.doAction({
            type: "ir.actions.act_window",
            res_model: "edi.order.review",
            res_id: resId,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("actions").add("edi_mobile_triage", EdiMobileTriage);

export { EdiMobileTriage };

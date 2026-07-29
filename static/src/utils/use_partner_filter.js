/** @odoo-module **/
// mml_edi/static/src/utils/use_partner_filter.js
//
// Shared trading-partner segmented-filter hook + control component for the EDI
// client-action screens (the README "primary filter": All partners / Kestrelby
// / Nimbrel). Persists the selection per user in localStorage and exposes a
// `partnerParam` getter for RPC scoping. Modelled on the 3PL DC-filter hook
// (stock_3pl_core/utils/use_dc_filter.js) — the same persist + reconcile +
// onChange shape — so every EDI screen composes one control instead of
// re-authoring it.
//
// `storageKey` is caller-supplied so each screen persists independently; the
// dashboard passes "edi_dashboard_partner". The stored value is a partner CODE
// (or "all"), NOT an id, so it survives a re-install that renumbers ids.

import { useState, Component } from "@odoo/owl";

function readStored(key) {
    try {
        return window.localStorage.getItem(key) || "all";
    } catch (e) {
        return "all";
    }
}

function writeStored(key, value) {
    try {
        window.localStorage.setItem(key, String(value));
    } catch (e) {
        // storage unavailable (private mode) — the filter simply won't persist.
    }
}

// Call once from a component's setup(). Returns the reactive filter handle.
export function usePartnerFilter(storageKey) {
    const state = useState({ partner: readStored(storageKey) });

    return {
        state,
        get partnerParam() {
            return state.partner === "all" ? null : state.partner;
        },
        // Fall back to "all" if the stored code is no longer among the payload's
        // partner options, so the screen never sticks on a partner that has
        // been removed. Returns true if it changed.
        reconcilePartners(partners) {
            const codes = (partners || []).map((p) => p.code);
            if (state.partner !== "all" && !codes.includes(state.partner)) {
                state.partner = "all";
                writeStored(storageKey, "all");
                return true;
            }
            return false;
        },
        async setPartner(code, onChange) {
            const next = String(code);
            if (next === state.partner) return;
            state.partner = next;
            writeStored(storageKey, next);
            if (onChange) await onChange();
        },
    };
}

// Segmented "All partners" + per-partner pill control. Consumer supplies the
// partner options and the usePartnerFilter() handle.
export class EdiPartnerFilter extends Component {
    static template = "mml_edi.EdiPartnerFilter";
    static props = {
        partners: { type: Array, optional: true },
        partnerFilter: { type: Object },
        onChange: { type: Function, optional: true },
    };
    static defaultProps = { partners: [] };

    pillClass(code) {
        return "edi-seg-pill" + (this.props.partnerFilter.state.partner === code ? " active" : "");
    }
    setPartner(code) {
        return this.props.partnerFilter.setPartner(code, this.props.onChange);
    }
}

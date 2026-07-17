/** @odoo-module **/
// mml_edi/static/src/utils/review_format.js
//
// Small, dependency-free helpers for the EDI review-queue screens. Pure
// functions so they are unit-testable (static/tests/review_format.test.js)
// without mounting OWL or mocking RPCs. The heavy formatting is done server-side
// (edi.review.queue returns ready-to-render dicts); these cover the few
// client-only derivations — the group-filter visibility and the ORDRSP progress
// bar width.

// Width (%) of the per-PO ORDRSP progress bar. Clamped to 0..100 and 0 when the
// PO has no store-orders yet, so the bar never renders past its track or NaN.
export function storeProgressPct(resolved, total) {
    if (!total || total <= 0) return 0;
    const pct = (resolved / total) * 100;
    if (pct < 0) return 0;
    if (pct > 100) return 100;
    return pct;
}

// Whether a queue group is shown under the active group-filter pill. "all"
// shows every group; any other value shows only the matching group.
export function groupVisible(groupKey, activeFilter) {
    return activeFilter === "all" || activeFilter === groupKey;
}

// Segmented-pill label, e.g. ("Blocking", 3) -> "Blocking 3". The count is
// always appended (0 included) so an emptied group still reads honestly.
export function filterPillLabel(label, count) {
    return `${label} ${count || 0}`;
}

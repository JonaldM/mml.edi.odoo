/** @odoo-module **/
// mml_edi/static/src/utils/edi_format.js
//
// Small, dependency-free formatting helpers for the EDI client-action screens.
// Pure functions so they are unit-testable (static/tests/edi_format.test.js)
// without mounting OWL or mocking RPCs.

// Age label: minutes under an hour, "Nh" up to 48h, "Nd" beyond. "—" when the
// value is missing.
export function formatAge(hours) {
    if (hours === null || hours === undefined) return "—";
    if (hours < 1) return Math.round(hours * 60) + "m";
    if (hours >= 48) return Math.round(hours / 24) + "d";
    return Math.round(hours) + "h";
}

// Percentage with an explicit unit; "—" when unmeasured (null). The backend
// returns null (not 0) when a KPI has no denominator, so a fresh board reads
// "—" rather than a false 0.0%.
export function formatPct(value, digits = 1) {
    return typeof value === "number" ? value.toFixed(digits) + "%" : "—";
}

// Bare hour value ("3.2h"); "—" when unmeasured.
export function formatHours(value) {
    return typeof value === "number" ? value.toFixed(1) + "h" : "—";
}

// Whole-number count; "—" when missing.
export function formatCount(value) {
    return typeof value === "number" ? value.toString() : "—";
}

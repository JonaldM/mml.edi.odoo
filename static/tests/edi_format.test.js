/** @odoo-module **/
// mml_edi/static/tests/edi_format.test.js
//
// Pure unit tests for the EDI dashboard formatting helpers — no OWL mount, no
// RPC mocks. Run via web.assets_unit_tests (hoot).

import { describe, expect, test } from "@odoo/hoot";
import { formatAge, formatPct, formatHours, formatCount } from "@mml_edi/utils/edi_format";

describe("edi_format.formatAge", () => {
    test("missing value renders an em dash", () => {
        expect(formatAge(null)).toBe("—");
        expect(formatAge(undefined)).toBe("—");
    });
    test("sub-hour ages render minutes", () => {
        expect(formatAge(0.5)).toBe("30m");
    });
    test("hours render below 48h", () => {
        expect(formatAge(3)).toBe("3h");
        expect(formatAge(47)).toBe("47h");
    });
    test("48h+ rolls over to days", () => {
        expect(formatAge(48)).toBe("2d");
        expect(formatAge(72)).toBe("3d");
    });
});

describe("edi_format.formatPct", () => {
    test("null (unmeasured) renders an em dash, not a false 0%", () => {
        expect(formatPct(null)).toBe("—");
    });
    test("numeric renders one decimal by default", () => {
        expect(formatPct(78.4)).toBe("78.4%");
        expect(formatPct(100)).toBe("100.0%");
    });
});

describe("edi_format.formatHours", () => {
    test("numeric renders one-decimal hours; null an em dash", () => {
        expect(formatHours(3.2)).toBe("3.2h");
        expect(formatHours(null)).toBe("—");
    });
});

describe("edi_format.formatCount", () => {
    test("numeric renders as a string; missing an em dash", () => {
        expect(formatCount(63)).toBe("63");
        expect(formatCount(0)).toBe("0");
        expect(formatCount(undefined)).toBe("—");
    });
});

/** @odoo-module **/
// mml_edi/static/tests/review_format.test.js
//
// Pure unit tests for the review-queue helpers — no OWL mount, no RPC mocks.
// Run via web.assets_unit_tests (hoot).

import { describe, expect, test } from "@odoo/hoot";
import { storeProgressPct, groupVisible, filterPillLabel } from "@mml_edi/utils/review_format";

describe("review_format.storeProgressPct", () => {
    test("zero / no total renders an empty bar, never NaN", () => {
        expect(storeProgressPct(0, 0)).toBe(0);
        expect(storeProgressPct(2, 0)).toBe(0);
        expect(storeProgressPct(1, -1)).toBe(0);
    });
    test("partial progress is a plain percentage", () => {
        expect(storeProgressPct(2, 3)).toBeCloseTo(66.6667, 3);
        expect(storeProgressPct(1, 4)).toBe(25);
    });
    test("full progress caps at 100", () => {
        expect(storeProgressPct(3, 3)).toBe(100);
        expect(storeProgressPct(5, 3)).toBe(100);
    });
});

describe("review_format.groupVisible", () => {
    test("'all' shows every group", () => {
        expect(groupVisible("blocking", "all")).toBe(true);
        expect(groupVisible("auto", "all")).toBe(true);
    });
    test("a specific filter shows only its own group", () => {
        expect(groupVisible("blocking", "blocking")).toBe(true);
        expect(groupVisible("warning", "blocking")).toBe(false);
    });
});

describe("review_format.filterPillLabel", () => {
    test("appends the count, including a truthful 0", () => {
        expect(filterPillLabel("Blocking", 3)).toBe("Blocking 3");
        expect(filterPillLabel("Warnings", 0)).toBe("Warnings 0");
        expect(filterPillLabel("Change", undefined)).toBe("Change 0");
    });
});

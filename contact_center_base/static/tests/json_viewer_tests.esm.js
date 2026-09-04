/** @odoo-module **/

/* global QUnit */

import {
    formatJsonSize,
    formatJsonValue,
    isEmptyJsonValue,
    jsonByteLength,
    jsonLineCount,
} from "@contact_center_base/js/json_viewer_utils.esm";

QUnit.module("contact_center_base > JSON viewer", (hooks) => {
    hooks.beforeEach(() => {
        const banner = document.getElementById("oe_neutralize_banner");
        if (banner) {
            (banner.parentElement || banner).remove();
        }
    });

    QUnit.test(
        "pretty-prints nested payloads without changing their values",
        (assert) => {
            const value = {
                event: "Message",
                nested: {active: true, ids: [1, 2]},
            };
            const formatted = formatJsonValue(value);

            assert.strictEqual(formatted, JSON.stringify(value, null, 2));
            assert.deepEqual(JSON.parse(formatted), value);
            assert.strictEqual(jsonLineCount(formatted), 10);
        }
    );

    QUnit.test("keeps provider markup as inert text for t-esc rendering", (assert) => {
        const markup = '<img src=x onerror="globalThis.pwned=true">';
        const formatted = formatJsonValue({body: markup});
        const container = document.createElement("code");

        // OWL's t-esc renders through the text-content path: the provider value
        // must round-trip while never becoming an executable DOM element.
        container.textContent = formatted;

        assert.strictEqual(JSON.parse(container.textContent).body, markup);
        assert.strictEqual(container.querySelector("img"), null);
        assert.notOk(window.pwned);
    });

    QUnit.test("recognizes empty ORM values and reports UTF-8 size", (assert) => {
        assert.ok(isEmptyJsonValue(false));
        assert.ok(isEmptyJsonValue(null));
        assert.strictEqual(formatJsonValue(false), "");
        assert.strictEqual(formatJsonValue({}), "{}");
        assert.strictEqual(jsonByteLength("ação"), 6);
        assert.strictEqual(formatJsonSize(0), "0 B");
        assert.strictEqual(formatJsonSize(1024), "1,0 KB");
    });

    QUnit.test("does not crash on values that cannot be serialized", (assert) => {
        const cyclic = {};
        cyclic.self = cyclic;

        assert.strictEqual(formatJsonValue(cyclic), "");
        assert.strictEqual(jsonLineCount(""), 0);
    });
});

import fs from "node:fs";
import vm from "node:vm";
import assert from "node:assert/strict";
/* eslint-env node */
async function main() {
// Logic-only execution of repository QUnit cases. Native OWL/browser proof is separate.
const tests = [];
const pureModelCases = new Set([
  "metadata scope requires an exact versioned singleton and channel",
  "detail authorization is explicit with safe legacy full fallback",
  "full group detail survives normalization while compact stays seven fields",
  "filters only valid Contact Center bus notifications",
  "missing event channel is a data-free full invalidation, never a read or delivery patch",
]);
const sandbox = {
  console,
  Promise,
  performance,
  Event,
  EventTarget,
  window: Object.assign(new EventTarget(), {crypto: {}}),
  CustomEvent: class CustomEvent extends Event {
    constructor(name, opts = {}) {
      super(name);
      this.detail = opts.detail;
    }
  },
  browser: Object.assign(new EventTarget(), {setTimeout, clearTimeout}),
  document: new EventTarget(),
  session: {},
  registry: {category: () => ({add() { return undefined; }})},
  _t: (x) => x,
  outboundStructuredCapabilities: () => [],
  QUnit: {
    module(name, cb) {
      cb({beforeEach() { return undefined; }, afterEach() { return undefined; }, before() { return undefined; }, after() { return undefined; }});
    },
    test(name, fn) {
      // The remaining model suite mounts OWL components and requires native QUnit.
      if (
        !sandbox.testSource.endsWith("contact_center_model_tests.esm.js") ||
        pureModelCases.has(name)
      ) {
        tests.push({name, fn});
      }
    },
  },
};
sandbox.document.hidden = false;
const context = vm.createContext(sandbox);
for (const path of [
  "contact_center_ui/static/src/js/contact_center_model.esm.js",
  "contact_center_ui/static/src/js/contact_center_refresh.esm.js",
  "contact_center_ui/static/src/js/contact_center_store.esm.js",
  "contact_center_ui/static/src/js/contact_center_shared_reads.esm.js",
  "contact_center_ui/static/src/js/contact_center_systray.esm.js",
  "contact_center_ui/static/tests/shared_reads_tests.esm.js",
  "contact_center_ui/static/tests/systray_tests.esm.js",
  "contact_center_ui/static/tests/contact_center_model_tests.esm.js",
  "contact_center_ui/static/tests/e2_test_helpers.esm.js",
  "contact_center_ui/static/tests/selected_detail_tests.esm.js",
  "contact_center_ui/static/tests/conversation_delta_tests.esm.js",
]) {
  sandbox.testSource = path;
  const original = fs.readFileSync(path, "utf8");
  const exported = [
    ...original.matchAll(/\bexport\s+(?:async\s+)?(?:const|class|function)\s+(\w+)/g),
  ].map((item) => item[1]);
  const source = original
    .replace(/^import[\s\S]*?;\s*$/gm, "")
    .replace(/\bexport\s+(?=(?:async\s+)?(?:const|class|function)\b)/g, "");
  const result = vm.runInContext(
    `(function(){${source}; return {${exported.join(",")}};})()`,
    context,
    {filename: path}
  );
  Object.assign(sandbox, result);
}
let count = 0;
for (const test of tests) {
  let assertions = 0;
  const checks = {
    strictEqual(a, b, m) {
      assertions++;
      assert.strictEqual(a, b, m);
    },
    notStrictEqual(a, b, m) {
      assertions++;
      assert.notStrictEqual(a, b, m);
    },
    deepEqual(a, b, m) {
      assertions++;
      assert.deepEqual(JSON.parse(JSON.stringify(a)), JSON.parse(JSON.stringify(b)), m);
    },
    ok(a, m) {
      assertions++;
      assert.ok(a, m);
    },
    notOk(a, m) {
      assertions++;
      assert.ok(!a, m);
    },
    throws(fn, expected, m) {
      assertions++;
      assert.throws(
        fn,
        (e) =>
          !expected ||
          (expected.test ? expected.test(e.message) : e.name === expected.name),
        m
      );
    },
  };
  await test.fn(checks);
  count += assertions;
  console.log(`PASS ${test.name} (${assertions} assertions)`);
}
console.log(
  `RESULT ${tests.length} tests, ${count} assertions, zero failures (isolated Node, not native QUnit).`
);

}
main().catch((error) => { console.error(error); process.exitCode = 1; });

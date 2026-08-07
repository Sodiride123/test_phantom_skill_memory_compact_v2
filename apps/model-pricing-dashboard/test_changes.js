/* Regression tests for app.js deriveChanges() — the per-model price-change
 * badge derivation (issue #114). Run directly:
 *     node test_changes.js
 * (also exercised from test_changes.py via subprocess so it runs under pytest).
 * No DOM, no network — deriveChanges is exported via a CommonJS shim. */

const assert = require("assert");
const { deriveChanges } = require("./app.js");

let passed = 0;
function test(name, fn) {
  try {
    fn();
    console.log("  ok   " + name);
    passed++;
  } catch (e) {
    console.log("  FAIL " + name + " :: " + e.message);
    process.exitCode = 1;
  }
}

const snap = (date, prices) => ({ date, prices });

test("no change -> empty", () => {
  const h = [
    snap("2026-08-01", { "xAI/Grok 4": { input: 3.0, output: 15.0 } }),
    snap("2026-08-07", { "xAI/Grok 4": { input: 3.0, output: 15.0 } }),
  ];
  assert.deepStrictEqual(deriveChanges(h), {});
});

test("up + down detected with from/to/dir/date", () => {
  const h = [
    snap("2026-08-01", {
      "xAI/Grok 4": { input: 3.0, cached: 0.75, output: 15.0 },
      "OpenAI/GPT-4o": { input: 2.5, output: 10.0 },
    }),
    snap("2026-08-07", {
      "xAI/Grok 4": { input: 1.25, cached: 0.2, output: 2.5 }, // all down
      "OpenAI/GPT-4o": { input: 3.5, output: 10.0 },          // input up, output same
    }),
  ];
  const c = deriveChanges(h);
  assert.deepStrictEqual(c["xAI/Grok 4"].input_price,
    { from: 3.0, to: 1.25, dir: "down", date: "2026-08-07" });
  assert.deepStrictEqual(c["xAI/Grok 4"].output_price,
    { from: 15.0, to: 2.5, dir: "down", date: "2026-08-07" });
  assert.strictEqual(c["OpenAI/GPT-4o"].input_price.dir, "up");
  assert.ok(!("output_price" in c["OpenAI/GPT-4o"]), "unchanged field omitted");
});

test("new/removed models and missing fields are skipped safely", () => {
  const h = [
    snap("2026-08-01", { "A/X": { input: 1.0 } }),
    snap("2026-08-07", {
      "A/X": { input: 1.0, output: 5.0 }, // output appeared (prev missing) -> skip
      "B/Y": { input: 2.0 },              // new model (no prev) -> skip
    }),
  ];
  assert.deepStrictEqual(deriveChanges(h), {});
});

test("non-numeric / null values ignored", () => {
  const h = [
    snap("2026-08-01", { "A/X": { cached: null } }),
    snap("2026-08-07", { "A/X": { cached: 0.5 } }),
  ];
  assert.deepStrictEqual(deriveChanges(h), {});
});

test("fewer than two snapshots -> empty; malformed tolerated", () => {
  assert.deepStrictEqual(deriveChanges([]), {});
  assert.deepStrictEqual(deriveChanges([snap("d", {})]), {});
  assert.deepStrictEqual(deriveChanges(null), {});
  assert.deepStrictEqual(deriveChanges([{ date: "a" }, { date: "b" }]), {});
});

console.log(`\n${passed} test(s) passed`);
if (process.exitCode) throw new Error("deriveChanges tests failed");

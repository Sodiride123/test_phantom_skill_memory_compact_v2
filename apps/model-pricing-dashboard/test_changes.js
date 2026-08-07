/* Regression tests for app.js deriveChanges() — the per-model price-change
 * badge derivation (issue #114). Run directly:
 *     node test_changes.js
 * (also exercised from test_changes.py via subprocess so it runs under pytest).
 * No DOM, no network — deriveChanges is exported via a CommonJS shim. */

const assert = require("assert");
const { deriveChanges, buildTrend, trendMetric } = require("./app.js");

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

// ---- buildTrend (issue #115): provider average price over time ----
test("buildTrend averages per provider per run", () => {
  const h = [
    snap("2026-08-01", {
      "OpenAI/A": { input: 2, output: 8 },
      "OpenAI/B": { input: 4, output: 12 },
      "xAI/C": { input: 1, output: 3 },
    }),
    snap("2026-08-07", {
      "OpenAI/A": { input: 1, output: 6 },
      "OpenAI/B": { input: 1, output: 10 },
      "xAI/C": { input: 5, output: 9 },
    }),
  ];
  const t = buildTrend(h, "output_price");
  assert.deepStrictEqual(t.labels, ["2026-08-01", "2026-08-07"]);
  // OpenAI avg output: run1 (8+12)/2=10, run2 (6+10)/2=8
  assert.deepStrictEqual(t.providers.OpenAI, [10, 8]);
  assert.deepStrictEqual(t.providers.xAI, [3, 9]);
});

test("buildTrend sparse provider -> null (gap), empty provider dropped", () => {
  // xAI has a model with numeric input in run1 but not run2 -> null gap in run2.
  // DeepSeek never appears -> its all-null series is dropped from the legend.
  const h = [
    snap("2026-08-01", { "OpenAI/A": { input: 2, output: 8 }, "xAI/C": { input: 5, output: 9 } }),
    snap("2026-08-07", { "OpenAI/A": { input: 2, output: 8 }, "xAI/C": { output: 9 } }),
  ];
  const t = buildTrend(h, "input_price");
  assert.deepStrictEqual(t.providers.OpenAI, [2, 2]);
  assert.deepStrictEqual(t.providers.xAI, [5, null]); // run2 input missing -> null (spanGaps bridges it)
  assert.ok(!("DeepSeek" in t.providers), "all-null provider dropped");
  assert.ok(!("Mistral" in t.providers));
});

test("buildTrend blended needs both sides; non-numeric ignored", () => {
  const h = [snap("2026-08-01", {
    "OpenAI/A": { input: 2, output: 8 },       // blended 5
    "OpenAI/B": { input: null, output: 8 },    // blended null (no input)
  })];
  const t = buildTrend(h, "blended");
  // Only A contributes: blended (2+8)/2 = 5; B skipped (null input).
  assert.deepStrictEqual(t.providers.OpenAI, [5]);
  assert.strictEqual(trendMetric({ input: 2, output: 8 }, "blended"), 5);
  assert.strictEqual(trendMetric({ input: null, output: 8 }, "blended"), null);
  assert.strictEqual(trendMetric({ input: 2 }, "output_price"), undefined);
});

test("buildTrend tolerates empty/malformed history", () => {
  assert.deepStrictEqual(buildTrend(null, "output_price"), { labels: [], providers: {} });
  // A snapshot with no prices still contributes a label but an empty series.
  assert.deepStrictEqual(buildTrend([{ date: "x" }], "output_price"), { labels: ["x"], providers: {} });
  assert.deepStrictEqual(buildTrend([], "output_price"), { labels: [], providers: {} });
});

console.log(`\n${passed} test(s) passed`);
if (process.exitCode) throw new Error("tests failed");

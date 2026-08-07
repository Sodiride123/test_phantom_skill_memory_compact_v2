/* Regression tests for app.js themeChartColors() — the chart palette used by
 * the light/dark theme toggle (issue #131). Run directly:
 *     node test_theme.js
 * (also exercised from test_theme.py via subprocess so it runs under pytest).
 * No DOM, no network — themeChartColors is exported via a CommonJS shim. */

const assert = require("assert");
const { themeChartColors } = require("./app.js");

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

const HEX = /^#[0-9a-f]{6}$/i;

test("dark palette has text/muted/grid hex colors", () => {
  const c = themeChartColors("dark");
  for (const k of ["text", "muted", "grid"]) {
    assert.ok(HEX.test(c[k]), `dark.${k} is a hex color, got ${c[k]}`);
  }
});

test("light palette has text/muted/grid hex colors", () => {
  const c = themeChartColors("light");
  for (const k of ["text", "muted", "grid"]) {
    assert.ok(HEX.test(c[k]), `light.${k} is a hex color, got ${c[k]}`);
  }
});

test("light and dark palettes differ on every channel", () => {
  const d = themeChartColors("dark");
  const l = themeChartColors("light");
  for (const k of ["text", "muted", "grid"]) {
    assert.notStrictEqual(d[k], l[k], `${k} should differ between themes`);
  }
});

test("dark palette matches the legacy hard-coded chart colors", () => {
  // Pins the pre-#131 colors so the default theme renders exactly as before.
  assert.deepStrictEqual(themeChartColors("dark"), {
    text: "#e6ebf5",
    muted: "#94a2bd",
    grid: "#2a3550",
  });
});

test("unknown / missing theme falls back to dark", () => {
  for (const bogus of [undefined, null, "", "solarized", "DARK"]) {
    assert.deepStrictEqual(themeChartColors(bogus), themeChartColors("dark"));
  }
});

console.log(`\n${passed} test(s) passed`);

// web/scripts/live_parity.py가 만든 픽스처로 web/live.js 결과를 pipeline 엔진(확정 계산)과 비교한다.
import fs from "node:fs";
import { applyLive, parseUs10y } from "../live.js";
const cases = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const us = parseUs10y(fs.readFileSync(new URL("../../data/us10y/dgs10.csv", import.meta.url), "utf8"));
const fields = ["price", "price_date", "dps", "yield", "multiple", "pct", "p10", "p90", "tv"];
let ok = 0, skipped = {}, exact = 0; const diffs = {}; const worst = {};
for (const c of cases) {
  const res = applyLive(c.A, c.rows, us, c.today);
  if (!res.ok) { skipped[res.reason] = (skipped[res.reason] || 0) + 1; continue; }
  ok++;
  const L = res.analysis, B = c.B;
  let allExact = true;
  const cmp = (name, a, b) => {
    const d = a == null || b == null ? (a === b ? 0 : Infinity) : typeof a === "string" ? (a === b ? 0 : Infinity) : Math.abs(a - b);
    if (d !== 0) allExact = false;
    if (!(name in worst) || d > worst[name].d) worst[name] = { d, a, b, code: c.code, k: c.k };
  };
  for (const f of fields) cmp(`summary.${f}`, L.summary[f], B.summary[f]);
  for (const f of ["as_of", "us10y", "us10y_date"]) cmp(f, L[f], B[f]);
  for (const f of ["n", "p10", "p50", "p90", "min", "max", "percentile"]) cmp(`multiple_stats.${f}`, L.multiple_stats?.[f], B.multiple_stats?.[f]);
  for (const f of ["percentile", "p50"]) cmp(`yield_stats.${f}`, L.yield_stats?.[f], B.yield_stats?.[f]);
  cmp("series.len", L.series.d.length, B.series.d.length);
  const n = B.series.d.length;
  for (const f of ["d", "px", "dps", "y", "u", "m"]) cmp(`series.${f}[-1]`, L.series[f][n - 1], B.series[f][n - 1]);
  if (allExact) exact++;
}
console.log(`cases=${cases.length} computed=${ok} exact-all-fields=${exact} skipped=${JSON.stringify(skipped)}`);
for (const [k, v] of Object.entries(worst)) if (v.d !== 0) console.log(`  max diff ${k}: ${v.d}  (live=${v.a} engine=${v.b} ${v.code} k=${v.k})`);

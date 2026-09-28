/**
 * KRX 최신 시세로 저장된 analysis.json을 '잠정' 갱신한다 (Worker에서 실행).
 *
 * 확정 계산은 여전히 GitHub Actions의 pipeline/engine.py가 한다. 여기서는 가격에만 의존하는 값
 * (현재 주가·배당수익률·배당/10Y 배수·역사적 백분위·차트 마지막 구간)을 engine.py와 같은 공식으로 다시 계산한다.
 * 가정이 하나라도 깨지면(분할·병합 의심, 배당 확정일 경과, 연도 변경, 저장 데이터와 KRX 불일치 등)
 * 계산하지 않고 {ok:false, reason}을 돌려 화면이 Actions 결과를 기다리게 한다.
 *
 * 대응하는 Python: pipeline/cache.py compute_analysis(), pipeline/engine.py dividend_yield(), us10y_multiple(),
 * us10y_asof(), detect_corp_actions(), quantile(), percentile_rank(), history_stats().
 */
const CORP_ACTION_THRESHOLD = 0.02;   // pipeline/config.py
const MIN_US10Y_FOR_MULTIPLE = 0.0;   // pipeline/config.py
const HISTORY_YEARS = 10;             // pipeline/config.py

/** pipeline/cache.py _r(): 소수 n자리 반올림, 유한하지 않으면 null */
export const r = (x, n = 4) => (x == null || !Number.isFinite(x) ? null : Number(x.toFixed(n)));

export const dividendYield = (dps, price) =>
  dps == null || price == null || price <= 0 || dps < 0 ? null : (dps / price) * 100.0;
export const us10yMultiple = (y, u, min = MIN_US10Y_FOR_MULTIPLE) => (y == null || u == null || u <= min ? null : y / u);

/** t보다 앞선 날짜 중 가장 최근 DGS10 (한국 장 마감 시점엔 미국 당일 값이 없다) */
export function us10yAsof(t, usDates, usVals) {
  let lo = 0, hi = usDates.length;            // bisect_left
  while (lo < hi) { const mid = (lo + hi) >> 1; if (usDates[mid] < t) lo = mid + 1; else hi = mid; }
  const i = lo - 1;
  return i < 0 ? [null, null] : [usVals[i], usDates[i]];
}

export function quantile(sorted, q) {
  const n = sorted.length;
  if (n === 1) return sorted[0];
  const pos = q * (n - 1), lo = Math.floor(pos), hi = Math.min(lo + 1, n - 1);
  return sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo);
}

export function historyStats(values, current) {
  const vals = values.filter((v) => v != null && Number.isFinite(v)).sort((a, b) => a - b);
  if (vals.length < 20 || current == null) return null;
  let below = 0, equal = 0;
  for (const v of vals) { if (v < current) below++; else if (v === current) equal++; }
  return {
    n: vals.length,
    p10: quantile(vals, 0.10), p25: quantile(vals, 0.25), p50: quantile(vals, 0.50),
    p75: quantile(vals, 0.75), p90: quantile(vals, 0.90),
    min: vals[0], max: vals[vals.length - 1], current,
    percentile: ((below + 0.5 * equal) / vals.length) * 100.0,
  };
}
const roundStats = (h) => (h ? Object.fromEntries(Object.entries(h).map(([k, v]) => [k, r(v)])) : null);

/** 등락률(기준가 대비)로 권리락(분할·병합·무상증자) 여부 판단 — engine.detect_corp_actions와 같은 식 */
function corpActionOn(prevClose, row) {
  if (!(prevClose > 0) || !(row.close > 0) || row.change_pct == null || Number.isNaN(row.change_pct)) return false;
  const base = row.close / (1.0 + row.change_pct / 100.0);
  return base > 0 && Math.abs(prevClose / base - 1.0) > CORP_ACTION_THRESHOLD;
}

function statsStart(asOf) {   // date(as_of.year - 10, as_of.month, min(as_of.day, 28))
  const [y, m, d] = asOf.split("-").map(Number);
  return `${y - HISTORY_YEARS}-${String(m).padStart(2, "0")}-${String(Math.min(d, 28)).padStart(2, "0")}`;
}

/**
 * @param a      저장된 analysis.json (Actions가 만든 확정 결과)
 * @param rows   KRX 단순종가 일별 시세 [{date, close, change_pct, trading_value}] — a.as_of 이전 거래일을 1일 이상 포함
 * @param us     {dates: [...YYYY-MM-DD], vals: [...]} data/us10y/dgs10.csv
 * @param today  평가일(KST 오늘, YYYY-MM-DD)
 */
export function applyLive(a, rows, us, today) {
  const fail = (reason) => ({ ok: false, reason });
  const s = a?.series;
  if (!s || !s.d?.length || a.summary?.dps == null) return fail("no_series");
  const asOf = a.as_of;
  const valuation = a.valuation_date || asOf;
  if (today.slice(0, 4) !== valuation.slice(0, 4)) return fail("year_changed");   // 예상 DPS 산식의 연도 기준이 바뀜

  rows = rows.filter((x) => x.date <= today);
  const iNew = rows.findIndex((x) => x.date >= asOf);
  if (iNew <= 0) return fail(iNew < 0 ? "no_new_rows" : "no_prior_row");
  const fresh = rows.slice(iNew);

  // 1) 저장 이후 권리락이 있으면 수정주가 전체가 바뀐다 → 확정 계산에 맡긴다.
  for (let i = iNew; i < rows.length; i++) {
    if (rows[i].date > asOf && corpActionOn(rows[i - 1].close, rows[i])) return fail("corp_action");
  }
  // 2) 저장 데이터와 KRX가 같은 종가를 말하는지(저장 직전 거래일). 그 뒤 권리락이 없으면 수정주가 = 원주가.
  const prior = rows[iNew - 1];
  const k = s.d.lastIndexOf(prior.date);
  const actionAfterPrior = (a.actions || []).some((x) => x.date > prior.date);
  if (k >= 0 && !actionAfterPrior && Math.abs(s.px[k] - prior.close) > Math.max(0.1, prior.close * 1e-4)) return fail("mismatch");

  // 3) 배당: 저장 이후 새 확정일이 없어야 DPS 계단값이 그대로다.
  const confirmed = [...(a.components || []).map((c) => c.confirmed), ...(a.annual || []).map((y) => y.confirmed)].filter(Boolean);
  if (confirmed.some((c) => c > asOf && c <= today)) return fail("dps_changed");
  const seriesDps = s.dps[s.dps.length - 1];
  const dps = a.summary.dps;   // expected_dps_asof(평가일) — 새 보고서가 없으면 평가일이 바뀌어도 같다

  // 4) 차트 시계열: 저장 기준일(장중 값일 수 있음) 이후를 KRX 값으로 교체·추가
  const cut = s.d.findIndex((d) => d >= asOf);
  const keep = cut < 0 ? s.d.length : cut;
  const out = { d: s.d.slice(0, keep), px: s.px.slice(0, keep), dps: s.dps.slice(0, keep),
                y: s.y.slice(0, keep), u: s.u.slice(0, keep), m: s.m.slice(0, keep) };
  const fullM = out.m.slice(), fullY = out.y.slice();   // 통계는 새 구간을 반올림 전 값으로 쓴다
  for (const row of fresh) {
    const y = dividendYield(seriesDps, row.close);
    const [u] = us10yAsof(row.date, us.dates, us.vals);
    const m = us10yMultiple(y, u);
    out.d.push(row.date); out.px.push(r(row.close, 1)); out.dps.push(r(seriesDps, 2));
    out.y.push(r(y, 3)); out.u.push(r(u, 3)); out.m.push(r(m, 3));
    fullM.push(m); fullY.push(y);
  }

  // 5) 현재값·역사 통계 (compute_analysis와 같은 순서)
  const last = fresh[fresh.length - 1];
  const price = last.close;
  const [curUs, curUsDate] = us10yAsof(today, us.dates, us.vals);
  const y = dividendYield(dps, price);
  const m = us10yMultiple(y, curUs);
  const start = statsStart(last.date);
  const inWin = out.d.map((d) => d >= start);
  const hist = historyStats(fullM.filter((_, i) => inWin[i]), m);
  const yhist = historyStats(fullY.filter((_, i) => inWin[i]), y);

  const summary = {
    ...a.summary,
    price: r(price, 2), price_date: last.date, yield: r(y), multiple: r(m),
    pct: hist ? r(hist.percentile, 1) : null, p10: hist ? r(hist.p10) : null, p90: hist ? r(hist.p90) : null,
    tv: r(last.trading_value, 0),
  };
  return {
    ok: true,
    analysis: {
      ...a,
      as_of: last.date, valuation_date: today,
      current_price: summary.price, dividend_yield: summary.yield,
      us10y: r(curUs, 3), us10y_date: curUsDate,
      dividend_yield_to_us10y: summary.multiple, historical_percentile: summary.pct,
      summary,
      multiple_stats: roundStats(hist), historical_multiple: roundStats(hist), yield_stats: roundStats(yhist),
      series: out,
    },
    info: { replaced_from: asOf, rows: fresh.length, last_date: last.date },
  };
}

/** data/us10y/dgs10.csv 텍스트 → {dates, vals} */
export function parseUs10y(csv) {
  const dates = [], vals = [];
  for (const line of csv.split(/\r?\n/).slice(1)) {
    const [d, v] = line.split(",");
    if (d && v !== undefined && v !== "" && Number.isFinite(Number(v))) { dates.push(d); vals.push(Number(v)); }
  }
  return { dates, vals };
}

/**
 * 메인 화면 목록의 빠른 잠정 갱신: KRX 전종목 시세 + 저장된 예상 DPS·미국10Y로 주가·배당수익률·배수만 다시 계산한다.
 * 역사적 백분위는 과거 시계열이 필요해 계산하지 않는다(마지막 확정값 유지). 속도 우선이라 종목별 권리락 판정 대신,
 * 저장 주가와 2배 이상 차이 나면(분할·병합 의심) 그 종목은 건너뛴다.
 * @param rows   index.json stocks[]
 * @param prices Map(code → {close, change_pct, trading_value})  (tradeDate 하루치)
 */
export function liveIndexRows(rows, prices, tradeDate, us, today) {
  const [u] = us10yAsof(today, us.dates, us.vals);
  const out = {};
  for (const row of rows) {
    const p = prices.get(row.code);
    if (!p || (row.price_date && row.price_date > tradeDate)) continue;
    if (row.price > 0 && (p.close / row.price > 2 || row.price / p.close > 2)) continue;
    const y = dividendYield(row.dps, p.close);
    out[row.code] = {
      price: r(p.close, 2), price_date: tradeDate, change_pct: r(p.change_pct, 2),
      yield: r(y), multiple: r(us10yMultiple(y, u)),
    };
  }
  return { us10y: r(u, 3), rows: out };
}

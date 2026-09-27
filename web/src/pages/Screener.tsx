import { useEffect, useMemo, useState } from "react";
import type { Meta, ScreenerRow } from "../lib/types";
import { mult, pct, won, NA } from "../lib/format";
import { BandGauge } from "../components/BandGauge";

type MarketSel = "ALL" | "KOSPI" | "KOSDAQ";
type SortKey = "mult_desc" | "mult_asc" | "yield_desc" | "dps_desc" | "paid_desc";

interface Filters {
  market: MarketSel;
  minPaid: number;       // 최근 10년 중 최소 배당 지급 연수
  minMult: number | "";  // 최소 배당/10Y
  minYield: number | ""; // 최소 배당수익률 %
  minCap: number | "";   // 최소 시가총액 (억원)
  minTv: number | "";    // 최소 거래대금 (억원)
  sort: SortKey;
  q: string;
}

const DEFAULTS: Filters = { market: "ALL", minPaid: 0, minMult: "", minYield: "", minCap: "", minTv: "", sort: "mult_desc", q: "" };
const KEY = "screener-filters-v1";

function loadFilters(): Filters {
  try {
    const s = localStorage.getItem(KEY);
    return s ? { ...DEFAULTS, ...JSON.parse(s) } : DEFAULTS;
  } catch { return DEFAULTS; }
}

const SORTS: { key: SortKey; label: string }[] = [
  { key: "mult_desc", label: "배당/10Y 높은 순" },
  { key: "mult_asc", label: "배당/10Y 낮은 순" },
  { key: "yield_desc", label: "배당수익률 높은 순" },
  { key: "dps_desc", label: "DPS 높은 순" },
  { key: "paid_desc", label: "배당 지속성 높은 순" },
];

/** null은 정렬 방향과 관계없이 맨 아래 */
function cmp(a: number | null, b: number | null, dir: 1 | -1): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  return (a - b) * dir;
}

function numInput(v: string): number | "" {
  if (v.trim() === "") return "";
  const n = Number(v);
  return Number.isFinite(n) ? n : "";
}

export function Screener({ meta, rows }: { meta: Meta; rows: ScreenerRow[] }) {
  const [f, setF] = useState<Filters>(loadFilters);
  const [limit, setLimit] = useState(150);
  useEffect(() => { try { localStorage.setItem(KEY, JSON.stringify(f)); } catch { /* 저장 실패는 무시 */ } }, [f]);
  const set = <K extends keyof Filters>(k: K, v: Filters[K]) => { setF((p) => ({ ...p, [k]: v })); setLimit(150); };

  const list = useMemo(() => {
    const q = f.q.trim().toLowerCase();
    const out = rows.filter((r) => {
      if (f.market !== "ALL" && r.market !== f.market) return false;
      if (q && !r.name.toLowerCase().includes(q) && !r.code.includes(q)) return false;
      if (f.minPaid > 0 && (r.paid10 ?? -1) < f.minPaid) return false;
      if (f.minMult !== "" && (r.multiple ?? -Infinity) < f.minMult) return false;
      if (f.minYield !== "" && (r.yield ?? -Infinity) < f.minYield) return false;
      if (f.minCap !== "" && (r.mcap ?? -Infinity) < f.minCap * 1e8) return false;
      if (f.minTv !== "" && (r.tv ?? -Infinity) < f.minTv * 1e8) return false;
      return true;
    });
    const s = f.sort;
    out.sort((a, b) =>
      s === "mult_desc" ? cmp(a.multiple, b.multiple, -1)
      : s === "mult_asc" ? cmp(a.multiple, b.multiple, 1)
      : s === "yield_desc" ? cmp(a.yield, b.yield, -1)
      : s === "dps_desc" ? cmp(a.dps, b.dps, -1)
      : cmp(a.paid10, b.paid10, -1) || cmp(a.multiple, b.multiple, -1));
    return out;
  }, [rows, f]);

  return (
    <main className="page">
      <header className="masthead">
        <h1>배당수익률 ÷ 미국 10년물</h1>
        <dl className="headline-figures">
          <div><dt>기준일</dt><dd>{meta.as_of}</dd></div>
          <div>
            <dt>미국 10년물</dt>
            <dd>{pct(meta.us10y)}<small>{meta.us10y_date ? ` ${meta.us10y_date} 값` : ""}</small></dd>
          </div>
        </dl>
      </header>

      <section className="controls" aria-label="조건">
        <div className="seg" role="radiogroup" aria-label="시장">
          {(["ALL", "KOSPI", "KOSDAQ"] as const).map((m) => (
            <button key={m} role="radio" aria-checked={f.market === m} className={f.market === m ? "on" : ""}
              onClick={() => set("market", m)}>{m === "ALL" ? "전체" : m}</button>
          ))}
        </div>

        <div className="field">
          <span className="field-label">최근 10년 중 배당 지급</span>
          <div className="seg small">
            {[0, 5, 8, 10].map((n) => (
              <button key={n} className={f.minPaid === n ? "on" : ""} onClick={() => set("minPaid", n)}>
                {n === 0 ? "전체" : n === 10 ? "10년" : `${n}년 이상`}
              </button>
            ))}
          </div>
          <label className="inline">
            <input type="number" inputMode="numeric" min={0} max={10} value={f.minPaid}
              onChange={(e) => set("minPaid", Math.max(0, Math.min(10, Number(e.target.value) || 0)))} />년 이상
          </label>
        </div>

        <label className="field">
          <span className="field-label">최소 배당/10Y</span>
          <span className="inline"><input type="number" inputMode="decimal" step="0.1" placeholder="예: 1.0" value={f.minMult}
            onChange={(e) => set("minMult", numInput(e.target.value))} />x</span>
        </label>
        <label className="field">
          <span className="field-label">정렬</span>
          <select value={f.sort} onChange={(e) => set("sort", e.target.value as SortKey)}>
            {SORTS.map((s) => <option key={s.key} value={s.key}>{s.label}</option>)}
          </select>
        </label>
        <label className="field grow">
          <span className="field-label">종목 찾기</span>
          <input type="search" placeholder="이름 또는 코드" value={f.q} onChange={(e) => set("q", e.target.value)} />
        </label>
        <details className="more-filters">
          <summary>추가 조건{[f.minYield, f.minCap, f.minTv].filter((v) => v !== "").length > 0 ? " (적용 중)" : ""}</summary>
          <div className="more-filters-body">
          <label className="field">
            <span className="field-label">최소 배당수익률</span>
            <span className="inline"><input type="number" inputMode="decimal" step="0.5" value={f.minYield}
              onChange={(e) => set("minYield", numInput(e.target.value))} />%</span>
          </label>
          <label className="field">
            <span className="field-label">최소 시가총액</span>
            <span className="inline"><input type="number" inputMode="numeric" step="100" value={f.minCap}
              onChange={(e) => set("minCap", numInput(e.target.value))} />억원</span>
          </label>
          <label className="field">
            <span className="field-label">최소 거래대금(기준일)</span>
            <span className="inline"><input type="number" inputMode="numeric" step="1" value={f.minTv}
              onChange={(e) => set("minTv", numInput(e.target.value))} />억원</span>
          </label>
          </div>
        </details>
        <button className="reset" onClick={() => setF(DEFAULTS)}>조건 초기화</button>
      </section>

      <p className="result-count">{list.length.toLocaleString()}개 종목</p>

      <div className="table-wrap">
        <table className="grid">
          <thead>
            <tr>
              <th className="num rank">순위</th>
              <th className="stick">종목</th>
              <th className="hide-sm">시장</th>
              <th className="num hide-sm">주가</th>
              <th className="num hide-sm">예상 DPS</th>
              <th className="num">배당률</th>
              <th className="num hide-sm">미국10Y</th>
              <th className="num key">배당/10Y</th>
              <th className="num">10년 배당</th>
              <th>역사적 위치</th>
            </tr>
          </thead>
          <tbody>
            {list.slice(0, limit).map((r, i) => (
              <tr key={r.code} onClick={() => { window.location.hash = `#/stock/${r.code}`; }}>
                <td className="num rank">{i + 1}</td>
                <td className="stick">
                  <a href={`#/stock/${r.code}`} className="name">{r.name}</a>
                  <span className="code">{r.code}<span className="show-sm"> {r.market}</span></span>
                </td>
                <td className="hide-sm">{r.market}</td>
                <td className="num hide-sm">{won(r.price)}</td>
                <td className="num hide-sm">{r.has_div_data ? won(r.dps) : NA}</td>
                <td className="num">{pct(r.yield)}</td>
                <td className="num hide-sm">{pct(meta.us10y)}</td>
                <td className="num key">{mult(r.multiple)}</td>
                <td className="num">{r.paid10 == null ? NA : `${r.paid10}/10`}</td>
                <td><BandGauge pct={r.pct} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {list.length > limit && (
        <button className="more" onClick={() => setLimit((l) => l + 300)}>
          {Math.min(300, list.length - limit)}개 더 보기
        </button>
      )}
      <p className="note">
        역사적 위치: 현재 배당/10Y 배수가 그 종목의 최근 10년 일별 값 중 몇 % 위치인지. 음영은 10~90% 구간.
        배당 데이터가 없는 종목은 N/A로 표시합니다.
      </p>
    </main>
  );
}

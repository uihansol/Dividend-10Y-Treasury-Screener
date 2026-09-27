import { useEffect, useMemo, useState } from "react";
import {
  Bar, BarChart, Brush, CartesianGrid, Cell, Legend, Line, LineChart, ReferenceDot, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { Detail } from "../lib/types";
import { flagText, mult, pct, signedPct, won, NA } from "../lib/format";

const DART = (rcp: string) => `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${rcp}`;
const C = {
  line: "var(--ink)", accent: "var(--accent)", band: "var(--ink-3)", grid: "var(--rule)",
  interim: "var(--accent-soft-strong)", price: "var(--accent)",
};

function Card({ label, value, sub, strong }: { label: string; value: string; sub?: string; strong?: boolean }) {
  return (
    <div className={`card${strong ? " strong" : ""}`}>
      <div className="card-label">{label}</div>
      <div className="card-value">{value}</div>
      {sub && <div className="card-sub">{sub}</div>}
    </div>
  );
}

// ---------------------------------------------------------------- 확대(zoom)
type Range = { startIndex: number; endIndex: number };

/** 시계열 차트의 확대 범위. Brush를 드래그하면 startIndex/endIndex가 바뀌고,
 * 같은 범위를 여러 차트가 함께 쓰면(합쳐보기) 확대가 그대로 동기화된다. */
function useZoom(length: number) {
  const full = { startIndex: 0, endIndex: Math.max(length - 1, 0) };
  const [range, setRange] = useState<Range>(full);
  useEffect(() => setRange(full), [length]); // eslint-disable-line react-hooks/exhaustive-deps
  const onChange = (r: { startIndex?: number; endIndex?: number }) => {
    if (r.startIndex == null || r.endIndex == null || r.endIndex <= r.startIndex) return;
    setRange({ startIndex: r.startIndex, endIndex: r.endIndex });
  };
  return { range, onChange, zoomed: range.startIndex > 0 || range.endIndex < full.endIndex, reset: () => setRange(full) };
}

function ZoomBar({ zoomed, onReset }: { zoomed: boolean; onReset: () => void }) {
  return (
    <div className="zoom-row">
      <span className="zoom-hint">아래 눈금을 드래그하면 특정 기간을 확대해서 볼 수 있습니다.</span>
      {zoomed && <button type="button" className="zoom-reset" onClick={onReset}>전체 기간으로</button>}
    </div>
  );
}

// ---------------------------------------------------------------- 항목 선택/해제
/** 범례(항목 이름)를 누르면 그 항목만 숨긴다. 차트마다 독립적으로 쓴다. */
function useToggle() {
  const [hidden, setHidden] = useState<Record<string, boolean>>({});
  const toggle = (key: string) => setHidden((h) => ({ ...h, [key]: !h[key] }));
  const isHidden = (key: string) => !!hidden[key];
  // recharts의 Legend onClick/formatter 타입은 넓어서(Payload) any로 받고 dataKey만 꺼내 쓴다.
  const onLegendClick = (e: any) => { if (e?.dataKey != null) toggle(String(e.dataKey)); };
  const legendFormatter = (value: string, entry: any) => (
    <span className={isHidden(String(entry?.dataKey)) ? "legend-off" : undefined}>{value}</span>
  );
  return { isHidden, toggle, onLegendClick, legendFormatter };
}

// ---------------------------------------------------------------- 탭
const TABS = ["종합", "배당", "10년물 비교", "밴드"] as const;
type Tab = (typeof TABS)[number];

/** 탭을 눌러도 페이지를 다시 그리거나 데이터를 다시 받지 않는다 — 이미 받은 analysis.json 안에서
 * 보여줄 섹션만 바꾼다(display:none으로 숨겨 차트 mount/unmount 비용도 없앤다). */
function Tabs({ active, onChange }: { active: Tab; onChange: (t: Tab) => void }) {
  return (
    <div className="tabs" role="tablist" aria-label="종목 상세 탭">
      {TABS.map((t) => (
        <button key={t} type="button" role="tab" aria-selected={t === active}
          className={`tab${t === active ? " on" : ""}`} onClick={() => onChange(t)}>
          {t}
        </button>
      ))}
    </div>
  );
}

/** 종목 상세 (analysis.json 한 개를 그대로 그린다). 데이터 조회·갱신은 StockPage가 담당 */
export function DetailPage({ d }: { d: Detail }) {
  const series = useMemo(() => {
    const s = d.series;
    return s.d.map((date, i) => ({ date, m: s.m[i], y: s.y[i], u: s.u[i], dps: s.dps[i], px: s.px[i] }));
  }, [d]);

  const s = d.summary, st = d.multiple_stats, p = d.persistence;
  const rateCompare = useMemo(() => [
    { name: "배당수익률", v: s.yield ?? 0, fill: C.accent },
    { name: "미국 10년물", v: d.us10y ?? 0, fill: C.band },
  ], [s.yield, d.us10y]);
  const total = d.components.reduce((a, c) => a + c.dps, 0);
  const yearTicks = (data: { date: string }[]) =>
    data.filter((_, i) => i === 0 || data[i - 1].date.slice(0, 4) !== data[i].date.slice(0, 4)).map((x) => x.date);

  // 배당/10Y 배수 + 주가 (같은 기간을 함께 보므로 확대·범위는 하나로 동기화한다)
  const zoomM = useZoom(series.length);
  const viewM = series.slice(zoomM.range.startIndex, zoomM.range.endIndex + 1);
  const ticksM = yearTicks(viewM);
  const yMax = st ? Math.max(st.p90 * 1.8, st.current * 1.25) : undefined;
  const lastM = viewM[viewM.length - 1];
  const togM = useToggle();

  // 배당수익률 vs 미국 10년물
  const zoomY = useZoom(series.length);
  const viewY = series.slice(zoomY.range.startIndex, zoomY.range.endIndex + 1);
  const ticksY = yearTicks(viewY);
  const togY = useToggle();

  const [tab, setTab] = useState<Tab>("종합");
  const show = (t: Tab) => (t === tab ? undefined : { display: "none" as const });

  return (
    <main className="page detail">
      <header className="detail-head">
        <h1>{d.name}</h1>
        <span className="code">{d.code} {d.market}</span>
      </header>

      <section className="cards">
        <Card label="현재 주가" value={won(s.price)} sub={`${s.price_date} 종가`} />
        <Card label="예상 DPS" value={won(s.dps)} sub="현재까지 확정된 배당" />
        <Card label="배당수익률" value={pct(s.yield)} />
        <Card label="미국 10년물" value={pct(d.us10y)} sub={d.us10y_date ? `${d.us10y_date} 값` : undefined} />
        <Card label="배당/10Y" value={mult(s.multiple)} strong />
        <Card label="역사적 백분위" value={st ? `${st.percentile.toFixed(0)}%` : NA} sub="최근 10년 일별 배수 중" />
        <Card label="10년 배당" value={p.paid10 != null ? `${p.paid10}/10` : NA}
          sub={p.known10 != null && p.known10 < 10 ? `데이터 있는 해 ${p.known10}년` : undefined} />
      </section>

      <Tabs active={tab} onChange={setTab} />

      <div style={show("종합")}>
      <section className="panel">
        <h2>예상 DPS 구성</h2>
        <table className="composition">
          <tbody>
            {d.components.map((c) => (
              <tr key={c.label} className={c.kind === "provisional" ? "provisional" : undefined}>
                <td>
                  {c.label}
                  {c.kind === "provisional" && <span className="badge-prov" title="정기보고서 확정 전, 수시공시 기준">미확정</span>}
                </td>
                <td className="num">{won(c.dps)}원</td>
                <td className="src">
                  {c.ref ? <a href={DART(c.ref)} target="_blank" rel="noreferrer">{c.confirmed} 공시</a> : c.confirmed}
                </td>
              </tr>
            ))}
            <tr className="sum">
              <td>{d.components_status === "annual_confirmed" ? `${d.components[0]?.year}년 확정 DPS` : "현재 예상 DPS"}</td>
              <td className="num">{won(total)}원</td>
              <td />
            </tr>
          </tbody>
        </table>
        {s.flags.length > 0 && <ul className="flags">{s.flags.map((f) => <li key={f}>{flagText(f)}</li>)}</ul>}
      </section>

      <section className="panel">
        <h2>오늘, 배당수익률 vs 미국 10년물</h2>
        {s.yield != null && d.us10y != null ? (
          <>
            <div className="chart compare">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={rateCompare} layout="vertical" margin={{ top: 4, right: 44, left: 0, bottom: 4 }}>
                  <XAxis type="number" hide domain={[0, (dataMax: number) => dataMax * 1.15]} />
                  <YAxis type="category" dataKey="name" width={104} tickLine={false} axisLine={false} tick={{ fontSize: 13 }} />
                  <Bar dataKey="v" barSize={22} radius={[0, 4, 4, 0]} isAnimationActive={false}
                    label={{ position: "right", formatter: (v: number) => pct(v), fontSize: 13, fill: "var(--ink-2)" }}>
                    {rateCompare.map((r) => <Cell key={r.name} fill={r.fill} />)}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
            <p className="note">
              {s.multiple != null
                ? `배당수익률이 미국 10년물의 ${mult(s.multiple)} 수준입니다.`
                : "미국 10년물이 0% 이하라 배수를 계산하지 않습니다."}
            </p>
          </>
        ) : <p className="empty-msg">미국 10년물 데이터가 없어 비교할 수 없습니다.</p>}
      </section>
      </div>

      <div style={show("10년물 비교")}>
      <section className="panel">
        <div className="panel-head">
          <h2>배당수익률 vs 미국 10년물, 최근 10년</h2>
        </div>
        <div className="chart">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={viewY} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid stroke={C.grid} vertical={false} />
              <XAxis dataKey="date" ticks={ticksY} tickFormatter={(v: string) => v.slice(0, 4)} tick={{ fontSize: 12 }} />
              <YAxis width={44} tick={{ fontSize: 12 }} tickFormatter={(v: number) => `${v.toFixed(1)}%`} />
              <Tooltip formatter={(v: number, n: string) => [pct(v), n]} />
              <Legend wrapperStyle={{ fontSize: 12 }} onClick={togY.onLegendClick} formatter={togY.legendFormatter} />
              <Line dataKey="y" name="배당수익률" stroke={C.accent} dot={false} strokeWidth={1.3}
                isAnimationActive={false} hide={togY.isHidden("y")} />
              <Line dataKey="u" name="미국 10년물" stroke={C.band} dot={false} strokeWidth={1}
                isAnimationActive={false} hide={togY.isHidden("u")} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <div className="chart brush-nav">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={series} margin={{ top: 0, right: 8, left: 0, bottom: 0 }}>
              <Line dataKey="y" stroke={C.accent} dot={false} strokeWidth={1} isAnimationActive={false} />
              <Brush dataKey="date" height={28} travellerWidth={10} stroke={C.accent}
                startIndex={zoomY.range.startIndex} endIndex={zoomY.range.endIndex} onChange={zoomY.onChange}
                tickFormatter={(v: string) => v.slice(0, 4)} />
            </LineChart>
          </ResponsiveContainer>
        </div>
        <ZoomBar zoomed={zoomY.zoomed} onReset={zoomY.reset} />
      </section>
      </div>

      <div style={show("밴드")}>
      <section className="panel">
        <div className="panel-head">
          <h2>배당/10Y 배수 &amp; 주가, 최근 10년</h2>
        </div>
        {st ? (
          <>
            <div className="chart tall">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={viewM} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke={C.grid} vertical={false} />
                  <XAxis dataKey="date" ticks={ticksM} tickFormatter={(v: string) => v.slice(0, 4)} tick={{ fontSize: 12 }} />
                  {/* 세로축만 둘로 나눈 한 차트: 왼쪽=배당/10Y 배수, 오른쪽=주가(원). 각 축 눈금을 해당 선 색으로 칠해 구분한다. */}
                  <YAxis yAxisId="mult" domain={[0, yMax ?? "auto"]} allowDataOverflow width={44}
                    tick={{ fontSize: 12, fill: C.line }} tickFormatter={(v: number) => `${v.toFixed(1)}x`} />
                  <YAxis yAxisId="price" orientation="right" domain={["auto", "auto"]} width={64}
                    tick={{ fontSize: 12, fill: C.price }} tickFormatter={(v: number) => won(v)} />
                  <Tooltip formatter={(v: number, n: string) => [n === "주가" ? `${won(v)}원` : mult(v), n]}
                    labelFormatter={(l: string) => l} />
                  <Legend wrapperStyle={{ fontSize: 12 }} onClick={togM.onLegendClick} formatter={togM.legendFormatter} />
                  {([["p90", "90%"], ["p75", "75%"], ["p50", "50%"], ["p25", "25%"], ["p10", "10%"]] as const).map(([k, lab]) => (
                    <ReferenceLine key={k} yAxisId="mult" y={st[k]} stroke={C.band} strokeDasharray={k === "p50" ? undefined : "3 4"}
                      label={{ value: `${lab} ${st[k].toFixed(2)}`, position: "insideTopLeft", fontSize: 11, fill: "var(--ink-2)" }} />
                  ))}
                  <Line yAxisId="mult" type="monotone" dataKey="m" name="배당/10Y" stroke={C.line} dot={false} strokeWidth={1.3}
                    isAnimationActive={false} connectNulls={false} hide={togM.isHidden("m")} />
                  <Line yAxisId="price" type="monotone" dataKey="px" name="주가" stroke={C.price} dot={false} strokeWidth={1.3}
                    isAnimationActive={false} connectNulls={false} hide={togM.isHidden("px")} />
                  {lastM?.m != null && !togM.isHidden("m") && (
                    <ReferenceDot yAxisId="mult" x={lastM.date} y={lastM.m} r={5} fill={C.line} stroke="var(--surface)" />
                  )}
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="chart brush-nav">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={series} margin={{ top: 0, right: 8, left: 0, bottom: 0 }}>
                  <Line dataKey="m" stroke={C.line} dot={false} strokeWidth={1} isAnimationActive={false} />
                  <Brush dataKey="date" height={28} travellerWidth={10} stroke={C.accent}
                    startIndex={zoomM.range.startIndex} endIndex={zoomM.range.endIndex} onChange={zoomM.onChange}
                    tickFormatter={(v: string) => v.slice(0, 4)} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <ZoomBar zoomed={zoomM.zoomed} onReset={zoomM.reset} />
            <table className="quantiles">
              <thead><tr><th>10%</th><th>25%</th><th>50%</th><th>75%</th><th>90%</th><th className="now">현재</th></tr></thead>
              <tbody><tr>
                <td>{mult(st.p10)}</td><td>{mult(st.p25)}</td><td>{mult(st.p50)}</td>
                <td>{mult(st.p75)}</td><td>{mult(st.p90)}</td><td className="now">{mult(st.current)}</td>
              </tr></tbody>
            </table>
            <p className="note">
              일별 표본 {st.n.toLocaleString()}개. 왼쪽 세로축(배당/10Y 배수)은 금리가 매우 낮았던 시기의 급등 구간을 자르고
              표시합니다(최댓값 {mult(st.max)}). 백분위 계산에는 모든 값을 씁니다. 오른쪽 세로축은 주가(원)이며 단위가 달라
              배수와 눈금을 공유하지 않습니다. 범례 이름을 누르면 그 선만 숨길 수 있습니다.
            </p>
          </>
        ) : <p className="empty-msg">배수를 계산할 수 있는 날이 20일 미만입니다.</p>}
      </section>
      </div>

      <div style={show("배당")}>
      <section className="panel">
        <h2>연도별 실제 DPS (현재 주식 수 기준)</h2>
        {d.annual.length ? (
          <AnnualDpsChart annual={d.annual} accent={C.accent} interim={C.interim} grid={C.grid} />
        ) : <p className="empty-msg">확정된 연간 DPS가 없습니다.</p>}
        <p className="note">사업연도 귀속 기준. 분할·무상증자 등이 있으면 과거 DPS를 현재 1주 기준으로 환산했습니다.</p>
      </section>

      <section className="panel two-col">
        <div>
          <h2>배당 지속성</h2>
          <dl className="kv">
            <dt>최근 10년 중 배당</dt><dd>{p.paid10 ?? NA}년 / 10년</dd>
            <dt>최근 5년 중 배당</dt><dd>{p.paid5 ?? NA}년 / 5년</dd>
            <dt>배당 중단</dt><dd>{p.suspensions ?? NA}회</dd>
          </dl>
        </div>
        <div>
          <h2>DPS 변화</h2>
          <dl className="kv">
            <dt>10년 DPS CAGR</dt><dd>{signedPct(p.cagr10)}</dd>
            <dt>5년 DPS CAGR</dt><dd>{signedPct(p.cagr5)}</dd>
            <dt>최대 연간 감소폭</dt><dd>{signedPct(p.max_decline)}</dd>
            <dt>10년 전 DPS 대비 현재</dt><dd>{p.vs10y != null ? `${p.vs10y.toFixed(2)}배` : NA}
              {p.dps10y_ago != null && <small> ({p.latest_fy != null ? p.latest_fy - 10 : ""}년 {won(p.dps10y_ago)}원)</small>}</dd>
          </dl>
        </div>
      </section>
      </div>

      <section className="panel">
        <h2>사용한 데이터</h2>
        <ul className="provenance">
          <li>주가: KRX {s.price_date} 종가</li>
          <li>미국 10년물: FRED DGS10 {d.us10y_date ?? NA} 값 (기준일보다 앞선 가장 최근 값)</li>
          <li>배당: DART 정기보고서 {d.components.map((c) => `${c.confirmed} 접수`).join(", ") || NA}</li>
          <li>주식 수 변동 이벤트: {d.actions.length ? d.actions.map((a) => `${a.date} ×${a.ratio}`).join(", ") : "없음"}</li>
          <li>계산 기준일 {d.as_of}{d.metadata?.updated_at ? ` · 캐시 갱신 ${d.metadata.updated_at.replace("T", " ").slice(0, 16)}` : ""}</li>
        </ul>
      </section>
    </main>
  );
}

/** 연도별 중간·분기/기말 DPS 막대. 범례 이름을 누르면 그 항목을 숨긴다. */
function AnnualDpsChart({ annual, accent, interim, grid }: {
  annual: Detail["annual"]; accent: string; interim: string; grid: string;
}) {
  const tog = useToggle();
  return (
    <div className="chart">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={annual} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid stroke={grid} vertical={false} />
          <XAxis dataKey="year" tick={{ fontSize: 12 }} />
          <YAxis width={56} tick={{ fontSize: 12 }} tickFormatter={(v: number) => won(v)} />
          <Tooltip formatter={(v: number) => `${won(v)}원`} />
          <Legend wrapperStyle={{ fontSize: 12 }} onClick={tog.onLegendClick} formatter={tog.legendFormatter} />
          <Bar dataKey="interim" stackId="a" name="중간·분기" fill={interim} isAnimationActive={false} hide={tog.isHidden("interim")} />
          <Bar dataKey="final" stackId="a" name="기말" fill={accent} isAnimationActive={false} hide={tog.isHidden("final")} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

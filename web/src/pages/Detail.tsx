import { useEffect, useMemo, useState } from "react";
import {
  Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ReferenceDot, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import { loadDetail } from "../lib/data";
import type { Detail, Meta } from "../lib/types";
import { flagText, mult, pct, signedPct, won, NA } from "../lib/format";

const DART = (rcp: string) => `https://dart.fss.or.kr/dsaf001/main.do?rcpNo=${rcp}`;
const C = { line: "var(--ink)", accent: "var(--accent)", band: "var(--ink-3)", grid: "var(--rule)", interim: "var(--accent-soft-strong)" };

function Card({ label, value, sub, strong }: { label: string; value: string; sub?: string; strong?: boolean }) {
  return (
    <div className={`card${strong ? " strong" : ""}`}>
      <div className="card-label">{label}</div>
      <div className="card-value">{value}</div>
      {sub && <div className="card-sub">{sub}</div>}
    </div>
  );
}

export function DetailPage({ code, meta }: { code: string; meta: Meta }) {
  const [d, setD] = useState<Detail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    setD(null); setErr(null);
    loadDetail(code).then(setD).catch(() => setErr("이 종목은 배당 데이터가 없어 상세 분석을 만들지 않았습니다."));
  }, [code]);

  const series = useMemo(() => {
    if (!d) return [];
    const s = d.series;
    return s.d.map((date, i) => ({ date, m: s.m[i], y: s.y[i], u: s.u[i], dps: s.dps[i], px: s.px[i] }));
  }, [d]);

  if (err) return <main className="page"><a href="#/" className="back">← 스크리너</a><p className="empty-msg">{err}</p></main>;
  if (!d) return <main className="page"><a href="#/" className="back">← 스크리너</a><p className="loading">불러오는 중…</p></main>;

  const s = d.summary, st = d.multiple_stats, p = d.persistence;
  const yMax = st ? Math.max(st.p90 * 1.8, st.current * 1.25) : undefined;
  const last = series[series.length - 1];
  const total = d.components.reduce((a, c) => a + c.dps, 0);
  const ticks = series.filter((_, i) => i === 0 || series[i - 1].date.slice(0, 4) !== series[i].date.slice(0, 4)).map((x) => x.date);

  return (
    <main className="page detail">
      <a href="#/" className="back">← 스크리너</a>
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

      <section className="panel">
        <h2>예상 DPS 구성</h2>
        <table className="composition">
          <tbody>
            {d.components.map((c) => (
              <tr key={c.label}>
                <td>{c.label}</td>
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
        <h2>배당/10Y 배수, 최근 10년</h2>
        {st ? (
          <>
            <div className="chart tall">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={series} margin={{ top: 8, right: 56, left: 0, bottom: 0 }}>
                  <CartesianGrid stroke={C.grid} vertical={false} />
                  <XAxis dataKey="date" ticks={ticks} tickFormatter={(v: string) => v.slice(0, 4)} tick={{ fontSize: 12 }} />
                  <YAxis domain={[0, yMax ?? "auto"]} allowDataOverflow width={44} tick={{ fontSize: 12 }}
                    tickFormatter={(v: number) => `${v.toFixed(1)}x`} />
                  <Tooltip formatter={(v: number) => mult(v)} labelFormatter={(l: string) => l} />
                  {([["p90", "90%"], ["p75", "75%"], ["p50", "50%"], ["p25", "25%"], ["p10", "10%"]] as const).map(([k, lab]) => (
                    <ReferenceLine key={k} y={st[k]} stroke={C.band} strokeDasharray={k === "p50" ? undefined : "3 4"}
                      label={{ value: `${lab} ${st[k].toFixed(2)}`, position: "right", fontSize: 11, fill: "var(--ink-2)" }} />
                  ))}
                  <Line type="monotone" dataKey="m" name="배당/10Y" stroke={C.line} dot={false} strokeWidth={1.3}
                    isAnimationActive={false} connectNulls={false} />
                  {last?.m != null && <ReferenceDot x={last.date} y={last.m} r={5} fill={C.accent} stroke="var(--surface)" />}
                </LineChart>
              </ResponsiveContainer>
            </div>
            <table className="quantiles">
              <thead><tr><th>10%</th><th>25%</th><th>50%</th><th>75%</th><th>90%</th><th className="now">현재</th></tr></thead>
              <tbody><tr>
                <td>{mult(st.p10)}</td><td>{mult(st.p25)}</td><td>{mult(st.p50)}</td>
                <td>{mult(st.p75)}</td><td>{mult(st.p90)}</td><td className="now">{mult(st.current)}</td>
              </tr></tbody>
            </table>
            <p className="note">
              일별 표본 {st.n.toLocaleString()}개. 그래프 세로축은 금리가 매우 낮았던 시기의 급등 구간을 자르고 표시합니다
              (최댓값 {mult(st.max)}). 백분위 계산에는 모든 값을 씁니다.
            </p>
          </>
        ) : <p className="empty-msg">배수를 계산할 수 있는 날이 20일 미만입니다.</p>}
      </section>

      <section className="panel">
        <h2>연도별 실제 DPS (현재 주식 수 기준)</h2>
        {d.annual.length ? (
          <div className="chart">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={d.annual} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
                <CartesianGrid stroke={C.grid} vertical={false} />
                <XAxis dataKey="year" tick={{ fontSize: 12 }} />
                <YAxis width={56} tick={{ fontSize: 12 }} tickFormatter={(v: number) => won(v)} />
                <Tooltip formatter={(v: number) => `${won(v)}원`} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="interim" stackId="a" name="중간·분기" fill={C.interim} isAnimationActive={false} />
                <Bar dataKey="final" stackId="a" name="기말" fill={C.accent} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          </div>
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

      <section className="panel">
        <h2>배당수익률, 최근 10년</h2>
        <div className="chart">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={series} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
              <CartesianGrid stroke={C.grid} vertical={false} />
              <XAxis dataKey="date" ticks={ticks} tickFormatter={(v: string) => v.slice(0, 4)} tick={{ fontSize: 12 }} />
              <YAxis width={44} tick={{ fontSize: 12 }} tickFormatter={(v: number) => `${v.toFixed(1)}%`} />
              <Tooltip formatter={(v: number, n: string) => [pct(v), n]} />
              <Legend wrapperStyle={{ fontSize: 12 }} />
              <Line dataKey="y" name="배당수익률" stroke={C.accent} dot={false} strokeWidth={1.3} isAnimationActive={false} />
              <Line dataKey="u" name="미국 10년물" stroke={C.band} dot={false} strokeWidth={1} isAnimationActive={false} />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </section>

      <section className="panel">
        <h2>사용한 데이터</h2>
        <ul className="provenance">
          <li>주가: KRX {s.price_date} 종가</li>
          <li>미국 10년물: FRED DGS10 {d.us10y_date ?? NA} 값 (기준일보다 앞선 가장 최근 값)</li>
          <li>배당: DART 정기보고서 {d.components.map((c) => `${c.confirmed} 접수`).join(", ") || NA}</li>
          <li>주식 수 변동 이벤트: {d.actions.length ? d.actions.map((a) => `${a.date} ×${a.ratio}`).join(", ") : "없음"}</li>
          <li>계산 기준일 {meta.as_of}</li>
        </ul>
      </section>
    </main>
  );
}

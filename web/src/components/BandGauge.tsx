/** 역사적 위치 게이지: 0~100 백분위 축 위에 10~90% 구간을 칠하고 현재 위치를 점으로 표시 */
export function BandGauge({ pct }: { pct: number | null }) {
  if (pct == null) return <span className="na">N/A</span>;
  const tone = pct >= 90 ? "hi" : pct <= 10 ? "lo" : "mid";
  return (
    <span className="gauge" role="img" aria-label={`최근 10년 중 ${pct.toFixed(0)}% 위치`}>
      <span className="gauge-track">
        <span className="gauge-band" />
        <span className="gauge-mid" />
        <span className={`gauge-dot ${tone}`} style={{ left: `${Math.min(100, Math.max(0, pct))}%` }} />
      </span>
      <span className="gauge-num">{pct.toFixed(0)}%</span>
    </span>
  );
}

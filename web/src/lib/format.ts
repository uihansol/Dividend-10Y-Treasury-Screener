const nf0 = new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 });

export const NA = "N/A";

export function won(v: number | null | undefined): string {
  return v == null ? NA : nf0.format(Math.round(v));
}

export function pct(v: number | null | undefined, digits = 2): string {
  return v == null ? NA : `${v.toFixed(digits)}%`;
}

export function mult(v: number | null | undefined): string {
  return v == null ? NA : `${v.toFixed(2)}x`;
}

export function signedPct(v: number | null | undefined, digits = 1): string {
  if (v == null) return NA;
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

/** 원 → 억원/조원 */
export function krwBig(v: number | null | undefined): string {
  if (v == null) return NA;
  if (v >= 1e12) return `${(v / 1e12).toFixed(1)}조`;
  return `${nf0.format(Math.round(v / 1e8))}억`;
}

export const FLAG_TEXT: Record<string, string> = {
  prior_year_interim_not_counted: "전년도 중간·분기배당은 올해 확정 전이라 제외됨",
  prior_year_unconfirmed_periods_filled: "올해 아직 확정 안 된 분기는 전년도 같은 분기 배당으로 대체함",
  provisional_dividend_used: "정기보고서 확정 전, 수시공시(현금·현물배당결정)로 미리 반영한 값이 있음",
  interim_values_treated_as_per_period: "분기 보고서 값을 누적이 아닌 기간분으로 해석함 (확인 필요)",
  interim_exceeds_annual: "중간배당 누계가 연간 DPS보다 큼 (데이터 확인 필요)",
};

export function flagText(f: string): string {
  if (f.startsWith("negative_dps_ignored")) return "음수 DPS 값을 제외함";
  return FLAG_TEXT[f] ?? f;
}

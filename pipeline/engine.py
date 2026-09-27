"""계산 엔진 (입출력 없음, 순수 함수).

현재 스크리닝과 역사적 계산이 **같은 함수(expected_dps_asof)**를 쓰도록 만들어
두 계산 방식이 어긋나지 않게 한다(원칙 3).

용어
- 원(raw) DPS: DART 보고서에 적힌 그 당시 주식 수 기준 주당배당금
- 현재 기준(current-basis): 분할·병합·무상증자를 반영해 '오늘의 주식 1주' 기준으로 환산한 값
- 수정주가(adjusted close): 원주가를 현재 기준 주식 수로 환산한 값
  → 배당수익률 = 현재기준 DPS / 수정주가  ( = 그 날의 원 DPS / 그 날의 원주가 와 동일 )
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional, Sequence

PERIOD_ORDER = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}


# ---------------------------------------------------------------------------
# 자료 구조
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DividendReport:
    """DART 정기보고서 1건에서 읽은 누적 주당배당금."""

    fiscal_year: int
    period: str               # "Q1" | "H1" | "Q3" | "FY"
    cum_dps: float            # 해당 사업연도 기초~보고기간말 누적 주당 현금배당금 (원 DPS)
    basis_date: date          # 결산기준일(보고기간 말일). 이 날의 주식 수 기준 금액
    confirmed_date: date      # 보고서 접수일. 이 날부터 '알 수 있는' 정보
    ref: str = ""             # DART 접수번호 등 원천 참조


@dataclass(frozen=True)
class CorpAction:
    """주식 수 변동 이벤트. ratio = 이벤트 후 주식 수 / 이벤트 전 주식 수 (예: 50:1 분할 → 50)."""

    date: date                # 새 기준으로 거래가 시작된 첫 거래일
    ratio: float


@dataclass
class DpsComponent:
    label: str                # 예: "2025년 기말배당"
    kind: str                 # "final" | "interim" | "annual"
    fiscal_year: int
    dps: float                # 현재 기준
    confirmed_date: date
    ref: str = ""


@dataclass
class ExpectedDps:
    value: float
    components: list[DpsComponent]
    latest_fy: int            # 연간 DPS가 확정된 가장 최근 사업연도
    flags: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 주식 수 환산
# ---------------------------------------------------------------------------
def factor_after(d: date, actions: Sequence[CorpAction]) -> float:
    """d 이후(d 초과)에 일어난 모든 이벤트 비율의 곱. d 시점 1주 = 현재 몇 주인지."""
    f = 1.0
    for a in actions:
        if a.date > d:
            f *= a.ratio
    return f


def to_current_basis(raw_value: float, basis_date: date, actions: Sequence[CorpAction]) -> float:
    return raw_value / factor_after(basis_date, actions)


def adjusted_prices(dates: Sequence[date], closes: Sequence[float],
                    actions: Sequence[CorpAction]) -> list[float]:
    """원주가 → 현재 기준 수정주가. 이벤트 날짜 오름차순 가정 없이 동작."""
    acts = sorted(actions, key=lambda a: a.date)
    act_dates = [a.date for a in acts]
    # suffix product: 인덱스 i 이상 이벤트 비율의 곱
    suffix = [1.0] * (len(acts) + 1)
    for i in range(len(acts) - 1, -1, -1):
        suffix[i] = suffix[i + 1] * acts[i].ratio
    out = []
    for d, c in zip(dates, closes):
        i = bisect.bisect_right(act_dates, d)  # d 초과 이벤트의 시작 인덱스
        out.append(c / suffix[i])
    return out


# ---------------------------------------------------------------------------
# 연도별 배당 테이블
# ---------------------------------------------------------------------------
@dataclass
class FiscalYear:
    year: int
    fy_total: Optional[float] = None          # 현재 기준
    fy_confirmed: Optional[date] = None
    fy_ref: str = ""
    # 기간별 누적 중간·분기 배당 (현재 기준), 기간 순서대로
    interims: list[tuple[str, float, date, str]] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def interim_cum_asof(self, t: date) -> tuple[float, Optional[tuple[str, float, date, str]]]:
        best = None
        for rec in self.interims:
            if rec[2] <= t and (best is None or PERIOD_ORDER[rec[0]] > PERIOD_ORDER[best[0]]):
                best = rec
        return (best[1] if best else 0.0), best

    def interim_total(self) -> float:
        """사업연도 전체의 중간·분기배당 합계(가장 늦은 기간의 누적값)."""
        if not self.interims:
            return 0.0
        return max(self.interims, key=lambda r: PERIOD_ORDER[r[0]])[1]

    @property
    def final_dps(self) -> Optional[float]:
        if self.fy_total is None:
            return None
        return max(self.fy_total - self.interim_total(), 0.0)

    def has_interim(self) -> bool:
        return any(r[1] > 0 for r in self.interims)

    def is_quarterly(self) -> bool:
        return any(r[0] in ("Q1", "Q3") and r[1] > 0 for r in self.interims)


def build_fiscal_years(reports: Iterable[DividendReport],
                       actions: Sequence[CorpAction]) -> dict[int, FiscalYear]:
    """보고서 목록 → 사업연도별 테이블(모든 금액 현재 기준).

    분기 보고서 값이 누적이 아닌 '해당 기간분'으로 보이는 경우(뒤 기간 값이 앞 기간보다 작음)
    기간분으로 간주해 누적으로 바꾸고 플래그를 남긴다.
    """
    years: dict[int, FiscalYear] = {}
    for r in reports:
        if r.cum_dps is None or (isinstance(r.cum_dps, float) and math.isnan(r.cum_dps)):
            continue
        if r.cum_dps < 0:  # 음수 DPS는 정상 배당 데이터로 취급하지 않음
            fy = years.setdefault(r.fiscal_year, FiscalYear(r.fiscal_year))
            fy.flags.append(f"negative_dps_ignored:{r.period}")
            continue
        v = to_current_basis(r.cum_dps, r.basis_date, actions)
        fy = years.setdefault(r.fiscal_year, FiscalYear(r.fiscal_year))
        if r.period == "FY":
            fy.fy_total, fy.fy_confirmed, fy.fy_ref = v, r.confirmed_date, r.ref
        else:
            fy.interims = [x for x in fy.interims if x[0] != r.period]
            fy.interims.append((r.period, v, r.confirmed_date, r.ref))

    for fy in years.values():
        fy.interims.sort(key=lambda x: PERIOD_ORDER[x[0]])
        vals = [x[1] for x in fy.interims]
        if any(vals[i] + 1e-6 < vals[i - 1] for i in range(1, len(vals))):
            running, fixed = 0.0, []
            for p, v, c, ref in fy.interims:
                running += v
                fixed.append((p, running, c, ref))
            fy.interims = fixed
            fy.flags.append("interim_values_treated_as_per_period")
        if fy.fy_total is not None and fy.interim_total() > fy.fy_total + 1e-6:
            fy.flags.append("interim_exceeds_annual")
    return years


# ---------------------------------------------------------------------------
# 예상 연간 DPS (현재·역사 공통)
# ---------------------------------------------------------------------------
def _period_amounts(fy: FiscalYear) -> dict[str, float]:
    """사업연도 내 중간·분기 누적값을 실제 기간별 배당으로 변환한다."""
    recs = sorted(fy.interims, key=lambda x: PERIOD_ORDER[x[0]])
    out: dict[str, float] = {}
    prev = 0.0
    for period, cumulative, _, _ in recs:
        out[period] = max(cumulative - prev, 0.0)
        prev = cumulative
    return out


def _quarter_amounts(fy: Optional[FiscalYear]) -> dict[str, tuple[float, tuple[str, float, date, str]]]:
    """Q1/H1/Q3 누계 보고서에서 실제 Q1·Q2·Q3 배당을 복원한다."""
    if fy is None:
        return {}
    recs = {r[0]: r for r in fy.interims}
    out = {}
    if "Q1" in recs:
        out["Q1"] = (recs["Q1"][1], recs["Q1"])
    if "H1" in recs:
        h1 = recs["H1"][1]
        q1 = recs["Q1"][1] if "Q1" in recs else 0.0
        out["Q2"] = (max(h1 - q1, 0.0), recs["H1"])
    if "Q3" in recs:
        q3_cum = recs["Q3"][1]
        h1 = recs["H1"][1] if "H1" in recs else 0.0
        out["Q3"] = (max(q3_cum - h1, 0.0), recs["Q3"])
    return out

def expected_dps_asof(t: date, years: dict[int, FiscalYear], mode: str = "annual") -> Optional[ExpectedDps]:
    """기준일 t에 공개적으로 확정된 정보만으로 예상 연간 DPS를 계산한다.

    핵심 규칙
    - 일반(기말배당) 기업: 가장 최근 확정 연간 DPS의 기말배당을 사용한다.
    - 중간/분기배당 기업: 현재 연도에 확정된 배당은 그대로 사용하고,
      아직 확정되지 않은 뒤의 배당은 전년도 같은 기간의 배당으로 보완한다.
      예) 올해 Q1만 확정 → 올해 Q1 + 전년도 Q2 + 전년도 Q3 + 전년도 기말.
      올해 Q2까지 확정 → 올해 Q1~Q2 + 전년도 Q3 + 전년도 기말.
      올해 Q3까지 확정 → 올해 Q1~Q3 + 전년도 기말.
    - 현재 연도의 배당이 아직 하나도 확정되지 않았다면 직전 연도의 확정 연간 DPS를 사용한다.
    - 분기/중간배당 기업이라도 필요한 전년도 기간별 자료가 없으면 임의로 0을 넣지 않고
      확인 가능한 부분만으로 계산하지 않는다. 다만 전년도 기말배당은 연간 보고서로 확인한다.
    - mode는 기존 호환성을 위해 인자로 유지하지만, 이제 위 규칙이 단일 계산 규칙이다.
    """
    confirmed = [fy for fy in years.values()
                 if fy.fy_total is not None and fy.fy_confirmed is not None and fy.fy_confirmed <= t]
    if not confirmed:
        return None

    F = max(confirmed, key=lambda fy: fy.year)
    flags: list[str] = list(F.flags)
    final_f = F.final_dps or 0.0
    comps: list[DpsComponent] = []

    O = years.get(F.year + 1)
    current_rec = _confirmed_interim_asof(O, t) if O else None

    # 현재 연도에 중간/분기배당이 아직 하나도 확정되지 않은 경우.
    if O is None or current_rec is None:
        if F.has_interim():
            # 직전 연도의 연간 DPS가 이미 확정된 직후에는 그 연간 DPS 자체를 사용한다.
            last = F.interims[-1]
            comps.append(DpsComponent(f"{F.year}년 중간·분기배당", "interim", F.year,
                                      F.interim_total(), last[2], last[3]))
            comps.append(DpsComponent(f"{F.year}년 기말배당", "final", F.year,
                                      final_f, F.fy_confirmed, F.fy_ref))
            value = F.fy_total or 0.0
        else:
            comps.append(DpsComponent(f"{F.year}년 기말배당", "final", F.year,
                                      final_f, F.fy_confirmed, F.fy_ref))
            value = final_f
        return ExpectedDps(value=value, components=comps, latest_fy=F.year,
                           flags=sorted(set(flags)))

    # 현재 연도에 확정된 중간/분기배당이 있는 경우.
    flags += list(O.flags)

    # 분기배당 기업: 현재 확정 누계 + 전년도 미확정 기간의 동일 기간 배당.
    if O.is_quarterly():
        current_periods = _period_amounts(O)
        prior = years.get(F.year)
        prior_periods = _period_amounts(prior) if prior else {}

        latest_period = current_rec[0]
        confirmed_current = sum(v for p, v in current_periods.items()
                                if PERIOD_ORDER[p] <= PERIOD_ORDER[latest_period])

        # 전년도 같은 사업연도에서 이미 확정된 기간을 제외하고,
        # 현재 확정 시점 이후의 분기 배당을 전년도 값으로 보완한다.
        remaining_periods = [p for p in ("Q1", "H1", "Q3")
                             if PERIOD_ORDER[p] > PERIOD_ORDER[latest_period]]
        # H1은 Q1+Q2 누계이므로 실제 Q2 금액을 사용한다.
        # 전년도 Q3까지 자료가 있어야 Q2/Q3를 분리할 수 있다.
        prior_fill = []
        for p in remaining_periods:
            if p == "H1":
                # 내부적으로 H1 자체가 남는 경우는 없지만 방어적으로 처리.
                amount = prior_periods.get("H1")
            else:
                amount = prior_periods.get(p)
            if amount is None:
                flags.append(f"prior_year_period_missing:{p}")
            else:
                prior_fill.append((p, amount))

        # 전년도 기말배당은 항상 마지막에 보완한다.
        if prior is None or prior.fy_total is None or prior.fy_confirmed is None or prior.fy_confirmed > t:
            flags.append("prior_year_final_missing")
            prior_final = None
        else:
            prior_final = prior.final_dps or 0.0

        if len(prior_fill) != len(remaining_periods) or prior_final is None:
            return ExpectedDps(value=None, components=[], latest_fy=F.year,
                               flags=sorted(set(flags)))

        # 현재 확정분은 하나의 누계 구성요소로 표시하고,
        # 전년도 보완분은 기간별로 표시해 사용자가 어떤 가정이 들어갔는지 확인할 수 있게 한다.
        current_latest_value = current_rec[1]
        comps.append(DpsComponent(
            f"{O.year}년 {latest_period}까지 확정", "interim", O.year,
            current_latest_value, current_rec[2], current_rec[3]))

        for p, amount in prior_fill:
            label = {"H1": "2분기", "Q3": "3분기", "Q1": "1분기"}.get(p, p)
            rec = next((r for r in prior.interims if r[0] == p), None)
            # H1 누계가 아니라 실제 Q2 금액을 쓰는 경우에는 H1/Q3 원천 확인일 중
            # 가장 늦은 보고서의 확인일을 참조한다.
            if p == "H1":
                amount_rec = rec
            else:
                amount_rec = rec
            comps.append(DpsComponent(
                f"{F.year}년 {label} 보완", "interim", F.year, amount,
                amount_rec[2] if amount_rec else F.fy_confirmed,
                amount_rec[3] if amount_rec else F.fy_ref))

        comps.append(DpsComponent(f"{F.year}년 기말배당 보완", "final", F.year,
                                  prior_final, F.fy_confirmed, F.fy_ref))
        value = current_latest_value + sum(v for _, v in prior_fill) + prior_final
        flags.append("prior_year_unconfirmed_periods_filled")

    else:
        # 반기/중간배당 기업: 올해 확정 중간배당 + 전년도 기말배당.
        current_cum = current_rec[1]
        comps.append(DpsComponent(f"{O.year}년 중간배당", "interim", O.year,
                                  current_cum, current_rec[2], current_rec[3]))
        comps.append(DpsComponent(f"{F.year}년 기말배당 보완", "final", F.year,
                                  final_f, F.fy_confirmed, F.fy_ref))
        value = current_cum + final_f

    return ExpectedDps(value=value, components=comps, latest_fy=F.year,
                       flags=sorted(set(flags)))

# ---------------------------------------------------------------------------
# 수익률·배수
# ---------------------------------------------------------------------------
def dividend_yield(dps: Optional[float], price: Optional[float]) -> Optional[float]:
    """% 단위. 주가가 0 이하이거나 값이 없으면 None."""
    if dps is None or price is None or price <= 0 or dps < 0:
        return None
    return dps / price * 100.0


def us10y_multiple(div_yield: Optional[float], us10y: Optional[float],
                   min_us10y: float = 0.0) -> Optional[float]:
    if div_yield is None or us10y is None or us10y <= min_us10y:
        return None
    return div_yield / us10y


def us10y_asof(t: date, us_dates: Sequence[date], us_values: Sequence[float]) -> tuple[Optional[float], Optional[date]]:
    """t보다 **앞선** 날짜 중 가장 최근 DGS10. (한국 장 마감 시점엔 미국 당일 값이 없다)"""
    i = bisect.bisect_left(us_dates, t) - 1
    if i < 0:
        return None, None
    return us_values[i], us_dates[i]


# ---------------------------------------------------------------------------
# 역사적 시계열
# ---------------------------------------------------------------------------
@dataclass
class DailyPoint:
    d: date
    price: float              # 수정주가(현재 기준)
    dps: Optional[float]
    div_yield: Optional[float]
    us10y: Optional[float]
    multiple: Optional[float]


def daily_series(dates: Sequence[date], adj_closes: Sequence[float],
                 years: dict[int, FiscalYear],
                 us_dates: Sequence[date], us_values: Sequence[float],
                 min_us10y: float = 0.0, mode: str = "annual") -> list[DailyPoint]:
    """각 거래일에 당시 알 수 있었던 DPS만으로 배당수익률·배수를 계산한다.

    예상 DPS는 확정일에만 바뀌는 계단 함수이므로 변경 시점마다 한 번씩만 계산한다.
    """
    change_points = sorted({fy.fy_confirmed for fy in years.values() if fy.fy_confirmed}
                           | {c for fy in years.values() for (_, _, c, _) in fy.interims})
    step_values: list[Optional[float]] = [expected_dps_asof(cp, years, mode) for cp in change_points]
    step_values = [e.value if e else None for e in step_values]

    out: list[DailyPoint] = []
    for d, px in zip(dates, adj_closes):
        i = bisect.bisect_right(change_points, d) - 1
        dps = step_values[i] if i >= 0 else None
        y = dividend_yield(dps, px)
        u, _ = us10y_asof(d, us_dates, us_values)
        out.append(DailyPoint(d, px, dps, y, u, us10y_multiple(y, u, min_us10y)))
    return out


# ---------------------------------------------------------------------------
# 백분위
# ---------------------------------------------------------------------------
def quantile(sorted_vals: Sequence[float], q: float) -> float:
    """선형보간 분위수 (numpy 'linear'와 동일). q ∈ [0,1]."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def percentile_rank(values: Sequence[float], current: float) -> float:
    """current가 values 분포에서 몇 %에 있는지. 같은 값은 절반만 센다."""
    below = sum(1 for v in values if v < current)
    equal = sum(1 for v in values if v == current)
    return (below + 0.5 * equal) / len(values) * 100.0


def history_stats(values: Sequence[Optional[float]], current: Optional[float]) -> Optional[dict]:
    vals = sorted(v for v in values if v is not None and math.isfinite(v))
    if len(vals) < 20 or current is None:
        return None
    return {
        "n": len(vals),
        "p10": quantile(vals, 0.10), "p25": quantile(vals, 0.25), "p50": quantile(vals, 0.50),
        "p75": quantile(vals, 0.75), "p90": quantile(vals, 0.90),
        "min": vals[0], "max": vals[-1],
        "current": current,
        "percentile": percentile_rank(vals, current),
    }


# ---------------------------------------------------------------------------
# 배당 지속성
# ---------------------------------------------------------------------------
def cagr(start: Optional[float], end: Optional[float], years: int) -> Optional[float]:
    if start is None or end is None or start <= 0 or end <= 0 or years <= 0:
        return None
    return ((end / start) ** (1.0 / years) - 1.0) * 100.0


def persistence(years: dict[int, FiscalYear], asof: date, current_dps: Optional[float]) -> Optional[dict]:
    """asof까지 확정된 연간 DPS로 최근 10년 배당 지속성을 계산 (현재 기준 DPS)."""
    annual = {fy.year: fy.fy_total for fy in years.values()
              if fy.fy_total is not None and fy.fy_confirmed and fy.fy_confirmed <= asof}
    if not annual:
        return None
    F = max(annual)
    win10 = list(range(F - 9, F + 1))
    win5 = list(range(F - 4, F + 1))
    paid10 = sum(1 for y in win10 if (annual.get(y) or 0) > 0)
    paid5 = sum(1 for y in win5 if (annual.get(y) or 0) > 0)
    known10 = sum(1 for y in win10 if y in annual)
    known5 = sum(1 for y in win5 if y in annual)
    suspensions = sum(1 for y in win10
                      if y in annual and (y - 1) in annual and annual[y - 1] > 0 and annual[y] == 0)
    declines = [annual[y] / annual[y - 1] - 1.0 for y in win10
                if y in annual and (y - 1) in annual and annual[y - 1] > 0]
    max_decline = min(declines) * 100.0 if declines else None
    if max_decline is not None and max_decline > 0:
        max_decline = 0.0
    base10 = annual.get(F - 10)
    return {
        "latest_fy": F,
        "paid10": paid10, "known10": known10,
        "paid5": paid5, "known5": known5,
        "suspensions": suspensions,
        "cagr10": cagr(base10, annual.get(F), 10),
        "cagr5": cagr(annual.get(F - 5), annual.get(F), 5),
        "max_decline": max_decline,
        "vs10y": (current_dps / base10) if (current_dps is not None and base10 and base10 > 0) else None,
        "dps10y_ago": base10,
    }


def annual_breakdown(years: dict[int, FiscalYear], asof: date) -> list[dict]:
    """상세 페이지 연도별 막대그래프용: 사업연도별 중간·분기 / 기말 / 합계 (현재 기준)."""
    rows = []
    for y in sorted(years):
        fy = years[y]
        if fy.fy_total is None or not fy.fy_confirmed or fy.fy_confirmed > asof:
            continue
        rows.append({
            "year": y,
            "interim": fy.interim_total(),
            "final": fy.final_dps,
            "total": fy.fy_total,
            "quarterly": fy.is_quarterly(),
            "confirmed": fy.fy_confirmed.isoformat(),
            "flags": fy.flags,
        })
    return rows


# ---------------------------------------------------------------------------
# 권리락(주식 수 변동) 탐지
# ---------------------------------------------------------------------------
def detect_corp_actions(dates: Sequence[date], closes: Sequence[float], change_pcts: Sequence[float],
                        threshold: float) -> list[tuple[date, float, float, float]]:
    """KRX 등락률은 '기준가' 대비이다. 기준가 = 종가 / (1 + 등락률)
    분할·병합·무상증자 등 권리락일에는 기준가가 전일 종가와 달라진다.
    ratio = 전일 종가 / 당일 기준가  (예: 50:1 분할 → 약 50)

    현금배당은 한국에서 기준가를 조정하지 않으므로 여기서 잡히지 않는다.
    반환: (date, ratio, prev_close, base_price)
    """
    out = []
    prev = None
    for d, c, r in zip(dates, closes, change_pcts):
        if c is None or c <= 0 or r is None or (isinstance(r, float) and math.isnan(r)):
            continue
        if prev is not None:
            base = c / (1.0 + r / 100.0)
            if base > 0:
                ratio = prev / base
                if abs(ratio - 1.0) > threshold:
                    out.append((d, ratio, prev, base))
        prev = c
    return out


def round_ratio(ratio: float) -> float:
    """50.02 → 50, 0.1001 → 0.1 처럼 흔한 분할 비율에 가까우면 스냅. 아니면 그대로(무상증자 등)."""
    for cand in (ratio, 1.0 / ratio):
        k = round(cand)
        if k >= 2 and abs(cand - k) / k < 0.01:
            return float(k) if cand == ratio else 1.0 / k
    return ratio

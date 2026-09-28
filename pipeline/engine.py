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

PERIOD_ORDER = {"Q1": 1, "PROV_Q1": 1.5, "H1": 2, "PROV_H1": 2.5, "Q3": 3, "PROV_Q3": 3.5, "FY": 4}
# PROV_*: 정기보고서가 아직 반영하지 않은 '현금·현물배당결정' 수시공시 잠정값. 그 배당이 속한 기간
# 바로 뒤에 놓아, 같은 기간 정기보고서(아직 반영 전)보다는 앞서고 그다음 기간 정기보고서에는 밀린다.
# fy.interims에만 들어가고 FY와는 섞이지 않는다.


def is_provisional_period(period: str) -> bool:
    return period.startswith("PROV")


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
    kind: str                 # "final" | "interim" | "provisional" | "annual"
    fiscal_year: int
    dps: float                # 현재 기준
    confirmed_date: date
    ref: str = ""
    # kind="provisional": 정기보고서가 아직 안 나와 '현금·현물배당결정' 수시공시로만 확인된 값.
    # 정기보고서가 나오면 그 값이 우선하며 이 줄은 사라진다.


@dataclass
class ExpectedDps:
    value: float
    components: list[DpsComponent]
    latest_fy: int            # 연간 DPS가 확정된 가장 최근 사업연도 (F)
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
        # 기간분/누적 판정은 정기보고서끼리만 한다. 수시공시 잠정값(PROV_*)은 이미 누적으로 만들어
        # 들어오고, 반영이 늦는 정기보고서보다 클 수 있어 섞어 비교하면 잘못 판정한다.
        regular = [x for x in fy.interims if not is_provisional_period(x[0])]
        vals = [x[1] for x in regular]
        if any(vals[i] + 1e-6 < vals[i - 1] for i in range(1, len(vals))):
            running, fixed = 0.0, []
            for p, v, c, ref in regular:
                running += v
                fixed.append((p, running, c, ref))
            fy.interims = sorted(fixed + [x for x in fy.interims if is_provisional_period(x[0])],
                                 key=lambda x: PERIOD_ORDER[x[0]])
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
        prev = max(prev, cumulative)
    return out


def _cum_at(fy: Optional[FiscalYear], period: str) -> float:
    """fy 사업연도의 period 시점까지 누적 중간·분기배당 (그 기간 보고서가 없으면 직전 기간 값)."""
    if fy is None:
        return 0.0
    best = 0.0
    for p, v, _, _ in fy.interims:
        if PERIOD_ORDER[p] <= PERIOD_ORDER[period]:
            best = max(best, v)
    return best


_PERIOD_LABEL = {"Q1": "1분기", "H1": "반기", "Q3": "3분기",
                 "PROV_Q1": "1분기 배당 결정", "PROV_H1": "반기 배당 결정", "PROV_Q3": "3분기 배당 결정"}


def _interim_components(fy: FiscalYear, *, upto: Optional[str] = None, after: Optional[str] = None,
                        suffix: str = "") -> list[DpsComponent]:
    """fy에 신고된 1분기·반기·3분기 보고서(및 아직 정기보고서로 확정 안 된 PROV)를 각 기간
    '증분'(그 기간에만 해당하는 배당)으로 쪼갠다.

    사업연도 하나의 중간·분기배당을 한 줄로 뭉치지 않고, 실제로 확정된 보고서 종류(1분기/반기/3분기)
    마다 자기 접수일·접수번호를 가진 줄로 나눠 보여준다. 증분이 0원인 기간(그 분기엔 새로 확정된
    배당이 없었던 경우)은 표시하지 않는다. PROV(수시공시로만 확인된 값)는 kind="provisional"로
    표시해 정기보고서 확정분과 구분한다.
    upto: 이 기간까지만 포함(그 이후는 제외). after: 이 기간을 넘는(그 이후) 것만 포함.
    """
    out: list[DpsComponent] = []
    prev = 0.0
    for period, cum, confirmed, ref in sorted(fy.interims, key=lambda x: PERIOD_ORDER[x[0]]):
        amt, prev = max(cum - prev, 0.0), max(prev, cum)
        if upto is not None and PERIOD_ORDER[period] > PERIOD_ORDER[upto]:
            continue
        if after is not None and PERIOD_ORDER[period] <= PERIOD_ORDER[after]:
            continue
        if amt <= 0:
            continue
        prov = is_provisional_period(period)
        kind = "provisional" if prov else "interim"
        prov_suffix = " (수시공시, 정기보고서 확정 전)" if prov else suffix
        out.append(DpsComponent(f"{fy.year}년 {_PERIOD_LABEL[period]}{prov_suffix}", kind, fy.year,
                                amt, confirmed, ref))
    return out


def expected_dps_asof(t: date, years: dict[int, FiscalYear], mode: str = "substitute") -> Optional[ExpectedDps]:
    """날짜 t에 알 수 있었던 정보만으로 계산한 예상 연간 DPS (현재 기준).

    F = t까지 연간 DPS(사업보고서)가 확정된 가장 최근 사업연도 (= '전년도')
    O = F + 1 (진행 중 사업연도 = '현재 연도')
    p = O에서 t까지 확정된 가장 늦은 보고 기간 (Q1 / H1 / Q3)

    예상 DPS = O의 p까지 확정 누계 (1분기·반기·3분기 중 실제 확정된 보고서마다 한 줄씩)
             + F의 p 이후 중간·분기배당 (아직 확정 안 된 O의 같은 기간을 전년도 값으로 대체,
               역시 어떤 분기·반기 보고서에서 왔는지 각각 표시)
             + F의 기말배당

    예) 전년도 Q1·Q2·Q3·기말 = 300씩
        올해 Q1만 확정(400)   → 400 + 300 + 300 + 300 = 1,300
        올해 H1까지 확정(800) → 800 + 300 + 300 = 1,400
        올해 Q3까지 확정      → 올해 Q1~Q3 누계 + 전년도 기말
        올해 확정분이 없음    → 전년도 연간 DPS (중간+기말)
        사업보고서 제출 시    → F가 올해로 바뀌어 올해 실제 연간 DPS
    mode는 이전 호출부 호환용 인자이며 계산에 쓰지 않는다.
    """
    confirmed = [fy for fy in years.values()
                 if fy.fy_total is not None and fy.fy_confirmed is not None and fy.fy_confirmed <= t]
    if not confirmed:
        return None
    F = max(confirmed, key=lambda fy: fy.year)
    flags: list[str] = list(F.flags)
    final = F.final_dps or 0.0
    comps: list[DpsComponent] = []

    O = years.get(F.year + 1)
    cum_o, rec = (O.interim_cum_asof(t) if O else (0.0, None))

    if rec is not None and cum_o > 0:
        p = rec[0]
        fill = max(F.interim_total() - _cum_at(F, p), 0.0)
        comps.extend(_interim_components(O, upto=p))
        if fill > 0:
            comps.extend(_interim_components(F, after=p, suffix=" (미확정분 대체)"))
            flags.append("prior_year_unconfirmed_periods_filled")
        comps.append(DpsComponent(f"{F.year}년 기말배당", "final", F.year, final, F.fy_confirmed, F.fy_ref))
        flags += list(O.flags)
        if is_provisional_period(p):
            flags.append("provisional_dividend_used")
        value = cum_o + fill + final
    else:
        if F.has_interim():
            comps.extend(_interim_components(F))
        comps.append(DpsComponent(f"{F.year}년 기말배당", "final", F.year, final, F.fy_confirmed, F.fy_ref))
        value = F.fy_total or 0.0
    return ExpectedDps(value=value, components=comps, latest_fy=F.year, flags=sorted(set(flags)))


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


BOMB_BASELINE_YEARS = 3     # 폭탄 기준선(최근 N년 중앙값)에 쓰는 과거 연도 수
BOMB_BASELINE_MIN_YEARS = 2  # 기준선 계산에 필요한 최소 확정 연도 수


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def dividend_tags(years: dict[int, FiscalYear], asof: date, exp: Optional[ExpectedDps]) -> dict:
    """메인 목록 태그.

    crown(왕관): asof까지 확정된 최근 10개 사업연도 데이터가 전부 있고(연속 10년), 그 10년 모두
                배당을 지급했으며(0원인 해 없음), 10년 안에서 전년 대비 감소가 한 번도 없었던 경우.
    bomb(폭탄):  매년 비슷한 금액을 배당하는 기업과, 어느 한 해 1회성으로 크게 배당해 배당수익률이
                왜곡된 기업을 구분하기 위한 태그. '올해'(asof가 속한 연도) 자체의 새 배당 정보(중간·
                분기·수시공시)가 있어야 하고, 올해 예상(잠정 포함) DPS(exp.value)가 최근 BOMB_BASELINE_YEARS개
                확정 사업연도 DPS의 중앙값(그 회사의 '평소' 수준, 최소 BOMB_BASELINE_MIN_YEARS개 필요)보다
                50% 이상 높은 경우. 직전 1년치만 보지 않고 여러 해의 중앙값과 비교해, 마침 전년도 자체가
                낮았거나(휴배당 등) 높았던(마찬가지로 1회성) 경우에 잘못 판정하지 않는다.
    """
    annual = {fy.year: fy.fy_total for fy in years.values()
              if fy.fy_total is not None and fy.fy_confirmed and fy.fy_confirmed <= asof}
    crown = False
    if annual:
        F = max(annual)
        win10 = list(range(F - 9, F + 1))
        if all(y in annual and (annual[y] or 0) > 0 for y in win10):
            crown = all(annual[win10[i]] >= annual[win10[i - 1]] for i in range(1, len(win10)))

    bomb = False
    cur = years.get(asof.year)
    has_this_year_data = cur is not None and cur.interim_cum_asof(asof)[0] > 0
    if exp is not None and has_this_year_data:
        base_years = [annual[y] for y in range(asof.year - BOMB_BASELINE_YEARS, asof.year) if y in annual]
        if len(base_years) >= BOMB_BASELINE_MIN_YEARS:
            baseline = _median(base_years)
            if baseline > 0:
                bomb = (exp.value / baseline - 1.0) >= 0.50

    return {"crown": crown, "bomb": bomb}


def annual_breakdown(years: dict[int, FiscalYear], asof: date) -> list[dict]:
    """상세 페이지 연도별 막대그래프용: 사업연도별 중간·분기 / 기말 / 합계 (현재 기준).

    asof가 속한 사업연도의 사업보고서가 아직 안 나왔다면(그래서 위 확정 목록에 없다면),
    그때까지 확정된 중간·분기배당 및 '현금·현물배당결정' 수시공시(PROV) 누계만으로
    마지막에 provisional=True 행을 덧붙인다(기말배당은 아직 모르므로 0). 사업보고서가
    나오면 그 해는 위 확정 목록에 들어가 이 잠정 행은 자연히 사라진다.
    """
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
            "provisional": False,
            "expected": None,
        })

    cur = years.get(asof.year)
    if cur is not None and not any(r["year"] == asof.year for r in rows):
        cum, rec = cur.interim_cum_asof(asof)
        if cum > 0:
            rows.append({
                "year": asof.year,
                "interim": cum,
                "final": 0.0,
                "total": cum,
                "quarterly": any(p.replace("PROV_", "") in ("Q1", "Q3") for p, v, c, _ in cur.interims if v > 0 and c <= asof),
                "confirmed": rec[2].isoformat() if rec else None,
                "flags": cur.flags,
                "provisional": True,
                "expected": None,
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

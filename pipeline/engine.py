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
        """t까지 알려진 가장 큰 누계와 그 기록. 누계는 해가 가며 줄지 않으므로, 반영이 늦은 정기보고서
        (0으로 남은 반기보고서 등)보다 더 많이 알려 주는 기록을 최신으로 본다. 같으면 더 늦은 기간."""
        best = None
        for rec in self.interims:
            if rec[2] <= t and (best is None or (rec[1], PERIOD_ORDER[rec[0]]) > (best[1], PERIOD_ORDER[best[0]])):
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


def _raw_increments(fy: Optional[FiscalYear], t: Optional[date] = None) -> list[tuple[str, float, date, str]]:
    """보고서(기간)별 누계 증분. 누계가 늘어난 기록만, 기간 순."""
    if fy is None:
        return []
    recs = sorted((r for r in fy.interims if t is None or r[2] <= t), key=lambda r: PERIOD_ORDER[r[0]])
    out, prev = [], 0.0
    for p, cum, confirmed, ref in recs:
        if cum > prev + 1e-6:
            out.append((p, cum - prev, confirmed, ref))
            prev = cum
    return out


def dividend_events(fy: Optional[FiscalYear], t: Optional[date] = None) -> list[tuple[str, float, date, str]]:
    """사업연도의 중간·분기 배당을 '배당 1회' 단위로 나눈 목록(기간 순, t까지 확정분).

    보고서 누계의 증분이 곧 배당 1회다. 단, 앞 기간 보고서에 아무 증분이 없는데 다음 보고서에 두 번이
    묶여 실리는 경우가 있다(1분기 배당이 반기보고서에 함께 실림 — 122900 2021년: 1분기 0 → 반기 300
    = 150 × 2 / 정정으로 반기보고서 접수일이 늦어져 3분기 증분이 두 분기분 — KB금융 2024년).
    그 해 가장 작은 1회분의 정수배(2배 이상)이고, 바로 앞 기간들이 비어 있어 묶였을 수 있는 만큼만
    나눈다. 앞 기간에 이미 배당이 있으면 큰 증분은 한 번의 큰 배당으로 본다(SNT모티브 2025년 반기 600)."""
    raw = _raw_increments(fy, t)
    if not raw:
        return []
    pos = {"Q1": 1, "H1": 2, "Q3": 3}
    paid = {pos[p.replace("PROV_", "")] for p, *_ in raw}
    unit = min(a for _, a, _, _ in raw)
    out = []
    for p, a, confirmed, ref in raw:
        here = pos[p.replace("PROV_", "")]
        room = 1
        while here - room >= 1 and here - room not in paid:
            room += 1
        k = round(a / unit)
        split = 2 <= k <= room and abs(a / unit - k) < 0.15
        out += [(p, a / k, confirmed, ref)] * k if split else [(p, a, confirmed, ref)]
    return out


def _event_components(fy: FiscalYear, events: list[tuple[str, float, date, str]], suffix: str = "") -> list[DpsComponent]:
    """배당 1회 단위 목록을 화면 표시용으로 보고서(기간·접수번호)별 한 줄로 묶는다."""
    out: list[DpsComponent] = []
    for p, a, confirmed, ref in events:
        if out and out[-1].ref == ref and out[-1].confirmed_date == confirmed and out[-1].fiscal_year == fy.year \
                and out[-1].label.startswith(f"{fy.year}년 {_PERIOD_LABEL[p]}"):
            out[-1] = DpsComponent(out[-1].label, out[-1].kind, fy.year, out[-1].dps + a, confirmed, ref)
            continue
        prov = is_provisional_period(p)
        label = f"{fy.year}년 {_PERIOD_LABEL[p]}" + (" (수시공시, 정기보고서 확정 전)" if prov else suffix)
        out.append(DpsComponent(label, "provisional" if prov else "interim", fy.year, a, confirmed, ref))
    return out


def expected_dps_asof(t: date, years: dict[int, FiscalYear], mode: str = "substitute") -> Optional[ExpectedDps]:
    """날짜 t에 알 수 있었던 정보만으로 계산한 예상 연간 DPS (현재 기준).

    F = t까지 연간 DPS(사업보고서)가 확정된 가장 최근 사업연도 (= '전년도')
    O = F + 1 (진행 중 사업연도 = '올해')

    예상 DPS = 올해 지금까지 확정된 중간·분기 배당 (정기보고서 + 수시공시 잠정치)
             + 전년도 중간·분기 배당 중 '아직 올해 대응분이 안 나온' 나머지 (횟수로 맞춘다)
             + 전년도 기말배당

    '나머지'는 보고 기간이 아니라 배당 횟수로 맞춘다. 올해 k번 받았으면 전년도 (k+1)번째부터만
    대체한다. 2024년 배당절차 개선 이후 결정 뒤 기준일을 잡는 회사는 같은 배당이 한 보고서 앞당겨
    실려서(현대엘리베이터 017800: 2025년엔 반기·3분기, 2026년엔 1분기·반기에 각 1,000원) 기간으로
    맞추면 이미 받은 배당을 또 더한다(1,000 + 1,000 + 전년도 3분기 1,000 + 기말 12,010 = 15,010).
    횟수로 맞추면 2,000 + 기말 12,010 = 14,010.

    2017~2025년 조회 종목 전체의 실제 연간 DPS로 되짚어 본 결과(분기·반기보고서가 나온 시점마다
    2,327건), 기간 기준보다 정확히 맞힌 건이 많고(625 vs 620) 20% 넘게 과대추정한 건이 적었다
    (298 vs 301). 두 방식이 다른 22건의 오차 합은 1.60 vs 7.04. 틀리는 쪽은 올해 배당 횟수를 늘린
    회사(분기배당 도입)로, 이때는 과소추정이 된다.

    예) 전년도 Q1·Q2·Q3·기말 = 300씩
        올해 1회(400)         → 400 + 300 + 300 + 300 = 1,300
        올해 2회(누계 800)    → 800 + 300 + 300 = 1,400
        올해 확정분이 없음    → 전년도 연간 DPS (중간+기말)
        사업보고서 제출 시    → F가 올해로 바뀌어 올해 실제 연간 DPS
    전년도 중간·분기 누계가 연간 DPS보다 큰(원 데이터가 맞지 않는) 해는 나머지를 대체하지 않는다.
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
    o_events = dividend_events(O, t)
    if o_events and not F.interims and F.year == min(years):
        # 데이터 첫해에 분기·반기 보고서가 없으면(DART에 2015년 분기 배당 자료가 없음) 연간 DPS를 중간·기말로
        # 나눌 수 없다. 그대로 기말로 보고 올해 중간배당을 더하면 같은 배당을 두 번 센다(현대차 2016년 8월:
        # 1,000 + 4,000 = 5,000, 실제 연간 4,000). 전년도 연간 DPS 중 올해 이미 받은 만큼을 뺀 나머지만 더한다.
        # 첫해가 아니면 분기 보고서가 없다는 건 중간배당이 없던 해라는 뜻이라 기존 방식대로 더한다.
        cum_o = O.interim_cum_asof(t)[0]
        rest_annual = max((F.fy_total or 0.0) - cum_o, 0.0)
        comps.extend(_event_components(O, _raw_increments(O, t)))
        comps.append(DpsComponent(f"{F.year}년 연간배당 중 나머지 (분기 자료 없음)", "final", F.year,
                                  rest_annual, F.fy_confirmed, F.fy_ref))
        flags += list(O.flags) + ["prior_year_interims_unknown"]
        if any(is_provisional_period(p) for p, *_ in o_events):
            flags.append("provisional_dividend_used")
        value = cum_o + rest_annual
    elif o_events:
        cum_o = O.interim_cum_asof(t)[0]
        f_events = [] if "interim_exceeds_annual" in F.flags else dividend_events(F)
        rest = f_events[len(o_events):]
        comps.extend(_event_components(O, _raw_increments(O, t)))
        if rest:
            comps.extend(_event_components(F, rest, suffix=" (미확정분 대체)"))
            flags.append("prior_year_unconfirmed_periods_filled")
        comps.append(DpsComponent(f"{F.year}년 기말배당", "final", F.year, final, F.fy_confirmed, F.fy_ref))
        flags += list(O.flags)
        if any(is_provisional_period(p) for p, *_ in o_events):
            flags.append("provisional_dividend_used")
        value = cum_o + sum(a for _, a, _, _ in rest) + final
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


BOMB_RATIO = 0.50  # 최근 결산년도 DPS가 그 전년도보다 이 비율 이상 늘면 폭탄


def dividend_tags(years: dict[int, FiscalYear], asof: date) -> dict:
    """메인 목록 태그. asof까지 사업보고서로 확정된 연간 DPS만 쓴다(분기·잠정치는 쓰지 않는다).

    crown(왕관): 확정된 최근 10개 사업연도 데이터가 전부 있고(연속 10년), 그 10년 모두
                배당을 지급했으며(0원인 해 없음), 10년 안에서 전년 대비 감소가 한 번도 없었던 경우.
    bomb(폭탄):  가장 최근 확정 결산년도 DPS가 그 전년도 확정 DPS보다 BOMB_RATIO 이상 늘어난 경우.
                1회성으로 크게 배당해 배당수익률이 부풀려졌을 수 있다는 표시다. 전년도가 없거나 0원
                (무배당→배당 재개)이면 비율을 계산할 수 없어 붙이지 않는다.
    """
    annual = {fy.year: fy.fy_total for fy in years.values()
              if fy.fy_total is not None and fy.fy_confirmed and fy.fy_confirmed <= asof}
    crown = bomb = False
    if annual:
        F = max(annual)
        win10 = list(range(F - 9, F + 1))
        if all(y in annual and (annual[y] or 0) > 0 for y in win10):
            crown = all(annual[win10[i]] >= annual[win10[i - 1]] for i in range(1, len(win10)))
        prev = annual.get(F - 1)
        if prev:
            bomb = annual[F] / prev - 1.0 >= BOMB_RATIO

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

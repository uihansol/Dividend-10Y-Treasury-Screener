"""종목별 캐시 + 분석.

data/cache/stocks/{code}/
  metadata.json   코드·이름·시장, price_through, dividend_through, updated_at, 오류, 요약 지표
  prices.csv      date, close(원주가), change_pct(KRX 기준가 대비 %), trading_value
  dividends.json  {"log": {정기보고서 조회기록}, "reports": [...],
                  "announcements": {수시공시(현금·현물배당결정) 조회기록}}
  analysis.json   화면용 계산 결과 (기존 상세 페이지 JSON과 같은 형식)
data/cache/index.json   조회한 종목 요약 목록

analyze_stock(code)
  캐시 없음 → 2016-01-01부터 가격 + 2015년부터 배당 최초 수집
  캐시 있음 → 마지막 날짜 이후 가격, 아직 안 받은 보고서만 조회
  cooldown(기본 10분) 안에 다시 요청 → 네트워크 없이 캐시 재계산만
  단계(prices → dividends → us10y → compute)를 한 번에 또는 따로 실행할 수 있다
  KRX/DART 실패 → 기존 캐시로 계산하고 오류를 metadata에 기록, 캐시도 없으면 DataUnavailable
"""
from __future__ import annotations

import fcntl
import math
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Callable

import pandas as pd

from . import config as C
from .engine import (CorpAction, DividendReport, adjusted_prices, annual_breakdown, build_fiscal_years,
                     daily_series, detect_corp_actions, dividend_tags, dividend_yield, expected_dps_asof,
                     history_stats, persistence, round_ratio, us10y_asof, us10y_multiple)
from .store import KST, now_kst, read_json, write_json
from .krx import PRICE_COLS


class StockNotFound(KeyError):
    pass


class DataUnavailable(RuntimeError):
    pass


def stock_dir(code: str):
    return C.CACHE_DIR / code


def _r(x, n=4):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), n)


@contextmanager
def _lock(code: str):
    """같은 프로세스·머신에서 같은 종목 동시 처리 방지 (Actions 쪽은 워크플로 concurrency로 방지)."""
    d = stock_dir(code)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


# ---------------------------------------------------------------- 가격 캐시
def load_prices(code: str) -> pd.DataFrame:
    p = stock_dir(code) / "prices.csv"
    if not p.exists():
        return pd.DataFrame(columns=["date", "close", "change_pct", "trading_value"])
    return pd.read_csv(p, dtype={"date": str})


def update_stock_price_cache(code: str, today: date | None = None,
                             fetch: Callable | None = None, quick: bool = False) -> tuple[pd.DataFrame, dict]:
    """가격 캐시를 최신 데이터 우선으로 증분 갱신한다.

    기존 이력이 있으면 최근 7일을 먼저 조회해 최신 가격을 최대한 빨리 확보한다.
    그 다음 마지막 저장일과 최근 7일 사이에 생긴 과거 누락 구간만 보완한다.
    따라서 새로고침 시 최신 날짜가 먼저 캐시에 반영되고, 장기간 미접속한 종목도
    마지막 저장일 이후의 거래일 데이터가 빠지지 않는다.
    """
    if fetch is None:
        from .krx import fetch_stock_prices as fetch
    today = today or datetime.now(KST).date()
    old = load_prices(code)
    empty = pd.DataFrame(columns=PRICE_COLS)

    if len(old):
        mode = "incremental"
        latest = date.fromisoformat(old["date"].max())

        # ① 최신 7일을 먼저 조회한다.
        # 이미 저장된 날짜도 다시 받아 당일/최근 종가·거래대금을 갱신한다.
        recent_start = max(date.fromisoformat(C.PRICE_START), today - timedelta(days=7))
        recent = fetch(code, recent_start.isoformat(), today.isoformat()) if recent_start <= today else empty

        # ② 최신 구간을 확보한 뒤, 그보다 과거에 생긴 누락분만 보완한다.
        # 최근 7일과 겹치지 않게 범위를 잘라 중복 KRX 호출을 줄인다.
        gap_end = recent_start - timedelta(days=1)
        gap_start = latest + timedelta(days=1)
        gap = (
            fetch(code, gap_start.isoformat(), gap_end.isoformat())
            if gap_start <= gap_end and gap_start <= today
            else empty
        )

        pieces_new = [x for x in (recent, gap) if len(x)]
        new = pd.concat(pieces_new, ignore_index=True) if pieces_new else empty
        from_date = min(
            recent_start.isoformat(),
            gap_start.isoformat() if len(gap) else recent_start.isoformat(),
        )
    else:
        mode = "initial"
        # 최초 조회는 최근 데이터만 먼저 받아 화면을 빠르게 만든다.
        # 전체 과거 이력은 backfill_stock_price_cache()가 뒤에서 순차 보완한다.
        start = max(date.fromisoformat(C.PRICE_START), today - timedelta(days=90)) if quick else date.fromisoformat(C.PRICE_START)
        new = fetch(code, start.isoformat(), today.isoformat()) if start <= today else empty
        from_date = start.isoformat()

    # 기존 과거 데이터 + 최신 갱신 구간 + 과거 누락 구간을 합친다.
    # 같은 날짜는 최신 조회 결과를 우선해 교체한다.
    pieces = [x for x in (old, new) if len(x)]
    df = pd.concat(pieces, ignore_index=True) if pieces else old
    df = df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    if len(new):
        stock_dir(code).mkdir(parents=True, exist_ok=True)
        df.to_csv(stock_dir(code) / "prices.csv", index=False)

    return df, {
        "mode": mode,
        "added": int(len(new)),
        "from": from_date,
        "recent_first": True,
        "recent_days": 7,
        "quick_initial": bool(quick and mode == "initial"),
        "gap_filled": bool(len(gap)) if len(old) else False,
    }

# ---------------------------------------------------------------- 과거 가격 백필
def backfill_stock_price_cache(code: str, today: date | None = None,
                               fetch: Callable | None = None, chunk_days: int = 365) -> dict:
    """최근 데이터가 먼저 표시된 뒤, 가장 가까운 과거 구간부터 한 청크씩 보완한다.

    한 번에 2015년까지 모두 받지 않고 최근 캐시의 바로 앞 구간부터 최대 chunk_days만
    조회한다. 호출할 때마다 prices.csv를 저장하므로 UI가 중간 결과를 읽을 수 있다.
    """
    if fetch is None:
        from .krx import fetch_stock_prices as fetch
    today = today or datetime.now(KST).date()
    old = load_prices(code)
    if old.empty:
        return {"done": False, "added": 0, "reason": "no_cache"}

    earliest = date.fromisoformat(old["date"].min())
    floor = date.fromisoformat(C.PRICE_START)
    # PRICE_START(1월 1일)는 휴장일이라 첫 거래일은 그보다 늦다. 첫 주 안이면 이미 끝까지 받은 것으로 본다
    # (그렇지 않으면 완료된 종목도 매번 1일치 KRX 조회를 다시 한다).
    if earliest <= floor + timedelta(days=7):
        return {"done": True, "added": 0, "from": floor.isoformat(),
                "to": (earliest - timedelta(days=1)).isoformat()}

    end = earliest - timedelta(days=1)
    start = max(floor, end - timedelta(days=chunk_days - 1))
    if start <= floor + timedelta(days=7):   # 마지막 청크는 항상 floor까지 조회해 첫 거래일을 빠뜨리지 않는다
        start = floor
    new = fetch(code, start.isoformat(), end.isoformat()) if start <= end else pd.DataFrame(columns=PRICE_COLS)
    if len(new):
        df = pd.concat([old, new], ignore_index=True)
        df = df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
        stock_dir(code).mkdir(parents=True, exist_ok=True)
        df.to_csv(stock_dir(code) / "prices.csv", index=False)
    # 1년 구간 전체에 거래가 없으면 그 이전 상장 이력이 없는 것(2015년 이후 상장)으로 보고 이번 실행을 끝낸다.
    # 저장하는 상태가 아니므로 KRX 일시 오류로 빈 결과가 와도 다음 새로고침 때 다시 확인한다.
    return {
        "done": start == floor or len(new) == 0,
        "added": int(len(new)),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "remaining_before": earliest.isoformat(),
    }


# ---------------------------------------------------------------- 배당 캐시
def load_dividends(code: str) -> dict:
    return read_json(stock_dir(code) / "dividends.json", {"log": {}, "reports": [], "announcements": {}})


def update_stock_dividend_cache(code: str, corp_code: str, today: date | None = None,
                                fetch: Callable | None = None) -> tuple[dict, dict]:
    if fetch is None:
        from .dart import fetch_stock_dividends as fetch
    today = today or datetime.now(KST).date()
    cur = load_dividends(code)
    new, log = fetch(corp_code, C.DART_FIRST_YEAR, today.year, cur["log"], today=today)
    reps = {f'{r["fiscal_year"]}-{r["period"]}': r for r in cur["reports"]}
    for r in new:
        reps[f'{r["fiscal_year"]}-{r["period"]}'] = r
    out = {**cur, "log": log,
          "reports": sorted(reps.values(), key=lambda r: (r["fiscal_year"], C.PERIOD_ORDER[r["period"]]))}
    write_json(stock_dir(code) / "dividends.json", out)
    return out, {"mode": "initial" if not cur["log"] else "incremental", "added": len(new)}


def update_stock_announcement_cache(code: str, corp_code: str, today: date | None = None,
                                    fetch: Callable | None = None) -> tuple[dict, dict]:
    """올해 '현금·현물배당결정' 수시공시를 확인해 아직 정기보고서로 확정 안 된 값을 잠정치로 채운다.
    정기보고서(update_stock_dividend_cache)보다 훨씬 가벼운 호출(공시목록 1회 + 새 공시만 원문 조회)."""
    if fetch is None:
        from .dart import fetch_dividend_announcements as fetch
    today = today or datetime.now(KST).date()
    cur = load_dividends(code)
    ann_log = dict(cur.get("announcements", {}))
    new, ann_log = fetch(corp_code, today.year, ann_log, today=today)
    out = {**cur, "announcements": ann_log}
    write_json(stock_dir(code) / "dividends.json", out)
    return out, {"mode": "check", "added": len(new)}


def to_reports(div: dict) -> list[DividendReport]:
    out = []
    for r in div["reports"]:
        if not r.get("confirmed_date"):
            continue
        m, d = {"Q1": (3, 31), "H1": (6, 30), "Q3": (9, 30), "FY": (12, 31)}[r["period"]]
        basis = date.fromisoformat(r["basis_date"][:10]) if r.get("basis_date") else date(int(r["fiscal_year"]), m, d)
        out.append(DividendReport(int(r["fiscal_year"]), r["period"], float(r["cum_dps"]), basis,
                                  date.fromisoformat(r["confirmed_date"]), str(r.get("rcept_no", ""))))
    out.extend(_provisional_reports(div))
    return out


_QUARTER_ENDS = ((3, 31, "Q1"), (6, 30, "H1"), (9, 30, "Q3"), (12, 31, "FY"))
_INTERIM_PERIODS = ("Q1", "H1", "Q3")
_NEXT_PERIOD = {"Q1": "H1", "H1": "Q3", "Q3": "Q3"}


def _latest_period_end(d: date, *, inclusive: bool) -> tuple[int, str]:
    """d 이전(inclusive면 d 당일 포함) 가장 최근 분기 말 → (사업연도, 기간)."""
    for y in (d.year, d.year - 1):
        for m, dd, p in reversed(_QUARTER_ENDS):
            end = date(y, m, dd)
            if end < d or (inclusive and end == d):
                return y, p
    raise ValueError(d)


def _attribute_announcement(decided: date, basis: date) -> tuple[int, str]:
    """배당결정 공시가 어느 사업연도·기간의 배당인지.

    - 1~3월에 결정된 배당은 직전 사업연도 결산(기말)배당이다. 2024년 배당절차 개선 이후엔
      기준일이 결정 뒤(2~4월)로 잡혀 기준일 연도가 다음 해가 되므로(광주신세계 037710: 결정 2/11,
      기준일 3/31) 기준일로는 판단할 수 없다.
    - 기준일을 먼저 두고 나중에 결정하는 종전 방식(기준일 ≤ 결정일)은 기준일이 속한 분기의 배당이다
      (서호전기 065710: 결정 8/12, 기준일 6/30 → 반기).
    - 결정 뒤에 기준일을 잡는 개정 방식은 결정 직전에 끝난 분기의 배당이다
      (037710: 결정 5/13·기준일 5/29 → 1분기보고서에 600원으로 실림)."""
    y, p = _latest_period_end(decided, inclusive=False)
    if p == "FY" or basis > decided:
        return y, p
    return _latest_period_end(basis, inclusive=True)


def _provisional_reports(div: dict) -> list[DividendReport]:
    """'현금·현물배당결정' 수시공시 중 정기보고서가 아직 반영하지 않은 것만 잠정 보고서(PROV_*)로 만든다.

    원 데이터(조회한 종목 전체의 공시·정기보고서)에서 확인한 규칙:
    1. 사업연도·기간은 결정일과 기준일로 정한다(_attribute_announcement). 결산배당(FY)은
       사업보고서가 확정하므로 잠정치를 만들지 않는다 — 올해 분기 잠정치로 섞이면 폭탄 태그가
       잘못 붙는다.
    2. 같은 기준일·금액의 공시는 정정·재공시이므로 하나로 본다(가장 이른 결정일 기준).
    3. 한 기간에 공시가 여럿이면, 그중 하나라도 정기보고서가 이미 반영했으면 그 기간은 정기보고서를
       믿고 모두 버린다(예: 하나금융 086790 반기 1,155원이 반영돼 있으면 같은 기간의 다른 금액은
       무시). 모두 미반영이면 가장 나중에 결정된 공시(정정분)를 쓴다.
    4. 누계는 기간 순서대로 쌓는다: 직전 기간까지의 누계(정기보고서 또는 앞 기간 잠정치) + 이번 금액.
       예전에는 공시마다 따로 계산해 같은 해 잠정치 중 목록상 마지막(=DART 목록에서 가장 오래된)
       공시 하나만 남았다.
    5. '반영됨' = 같은 사업연도, 결정일 이후 접수, 같거나 뒤 기간의 정기보고서 누계가 이 누계 이상.
    6. 결정일 이후 나온 같은 기간 정기보고서가 반영하지 않았거나(KT&G 033780·서호전기 065710:
       8월 결정분이 반기보고서엔 0), 아직 그 보고서가 없는데 작년 같은 기간 보고서엔 배당이 없고
       다음 기간 보고서에 실렸다면, 이 회사는 이런 배당을 다음 기간 보고서에 싣는 것으로 보고
       잠정치를 다음 기간(PROV_Q3 등)으로 옮긴다. 그래야 전년도 미확정분 대체가 이중 계산되지 않는다."""
    periodic = [r for r in div["reports"] if r.get("confirmed_date")]
    order = {"Q1": 1, "H1": 2, "Q3": 3, "FY": 4}

    events: dict[tuple[str, float], dict] = {}
    for a in div.get("announcements", {}).values():
        if a.get("status") != "ok" or not a.get("confirmed_date") or not a.get("amount"):
            continue
        basis = a.get("basis_date") or a["confirmed_date"]
        key = (basis, float(a["amount"]))
        if key not in events or a["confirmed_date"] < events[key]["confirmed_date"]:
            events[key] = a

    by_period: dict[tuple[int, str], list[dict]] = {}
    for a in events.values():
        decided = date.fromisoformat(a["confirmed_date"])
        basis = date.fromisoformat(a.get("basis_date") or a["confirmed_date"])
        fy, p = _attribute_announcement(decided, basis)
        if p != "FY":
            by_period.setdefault((fy, p), []).append(a)

    out: list[DividendReport] = []
    for fy in sorted({k[0] for k in by_period}):
        reps = [r for r in periodic if int(r["fiscal_year"]) == fy]
        if any(r["period"] == "FY" for r in reps):
            continue  # 사업보고서가 나온 해는 그 연간 값이 전부 확정한다
        last_year = {r["period"]: float(r["cum_dps"]) for r in periodic if int(r["fiscal_year"]) == fy - 1}
        running = 0.0
        for p in _INTERIM_PERIODS:
            cands = sorted(by_period.get((fy, p), []), key=lambda a: a["confirmed_date"])
            if not cands:
                continue

            def target(a: dict) -> float:
                before = [float(r["cum_dps"]) for r in reps if r["confirmed_date"] < a["confirmed_date"]]
                return max([running] + before) + float(a["amount"])

            def covered(a: dict) -> bool:
                return any(r["confirmed_date"] >= a["confirmed_date"] and order[r["period"]] >= order[p]
                           and float(r["cum_dps"]) >= target(a) - 1e-6 for r in reps)

            hit = [target(a) for a in cands if covered(a)]
            if hit:
                running = max(hit)
                continue
            a = cands[-1]
            cum = target(a)
            lagging_now = any(r["period"] == p and r["confirmed_date"] >= a["confirmed_date"] for r in reps)
            prev_p = {"Q1": None, "H1": "Q1", "Q3": "H1"}[p]
            lagged_last_year = (p in last_year and _NEXT_PERIOD[p] in last_year
                                and last_year[p] <= last_year.get(prev_p, 0.0) + 1e-6
                                and last_year[_NEXT_PERIOD[p]] > last_year[p] + 1e-6)
            no_report_yet = not any(r["period"] == p for r in reps)
            shown = _NEXT_PERIOD[p] if lagging_now or (no_report_yet and lagged_last_year) else p
            out.append(DividendReport(fy, f"PROV_{shown}", cum,
                                      date.fromisoformat(a.get("basis_date") or a["confirmed_date"]),
                                      date.fromisoformat(a["confirmed_date"]), str(a.get("rcept_no", ""))))
            running = cum
    return out


# ---------------------------------------------------------------- 계산
def _overrides(code: str) -> dict[str, float]:
    p = C.CORP_ACTIONS_OVERRIDE_CSV
    if not p.exists():
        return {}
    df = pd.read_csv(p, dtype={"stock_code": str, "date": str})
    return {r.date: float(r.ratio) for r in df.itertuples() if str(r.stock_code).zfill(6) == code}


def load_us10y():
    from .fred import load_us10y as _l
    us = _l()
    return list(us["date"]), [float(v) for v in us["us10y"]]


def _with_expected_dps(annual: list[dict], exp) -> list[dict]:
    """연도별 막대그래프에 '예상 DPS'(exp.value, 폭탄 태그와 같은 값)를 덧붙인다. 태그와 차트가
    서로 다른 값을 보여주면 판단에 혼란을 주므로, exp가 실제로 계산한 연도(latest_fy + 1)에만 붙인다.
    expected_dps_asof() 자체는 바꾸지 않는다.

    메가스터디(072870)처럼 올해 아직 중간·분기배당이 전혀 공시되지 않은(확정된 분기보고서가 모두
    0인) 회사는 annual_breakdown()이 그 해 행 자체를 만들지 않는다 — 그러면 폭탄 태그·배당수익률이
    쓰는 예상 DPS(이 경우 exp가 '올해 확정분 없음'으로 판단해 전년도 연간 DPS를 그대로 씀)가 차트엔
    전혀 안 보인다. 그 해 행이 아직 없으면 확정·잠정 실적 없이(0) 예상 DPS만 있는 행을 새로 붙인다."""
    if exp is None:
        return annual
    target = exp.latest_fy + 1
    if annual and annual[-1]["provisional"] and annual[-1]["year"] == target:
        return [*annual[:-1], {**annual[-1], "expected": _r(exp.value, 2)}]
    if annual and any(r["year"] == target for r in annual):
        return annual  # 사업보고서로 이미 확정된 해 — 잠정·예상치를 덧붙이지 않는다
    if annual and target <= annual[-1]["year"]:
        return annual  # as_of/valuation_date 차이로 어긋난 드문 경우 — 순서를 깨지 않는다
    return [*annual, {"year": target, "interim": 0.0, "final": 0.0, "total": 0.0, "quarterly": False,
                      "confirmed": None, "flags": [], "provisional": True, "expected": _r(exp.value, 2)}]


def compute_analysis(info: dict, prices: pd.DataFrame, div: dict, us_dates, us_vals, valuation_date: date | None = None) -> dict:
    """engine.py로 현재·역사 지표 계산. (기존 build.py의 종목 1개 계산부를 그대로 옮김)"""
    code = info["code"]
    prices = prices[prices["close"] > 0].sort_values("date")
    if prices.empty:
        raise DataUnavailable("가격 데이터가 없습니다")
    dates = [date.fromisoformat(d) for d in prices["date"]]
    closes = [float(x) for x in prices["close"]]
    ev = detect_corp_actions(dates, closes, [float(x) for x in prices["change_pct"]], C.CORP_ACTION_THRESHOLD)
    ov = _overrides(code)
    actions = [CorpAction(d, ov.get(d.isoformat(), round_ratio(r))) for d, r, _, _ in ev]
    for od, ratio in ov.items():
        if not any(a.date.isoformat() == od for a in actions):
            actions.append(CorpAction(date.fromisoformat(od), ratio))
    actions = [a for a in actions if a.ratio != 1.0]
    adj = adjusted_prices(dates, closes, actions)
    as_of, price = dates[-1], adj[-1]
    valuation_date = valuation_date or datetime.now(KST).date()

    cur_us, cur_us_date = us10y_asof(valuation_date, us_dates, us_vals)
    years = build_fiscal_years(to_reports(div), actions)
    exp = expected_dps_asof(valuation_date, years) if years else None
    dps = exp.value if exp else None
    y = dividend_yield(dps, price)
    m = us10y_multiple(y, cur_us, C.MIN_US10Y_FOR_MULTIPLE)
    pers = persistence(years, valuation_date, dps) if years else None
    tags = dividend_tags(years, valuation_date, exp) if years else {"crown": False, "bomb": False}

    hist = yhist = None
    series = []
    if years:
        # 차트는 저장된 가격 데이터의 가능한 전체 기간을 사용한다.
        chart_start = date.fromisoformat(C.PRICE_START)
        i0 = next((i for i, d in enumerate(dates) if d >= chart_start), len(dates))
        series = daily_series(dates[i0:], adj[i0:], years, us_dates, us_vals, C.MIN_US10Y_FOR_MULTIPLE)
        # 역사적 백분위는 기존처럼 최근 10년만 사용한다. 차트 기간 확장은 통계 기준을 바꾸지 않는다.
        stats_start = date(as_of.year - C.HISTORY_YEARS, as_of.month, min(as_of.day, 28))
        stats_series = [p for p in series if p.d >= stats_start]
        hist = history_stats([p.multiple for p in stats_series], m)
        yhist = history_stats([p.div_yield for p in stats_series], y)

    tv = prices["trading_value"].iloc[-1] if "trading_value" in prices else None
    summary = {
        "code": code, "name": info["name"], "market": info["market"],
        "price": _r(price, 2), "price_date": as_of.isoformat(),
        "dps": _r(dps, 2), "yield": _r(y), "multiple": _r(m),
        "paid10": pers["paid10"] if pers else None, "known10": pers["known10"] if pers else None,
        "paid5": pers["paid5"] if pers else None,
        "pct": _r(hist["percentile"], 1) if hist else None,
        "p10": _r(hist["p10"]) if hist else None, "p90": _r(hist["p90"]) if hist else None,
        "mcap": None, "tv": _r(tv, 0) if tv is not None and not pd.isna(tv) else None,
        "has_div_data": bool(div["reports"]), "flags": exp.flags if exp else [],
        "tags": [k for k, v in tags.items() if v],
    }
    return {
        "code": code, "name": info["name"], "market": info["market"], "as_of": as_of.isoformat(),
        "valuation_date": valuation_date.isoformat(),
        # 요구사항 13의 필드 이름 (summary와 같은 값)
        "current_price": summary["price"], "current_dps": summary["dps"], "dividend_yield": summary["yield"],
        "us10y": _r(cur_us, 3), "us10y_date": cur_us_date.isoformat() if cur_us_date else None,
        "dividend_yield_to_us10y": summary["multiple"], "historical_percentile": summary["pct"],
        "summary": summary,
        "components": [{"label": c.label, "kind": c.kind, "year": c.fiscal_year, "dps": _r(c.dps, 2),
                        "confirmed": c.confirmed_date.isoformat(), "ref": c.ref} for c in (exp.components if exp else [])],
        "components_status": ("annual_confirmed" if exp and all(c.fiscal_year == exp.latest_fy for c in exp.components)
                              else "in_progress") if exp else None,
        "annual": _with_expected_dps(annual_breakdown(years, as_of), exp),
        "persistence": {k: (_r(v, 3) if isinstance(v, float) else v) for k, v in (pers or {}).items()},
        "dividend_persistence": {k: (_r(v, 3) if isinstance(v, float) else v) for k, v in (pers or {}).items()},
        "multiple_stats": {k: _r(v) for k, v in hist.items()} if hist else None,
        "historical_multiple": {k: _r(v) for k, v in hist.items()} if hist else None,
        "yield_stats": {k: _r(v) for k, v in yhist.items()} if yhist else None,
        "actions": [{"date": a.date.isoformat(), "ratio": _r(a.ratio, 6)} for a in actions],
        "series": {
            "d": [p.d.isoformat() for p in series], "px": [_r(p.price, 1) for p in series],
            "dps": [_r(p.dps, 2) for p in series], "y": [_r(p.div_yield, 3) for p in series],
            "u": [_r(p.us10y, 3) for p in series], "m": [_r(p.multiple, 3) for p in series],
        },
        "rules": C.DATA_RULES,
    }


def _within_cooldown(meta: dict | None, now: datetime | None = None) -> bool:
    """짧은 시간의 중복 요청은 막되, 날짜가 바뀌거나 장 마감 기준 시각(16:00 KST)을 넘기면 반드시 증분 갱신한다."""
    if not meta or not meta.get("updated_at") or meta.get("last_error"):
        return False
    try:
        t = datetime.fromisoformat(meta["updated_at"]).astimezone(KST)
    except (TypeError, ValueError):
        return False
    now = now or datetime.now(KST)
    if t.date() != now.date():
        return False
    # 마감 전에 받은 장중 가격을 종가로 굳히지 않도록, 16시 전 갱신 → 16시 후 요청은 쿨다운을 적용하지 않는다
    # (web/worker.js lastCloseKst()와 같은 기준 시각).
    close_cutoff = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if t < close_cutoff <= now:
        return False
    return (now - t).total_seconds() < C.REFRESH_COOLDOWN_SEC


# 수집 단계. GitHub Actions에서는 단계마다 별도 step으로 실행해 화면에 진행 상황(①~④)을 보여준다.
STAGES = ("prices", "dividends", "us10y")
STAGE_CHOICES = STAGES + ("compute",)


def _run_file(code: str):
    return stock_dir(code) / ".run.json"   # 단계 사이에 넘기는 이번 실행의 steps/errors (커밋하지 않음)


def _run_stage(code: str, stage: str, state: dict, info: dict, today, fetch_prices, fetch_dividends,
               update_us: bool, fetch_announcements=None, quick: bool = False) -> None:
    steps, errors = state["steps"], state["errors"]
    if state["skip"]:
        steps["cache"] = "cooldown: 네트워크 조회 생략"
        return
    try:
        if stage == "prices":
            steps["prices"] = update_stock_price_cache(code, today, fetch_prices, quick=quick)[1]
        elif stage == "dividends":
            steps["dividends"] = update_stock_dividend_cache(code, info["corp_code"], today, fetch_dividends)[1]
            # 정기보고서보다 먼저 나오는 '현금·현물배당결정' 수시공시 확인 (실패해도 정기보고서 결과는 유지)
            try:
                steps["announcements"] = update_stock_announcement_cache(
                    code, info["corp_code"], today, fetch_announcements)[1]
            except Exception as e:
                errors["announcements"] = repr(e)[:300]
        elif stage == "us10y" and update_us:
            from .fred import update_us10y
            src = read_json(C.SOURCES_JSON, {}).get("us10y", {})
            # 오늘 이미 성공했으면 생략. 실패만 했으면 짧게 한 번 더 시도(종목 분석을 오래 붙잡지 않음).
            if str(src.get("last_success", ""))[:10] != now_kst()[:10]:
                r = update_us10y(quick=True)
                steps["us10y"] = r
                if not r.get("ok"):
                    errors["us10y"] = r.get("error")
    except Exception as e:
        errors[stage] = repr(e)[:300]


def analyze_stock(code: str, *, force: bool = False, master: dict | None = None, today: date | None = None,
                  fetch_prices: Callable | None = None, fetch_dividends: Callable | None = None,
                  fetch_announcements: Callable | None = None,
                  update_us: bool = True, stage: str | None = None, quick: bool = False) -> dict:
    """종목 1개: 캐시 확인 → (필요하면) 가격·배당·미국10Y 업데이트 → engine 계산 → 캐시 저장.

    stage=None 이면 전부 한 번에. stage='prices'|'dividends'|'us10y' 는 그 단계만 실행하고
    이번 실행 상태를 .run.json에 넘기며, stage='compute' 가 마지막에 계산·저장한다.
    """
    from .master import load_master
    code = str(code).zfill(6)
    master = master if master is not None else load_master()
    if code not in master:
        raise StockNotFound(code)
    if stage is not None and stage not in STAGE_CHOICES:
        raise ValueError(stage)
    info = master[code]
    d = stock_dir(code)
    with _lock(code):
        meta = read_json(d / "metadata.json")
        if stage in (None, "prices"):   # 실행의 시작: cooldown 판단
            state = {"skip": not force and _within_cooldown(meta), "steps": {}, "errors": {}}
        else:
            state = read_json(_run_file(code))
            if state is None:
                # .run.json이 없다 = 이번 호출은 prices/dividends/us10y를 새로 수집하지 않는
                # 재계산 전용 호출이다(예: 백그라운드 과거 시세 보완 중 매 구간마다 부르는
                # --stage compute). 직전 수집에서 실패가 있었다면 그 사실을 지우지 말고
                # 이어간다 — 그러지 않으면 dividends 수집이 실패해도 최종 metadata.json엔
                # last_error가 null로 남아 실패가 있었다는 사실 자체가 사라진다.
                state = {"skip": True, "steps": {}, "errors": dict((meta or {}).get("last_error") or {})}
        for s in (STAGES if stage is None else [x for x in STAGES if x == stage]):
            _run_stage(code, s, state, info, today, fetch_prices, fetch_dividends, update_us, fetch_announcements, quick=quick)
        if stage in STAGES:
            write_json(_run_file(code), state)
            return state
        _run_file(code).unlink(missing_ok=True)
        steps, errors = state["steps"], state["errors"]

        prices, div = load_prices(code), load_dividends(code)
        if prices.empty:
            write_json(d / "metadata.json", {**(meta or {}), "code": code, "name": info["name"],
                       "market": info["market"], "last_error": errors or {"prices": "no data"},
                       "last_attempt": now_kst()})
            raise DataUnavailable(f"{info['name']} 데이터를 가져오지 못했습니다: {errors}")

        analysis = _save_analysis(code, info, prices, div, meta, {
            "updated_at": now_kst() if not errors else (meta or {}).get("updated_at", now_kst()),
            "valuation_date": (today or datetime.now(KST).date()).isoformat(),
            "last_attempt": now_kst(), "last_error": errors or None, "steps": steps,
        })
    rebuild_index()
    return analysis


def _save_analysis(code: str, info: dict, prices: pd.DataFrame, div: dict, meta: dict | None, extra: dict) -> dict:
    us_dates, us_vals = load_us10y()
    analysis = compute_analysis(info, prices, div, us_dates, us_vals,
                                 valuation_date=date.fromisoformat(extra["valuation_date"]) if extra.get("valuation_date") else None)
    new_meta = {
        "code": code, "name": info["name"], "market": info["market"],
        "price_through": prices["date"].max(),
        "dividend_through": max((r["fetched_at"][:10] for r in div["log"].values()), default=None),
        "us10y_through": us_dates[-1].isoformat() if us_dates else None,
        "created_at": (meta or {}).get("created_at") or now_kst(),
        **{k: (meta or {}).get(k) for k in ("updated_at", "last_attempt", "last_error", "steps")},
        **extra,
        "summary": analysis["summary"],
    }
    analysis["metadata"] = {k: v for k, v in new_meta.items() if k != "summary"}
    write_json(stock_dir(code) / "analysis.json", analysis, compact=True)
    write_json(stock_dir(code) / "metadata.json", new_meta)
    return analysis


def recompute_cached(master: dict | None = None) -> dict:
    """이미 캐시된 종목만, 네트워크 없이 다시 계산 (미국10Y가 갱신됐을 때 배수·백분위 반영).
    KRX·DART를 호출하지 않으며 조회한 적 없는 종목은 건드리지 않는다."""
    from .master import load_master
    master = master if master is not None else load_master()
    done, failed = [], {}
    if C.CACHE_DIR.exists():
        for p in sorted(C.CACHE_DIR.glob("*/prices.csv")):
            code = p.parent.name
            if code not in master:
                continue
            try:
                with _lock(code):
                    prices = load_prices(code)
                    if prices.empty:
                        continue
                    _save_analysis(code, master[code], prices, load_dividends(code),
                                   read_json(stock_dir(code) / "metadata.json"), {"recomputed_at": now_kst()})
                done.append(code)
            except Exception as e:
                failed[code] = repr(e)[:200]
    rebuild_index()
    return {"recomputed": len(done), "failed": failed or None}


def refresh_announcements_cached(master: dict | None = None, today: date | None = None,
                                 fetch: Callable | None = None) -> dict:
    """이미 캐시된 종목만 올해 '현금·현물배당결정' 공시 기록을 다시 확인하고(종목당 공시 목록 1회,
    새 공시만 원문 조회) 네트워크 없이 재계산한다. 자회사 공시 제외처럼 공시 판별 규칙이 바뀌었을 때
    각 종목을 따로 새로고침하지 않고 한 번에 반영하는 용도. 가격·정기보고서는 다시 받지 않는다."""
    from .master import load_master
    master = master if master is not None else load_master()
    done, failed = [], {}
    if C.CACHE_DIR.exists():
        for p in sorted(C.CACHE_DIR.glob("*/dividends.json")):
            code = p.parent.name
            if code not in master:
                continue
            try:
                with _lock(code):
                    update_stock_announcement_cache(code, master[code]["corp_code"], today, fetch)
                done.append(code)
            except Exception as e:
                failed[code] = repr(e)[:200]
    return {"refreshed": len(done), "failed": failed or None, "recompute": recompute_cached(master)}


def rebuild_index() -> dict:
    """캐시 폴더의 metadata.json을 모아 index.json 생성 (조회한 종목 표)."""
    rows = []
    if C.CACHE_DIR.exists():
        for p in sorted(C.CACHE_DIR.glob("*/metadata.json")):
            m = read_json(p, {})
            if m.get("summary"):
                rows.append({**m["summary"], "updated_at": m.get("updated_at"),
                             "price_through": m.get("price_through"), "has_error": bool(m.get("last_error"))})
    us = read_json(C.SOURCES_JSON, {})
    idx = {"built_at": now_kst(), "stocks": rows, "sources": us, "rules": C.DATA_RULES}
    write_json(C.CACHE_INDEX_JSON, idx, compact=True)
    return idx

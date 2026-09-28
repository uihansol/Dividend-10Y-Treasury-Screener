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


_PERIOD_END = {"Q1": (3, 31), "H1": (6, 30), "Q3": (9, 30), "FY": (12, 31)}


def _period_end_date(r: dict) -> date:
    if r.get("basis_date"):
        return date.fromisoformat(str(r["basis_date"])[:10])
    m, d = _PERIOD_END[r["period"]]
    return date(int(r["fiscal_year"]), m, d)


def _provisional_reports(div: dict) -> list[DividendReport]:
    """'현금·현물배당결정' 수시공시 중 같은 해 정기보고서로 아직 안 덮인 것만 PROV 보고서로 만든다.
    누계(cum_dps) = 그 공시 접수일까지 정기보고서로 이미 확정된 같은 해 누계 + 공시 금액.

    '이미 반영됐다'는 접수일 순서가 아니라 보고기간으로 판단한다: 정기보고서의 결산기준일이
    이 공시의 배당기준일 이후면(그 분기·반기가 이 배당까지 포함하는 기간이면) 그 정기보고서를
    신뢰하고 잠정치는 버린다. KT&G처럼 반기보고서(결산기준일 6/30)가 8월 분기배당 공시(배당기준일
    8/21)보다 늦게 '접수'돼도 그 배당을 반영하지 않는 경우가 있어, 접수일만으로는 판단할 수 없다.

    다만 결산기준일만으로 '반영됨'을 판단하면 서호전기(065710)처럼 반기보고서(결산기준일 6/30)가
    분기배당 공시(배당기준일 6/30, 같은 날) 이틀 뒤에 나왔는데도 그 표(주당 현금배당금)엔 아직
    0으로 남아 있는 경우를 잘못 '반영됨'으로 봐서 잠정치를 버리고, 그 해 자체가 화면에서 통째로
    사라진다. 그래서 결산기준일 조건에 더해, 그 정기보고서의 누계가 실제로 공시 금액만큼
    늘어났는지(직전 누계 + 공시 금액 이상인지)까지 확인한다 — 기간은 지났지만 표에 아직 안 실렸으면
    '반영 안 됨'으로 보고 잠정치를 유지한다.

    사업연도는 공시 접수일이 아니라 배당기준일(basis_date) 기준으로 정한다. 예를 들어 전년도
    기말배당 공시는 보통 다음 해 2~3월에 '접수'되므로, 접수일 연도를 쓰면 전년도 배당이 올해
    잠정치로 잘못 섞여 들어간다(서호전기 065710: 2025년 기말배당 공시가 접수일 기준 2026년으로
    잘못 분류되면서 2026년 중간배당 잠정치와 뒤섞였다)."""
    periodic = [r for r in div["reports"] if r.get("confirmed_date")]
    out = []
    for a in div.get("announcements", {}).values():
        if a.get("status") != "ok" or not a.get("confirmed_date") or not a.get("amount"):
            continue
        ann_basis = date.fromisoformat(a["basis_date"]) if a.get("basis_date") else date.fromisoformat(a["confirmed_date"])
        year = ann_basis.year
        same_year = [r for r in periodic if int(r["fiscal_year"]) == year]
        prior_cum = max([0.0] + [float(r["cum_dps"]) for r in same_year if r["confirmed_date"] <= a["confirmed_date"]])
        covered = any(_period_end_date(r) >= ann_basis and float(r["cum_dps"]) >= prior_cum + float(a["amount"])
                     for r in same_year)
        if covered:
            continue
        out.append(DividendReport(year, "PROV", prior_cum + float(a["amount"]), ann_basis,
                                  date.fromisoformat(a["confirmed_date"]), str(a.get("rcept_no", ""))))
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
    """연도별 막대그래프의 마지막 잠정 행에 '예상 DPS'(exp.value, 폭탄 태그와 같은 값)를 덧붙인다.
    태그와 차트가 서로 다른 값을 보여주면 판단에 혼란을 주므로, exp가 실제로 그 잠정 연도를
    계산한 경우(latest_fy + 1 == 그 행의 연도)에만 붙인다. expected_dps_asof() 자체는 바꾸지 않는다."""
    if not annual or not annual[-1]["provisional"] or exp is None or exp.latest_fy + 1 != annual[-1]["year"]:
        return annual
    return [*annual[:-1], {**annual[-1], "expected": _r(exp.value, 2)}]


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

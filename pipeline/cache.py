"""종목별 캐시 + 분석.

data/cache/stocks/{code}/
  metadata.json   코드·이름·시장, price_through, dividend_through, updated_at, 오류, 요약 지표
  prices.csv      date, close(원주가), change_pct(KRX 기준가 대비 %), trading_value
  dividends.json  {"log": {"2025-FY": {status, fetched_at}}, "reports": [...]}
  analysis.json   화면용 계산 결과 (기존 상세 페이지 JSON과 같은 형식)
data/cache/index.json   조회한 종목 요약 목록

analyze_stock(code)
  캐시 없음 → 2016-01-01부터 가격 + 2015년부터 배당 최초 수집
  캐시 있음 → 마지막 날짜 이후 가격, 아직 안 받은 보고서만 조회
  cooldown(기본 10분) 안에 다시 요청 → 네트워크 없이 캐시 재계산만
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
                     daily_series, detect_corp_actions, dividend_yield, expected_dps_asof, history_stats,
                     persistence, round_ratio, us10y_asof, us10y_multiple)
from .store import KST, now_kst, read_json, write_json


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
                             fetch: Callable | None = None) -> tuple[pd.DataFrame, dict]:
    """가격 캐시 증분 업데이트. 반환: (전체 가격, {"mode": initial|incremental|skip, "added": n})"""
    if fetch is None:
        from .krx import fetch_stock_prices as fetch
    today = today or datetime.now(KST).date()
    old = load_prices(code)
    if len(old):
        start = date.fromisoformat(old["date"].max()) + timedelta(days=1)
        mode = "incremental"
    else:
        start = date.fromisoformat(C.PRICE_START)
        mode = "initial"
    if start > today:
        return old, {"mode": "skip", "added": 0}
    new = fetch(code, start.isoformat(), today.isoformat())
    new = new[new["date"] >= start.isoformat()] if len(new) else new
    df = pd.concat([old, new], ignore_index=True) if len(new) else old
    df = df.drop_duplicates("date", keep="last").sort_values("date").reset_index(drop=True)
    if len(new):
        stock_dir(code).mkdir(parents=True, exist_ok=True)
        df.to_csv(stock_dir(code) / "prices.csv", index=False)
    return df, {"mode": mode, "added": int(len(new)), "from": start.isoformat()}


# ---------------------------------------------------------------- 배당 캐시
def load_dividends(code: str) -> dict:
    return read_json(stock_dir(code) / "dividends.json", {"log": {}, "reports": []})


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
    out = {"log": log, "reports": sorted(reps.values(), key=lambda r: (r["fiscal_year"], C.PERIOD_ORDER[r["period"]]))}
    write_json(stock_dir(code) / "dividends.json", out)
    return out, {"mode": "initial" if not cur["log"] else "incremental", "added": len(new)}


def to_reports(div: dict) -> list[DividendReport]:
    out = []
    for r in div["reports"]:
        if not r.get("confirmed_date"):
            continue
        m, d = {"Q1": (3, 31), "H1": (6, 30), "Q3": (9, 30), "FY": (12, 31)}[r["period"]]
        basis = date.fromisoformat(r["basis_date"][:10]) if r.get("basis_date") else date(int(r["fiscal_year"]), m, d)
        out.append(DividendReport(int(r["fiscal_year"]), r["period"], float(r["cum_dps"]), basis,
                                  date.fromisoformat(r["confirmed_date"]), str(r.get("rcept_no", ""))))
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


def compute_analysis(info: dict, prices: pd.DataFrame, div: dict, us_dates, us_vals) -> dict:
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

    cur_us, cur_us_date = us10y_asof(as_of, us_dates, us_vals)
    years = build_fiscal_years(to_reports(div), actions)
    exp = expected_dps_asof(as_of, years) if years else None
    dps = exp.value if exp else None
    y = dividend_yield(dps, price)
    m = us10y_multiple(y, cur_us, C.MIN_US10Y_FOR_MULTIPLE)
    pers = persistence(years, as_of, dps) if years else None

    hist = yhist = None
    series = []
    if years:
        start = date(as_of.year - C.HISTORY_YEARS, as_of.month, min(as_of.day, 28))
        i0 = next((i for i, d in enumerate(dates) if d >= start), len(dates))
        series = daily_series(dates[i0:], adj[i0:], years, us_dates, us_vals, C.MIN_US10Y_FOR_MULTIPLE)
        hist = history_stats([p.multiple for p in series], m)
        yhist = history_stats([p.div_yield for p in series], y)

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
    }
    return {
        "code": code, "name": info["name"], "market": info["market"], "as_of": as_of.isoformat(),
        # 요구사항 13의 필드 이름 (summary와 같은 값)
        "current_price": summary["price"], "current_dps": summary["dps"], "dividend_yield": summary["yield"],
        "us10y": _r(cur_us, 3), "us10y_date": cur_us_date.isoformat() if cur_us_date else None,
        "dividend_yield_to_us10y": summary["multiple"], "historical_percentile": summary["pct"],
        "summary": summary,
        "components": [{"label": c.label, "kind": c.kind, "year": c.fiscal_year, "dps": _r(c.dps, 2),
                        "confirmed": c.confirmed_date.isoformat(), "ref": c.ref} for c in (exp.components if exp else [])],
        "components_status": ("annual_confirmed" if exp and all(c.fiscal_year == exp.latest_fy for c in exp.components)
                              else "in_progress") if exp else None,
        "annual": annual_breakdown(years, as_of),
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


def _within_cooldown(meta: dict | None) -> bool:
    if not meta or not meta.get("updated_at") or meta.get("last_error"):
        return False
    t = datetime.fromisoformat(meta["updated_at"])
    return (datetime.now(KST) - t).total_seconds() < C.REFRESH_COOLDOWN_SEC


def analyze_stock(code: str, *, force: bool = False, master: dict | None = None, today: date | None = None,
                  fetch_prices: Callable | None = None, fetch_dividends: Callable | None = None,
                  update_us: bool = True) -> dict:
    from .master import load_master
    code = str(code).zfill(6)
    master = master if master is not None else load_master()
    if code not in master:
        raise StockNotFound(code)
    info = master[code]
    d = stock_dir(code)
    with _lock(code):
        meta = read_json(d / "metadata.json")
        errors, steps = {}, {}
        prices, div = load_prices(code), load_dividends(code)
        if force or not _within_cooldown(meta):
            try:
                prices, steps["prices"] = update_stock_price_cache(code, today, fetch_prices)
            except Exception as e:
                errors["prices"] = repr(e)[:300]
            try:
                div, steps["dividends"] = update_stock_dividend_cache(code, info["corp_code"], today, fetch_dividends)
            except Exception as e:
                errors["dividends"] = repr(e)[:300]
            if update_us:
                from .fred import update_us10y
                src = read_json(C.SOURCES_JSON, {}).get("us10y", {})
                # 오늘 이미 성공했으면 생략. 실패만 했으면 다시 시도한다.
                if str(src.get("last_success", ""))[:10] != now_kst()[:10]:
                    r = update_us10y()
                    steps["us10y"] = r
                    if not r.get("ok"):
                        errors["us10y"] = r.get("error")
        else:
            steps["cache"] = "cooldown: 네트워크 조회 생략"

        if prices.empty:
            write_json(d / "metadata.json", {**(meta or {}), "code": code, "name": info["name"],
                       "market": info["market"], "last_error": errors, "last_attempt": now_kst()})
            raise DataUnavailable(f"{info['name']} 데이터를 가져오지 못했습니다: {errors}")

        us_dates, us_vals = load_us10y()
        analysis = compute_analysis(info, prices, div, us_dates, us_vals)
        new_meta = {
            "code": code, "name": info["name"], "market": info["market"],
            "price_through": prices["date"].max(),
            "dividend_through": max((r["fetched_at"][:10] for r in div["log"].values()), default=None),
            "us10y_through": us_dates[-1].isoformat() if us_dates else None,
            "created_at": (meta or {}).get("created_at") or now_kst(),
            "updated_at": now_kst() if not errors else (meta or {}).get("updated_at", now_kst()),
            "last_attempt": now_kst(), "last_error": errors or None, "steps": steps,
            "summary": analysis["summary"],
        }
        analysis["metadata"] = {k: v for k, v in new_meta.items() if k != "summary"}
        write_json(d / "analysis.json", analysis, compact=True)
        write_json(d / "metadata.json", new_meta)
    rebuild_index()
    return analysis


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

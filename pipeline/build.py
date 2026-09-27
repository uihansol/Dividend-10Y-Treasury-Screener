"""저장된 원천 데이터 → 사이트용 JSON (web/public/data).

출력
 - meta.json            기준일, 미국10Y, 데이터 상태, 계산 규칙, 검증 요약
 - screener.json        전 종목 현재 지표 (첫 화면 표)
 - stocks/{code}.json   종목 상세 (배당 구성, 연도별 DPS, 10년 일별 수익률·배수, 백분위)
 - corporate_actions.csv(data/)  탐지된 주식 수 변동 이벤트 (검토용)
"""
from __future__ import annotations

import json
import math
import os
import shutil
from datetime import date, timedelta

import pandas as pd

from . import config as C
from .engine import (CorpAction, DividendReport, adjusted_prices, annual_breakdown,
                     build_fiscal_years, daily_series, detect_corp_actions, dividend_yield,
                     expected_dps_asof, history_stats, persistence, round_ratio, us10y_asof,
                     us10y_multiple)
from .fred import load_us10y
from .store import load_prices, load_sources, now_kst


def _r(x, n=4):
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return None
    return round(float(x), n)


def _d(s):
    if s is None or (isinstance(s, float) and math.isnan(s)) or str(s) in ("", "nan", "None"):
        return None
    return date.fromisoformat(str(s)[:10])


def load_reports() -> dict[str, list[DividendReport]]:
    if not C.DART_REPORTS_CSV.exists():
        return {}
    df = pd.read_csv(C.DART_REPORTS_CSV, dtype={"stock_code": str, "rcept_no": str})
    out: dict[str, list[DividendReport]] = {}
    for r in df.itertuples():
        conf = _d(r.confirmed_date)
        if conf is None:
            continue
        m, d = {"Q1": (3, 31), "H1": (6, 30), "Q3": (9, 30), "FY": (12, 31)}[r.period]
        basis = _d(r.basis_date) or date(int(r.fiscal_year), m, d)
        out.setdefault(r.stock_code.zfill(6), []).append(DividendReport(
            int(r.fiscal_year), r.period, float(r.cum_dps), basis, conf, str(r.rcept_no)))
    return out


def stale(name: str, through: str | None, today: date) -> bool:
    if not through:
        return True
    return (today - date.fromisoformat(through[:10])).days > C.STALE_DAYS.get(name, 7)


def build() -> dict:
    today = date.today()
    prices = load_prices()
    if prices.empty:
        raise RuntimeError("주가 데이터가 없습니다.")
    validation = {"price_nonpositive_rows": int((prices["close"] <= 0).sum())}
    prices = prices[prices["close"] > 0].sort_values(["stock_code", "date"])
    as_of = prices["date"].max()
    hist_start = date(as_of.year - C.HISTORY_YEARS, as_of.month, min(as_of.day, 28))

    us = load_us10y()
    validation["us10y_nonpositive_days"] = int((us["us10y"] <= 0).sum()) if len(us) else 0
    us_dates, us_vals = list(us["date"]), list(us["us10y"].astype(float))
    cur_us, cur_us_date = us10y_asof(as_of, us_dates, us_vals)

    stocks = pd.read_csv(C.STOCKS_CSV, dtype={"stock_code": str}) if C.STOCKS_CSV.exists() else pd.DataFrame()
    info = {r.stock_code.zfill(6): r for r in stocks.itertuples()} if len(stocks) else {}
    corp_codes = set()
    cc_path = C.DIV_DIR / "corp_codes.csv"
    if cc_path.exists():
        corp_codes = set(pd.read_csv(cc_path, dtype=str)["stock_code"])
    reports = load_reports()
    overrides = {}
    ov_path = C.DATA / "corporate_actions_override.csv"
    if ov_path.exists():  # 자동 탐지를 사람이 바로잡는 파일 (ratio=1 이면 해당 이벤트 무시)
        for r in pd.read_csv(ov_path, dtype={"stock_code": str, "date": str}).itertuples():
            overrides[(r.stock_code.zfill(6), r.date)] = float(r.ratio)

    out_dir = C.OUT
    if (out_dir / "stocks").exists():
        shutil.rmtree(out_dir / "stocks")
    (out_dir / "stocks").mkdir(parents=True, exist_ok=True)

    rows, action_rows = [], []
    flag_counts: dict[str, int] = {}
    for code, g in prices.groupby("stock_code", sort=False):
        meta = info.get(code)
        if meta is None or getattr(meta, "listing_status", "listed") != "listed":
            continue
        if corp_codes and code not in corp_codes:
            continue  # 우선주·DART 고유번호 없는 종목 제외
        dates = list(g["date"])
        closes = [float(x) for x in g["close"]]
        ev = detect_corp_actions(dates, closes, list(g["change_pct"]), C.CORP_ACTION_THRESHOLD)
        actions = [CorpAction(d, overrides.get((code, d.isoformat()), round_ratio(r))) for d, r, _, _ in ev]
        for (oc, od), ratio in overrides.items():  # 탐지되지 않은 이벤트를 수동 추가
            if oc == code and not any(a.date.isoformat() == od for a in actions):
                actions.append(CorpAction(date.fromisoformat(od), ratio))
                ev.append((date.fromisoformat(od), ratio, None, None))
        actions = [a for a in actions if a.ratio != 1.0]
        for d, r, prev, base in ev:
            used = overrides.get((code, d.isoformat()))
            action_rows.append({"stock_code": code, "date": d.isoformat(),
                                "ratio": _r(used if used is not None else round_ratio(r), 6),
                                "raw_ratio": _r(r, 6), "prev_close": prev, "base_price": _r(base, 2),
                                "source": "수동 보정" if used is not None else "KRX 기준가 역산"})
        adj = adjusted_prices(dates, closes, actions)
        last_date, last_price = dates[-1], adj[-1]
        traded_today = last_date == as_of

        reps = reports.get(code, [])
        years = build_fiscal_years(reps, actions)
        exp = expected_dps_asof(as_of, years, C.EXPECTED_DPS_MODE) if years else None
        dps = exp.value if exp else None
        y = dividend_yield(dps, last_price) if traded_today else None
        m = us10y_multiple(y, cur_us, C.MIN_US10Y_FOR_MULTIPLE)
        pers = persistence(years, as_of, dps) if years else None
        for f in (exp.flags if exp else []):
            flag_counts[f] = flag_counts.get(f, 0) + 1

        hist = None
        detail_series = None
        if years:
            i0 = next((i for i, d in enumerate(dates) if d >= hist_start), len(dates))
            s = daily_series(dates[i0:], adj[i0:], years, us_dates, us_vals,
                             C.MIN_US10Y_FOR_MULTIPLE, C.EXPECTED_DPS_MODE)
            hist = history_stats([p.multiple for p in s], m)
            yhist = history_stats([p.div_yield for p in s], y)
            detail_series = s

        row = {
            "code": code, "name": getattr(meta, "company_name", code), "market": meta.market,
            "price": _r(last_price, 2) if traded_today else None, "price_date": last_date.isoformat(),
            "dps": _r(dps, 2), "yield": _r(y), "multiple": _r(m),
            "paid10": pers["paid10"] if pers else None, "known10": pers["known10"] if pers else None,
            "paid5": pers["paid5"] if pers else None,
            "pct": _r(hist["percentile"], 1) if hist else None,
            "p10": _r(hist["p10"]) if hist else None, "p90": _r(hist["p90"]) if hist else None,
            "mcap": _r(getattr(meta, "market_cap", None), 0),
            "tv": _r(getattr(meta, "trading_value", None), 0),
            "has_div_data": bool(reps),
            "flags": exp.flags if exp else [],
        }
        rows.append(row)

        if reps and detail_series is not None:
            detail = {
                "code": code, "name": row["name"], "market": row["market"], "as_of": as_of.isoformat(),
                "summary": row, "us10y": _r(cur_us, 3), "us10y_date": cur_us_date.isoformat() if cur_us_date else None,
                "components": [{"label": c.label, "kind": c.kind, "year": c.fiscal_year, "dps": _r(c.dps, 2),
                                "confirmed": c.confirmed_date.isoformat(), "ref": c.ref} for c in (exp.components if exp else [])],
                "components_status": ("annual_confirmed" if exp and all(c.fiscal_year == exp.latest_fy for c in exp.components)
                                      else "in_progress") if exp else None,
                "annual": annual_breakdown(years, as_of),
                "persistence": {k: (_r(v, 3) if isinstance(v, float) else v) for k, v in (pers or {}).items()},
                "multiple_stats": {k: _r(v) for k, v in (hist or {}).items()} if hist else None,
                "yield_stats": {k: _r(v) for k, v in (yhist or {}).items()} if yhist else None,
                "actions": [{"date": a.date.isoformat(), "ratio": _r(a.ratio, 6)} for a in actions],
                # 일별 시계열(열 배열로 압축): 날짜, 수정주가, 예상DPS, 배당수익률, 미국10Y, 배수
                "series": {
                    "d": [p.d.isoformat() for p in detail_series],
                    "px": [_r(p.price, 1) for p in detail_series],
                    "dps": [_r(p.dps, 2) for p in detail_series],
                    "y": [_r(p.div_yield, 3) for p in detail_series],
                    "u": [_r(p.us10y, 3) for p in detail_series],
                    "m": [_r(p.multiple, 3) for p in detail_series],
                },
            }
            (out_dir / "stocks" / f"{code}.json").write_text(
                json.dumps(detail, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    pd.DataFrame(action_rows).to_csv(C.CORP_ACTIONS_CSV, index=False)

    sources = load_sources()
    src_view = {}
    for name in ("prices", "dividends", "us10y", "stocks"):
        s = sources.get(name, {})
        src_view[name] = {**s, "stale": stale(name, s.get("data_through"), today)}

    meta = {
        "built_at": now_kst(), "as_of": as_of.isoformat(),
        "us10y": _r(cur_us, 3), "us10y_date": cur_us_date.isoformat() if cur_us_date else None,
        "expected_dps_mode": C.EXPECTED_DPS_MODE,
        "sources": src_view, "rules": C.DATA_RULES,
        "counts": {"rows": len(rows), "with_dividend_data": sum(r["has_div_data"] for r in rows),
                   "with_multiple": sum(r["multiple"] is not None for r in rows),
                   "detected_corp_actions": len(action_rows)},
        "validation": {**validation, "flag_counts": flag_counts},
        "repo": os.environ.get("GITHUB_REPOSITORY"),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "screener.json").write_text(json.dumps(rows, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return meta["counts"]

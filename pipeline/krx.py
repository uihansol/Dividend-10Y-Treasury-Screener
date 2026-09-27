"""KRX 일별 주가·종목 목록 수집 (pykrx 사용).

⚠ 2025-12-27부터 KRX 정보데이터시스템이 회원제로 바뀌었고, 2026-09 이후에는
   비로그인 요청을 거절한다. 따라서 KRX 계정(KRX_ID / KRX_PW 환경변수)이 반드시 필요하다.
   KRX 계정은 90일마다 비밀번호 변경을 요구하므로, 만료되면 GitHub Secret도 바꿔야 한다.

하루 단위 전종목 조회(get_market_ohlcv(날짜, market=...))를 쓴다.
 - 과거 10년 최초 구축: 거래일 × 2시장 ≈ 5천 회 호출 (재시작 가능: 받은 날짜는 건너뜀)
 - 매일 업데이트: 마지막 저장일 이후 날짜만 호출
원주가(비수정 종가)와 등락률을 저장한다. 등락률은 KRX '기준가' 대비라서
분할·무상증자 등 권리락을 역산하는 데 쓴다(engine.detect_corp_actions).
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta

import pandas as pd

from . import config as C
from .store import (add_non_trading_day, daily_price_path, load_non_trading_days,
                    mark_source, saved_price_days)

SLEEP = 0.6  # 요청 간격(초). KRX 서버 차단 방지


class KrxUnavailable(RuntimeError):
    pass


def _stock():
    """pykrx는 import 시점에 로그인한다. 로그인 실패가 프로세스 전체를 죽이지 않도록 감싼다."""
    try:
        from pykrx import stock  # noqa: WPS433
        return stock
    except Exception as e:  # pragma: no cover - 네트워크 의존
        raise KrxUnavailable(f"pykrx import/KRX 로그인 실패: {e!r}. KRX_ID/KRX_PW와 비밀번호 만료 여부를 확인하세요.")


def fetch_day(ymd: str) -> pd.DataFrame | None:
    """하루치 KOSPI+KOSDAQ 전종목. 휴장일이면 None."""
    stock = _stock()
    frames = []
    for market in ("KOSPI", "KOSDAQ"):
        df = stock.get_market_ohlcv(ymd, market=market)
        time.sleep(SLEEP)
        if df is None or df.empty:
            continue
        df = df.reset_index()
        code_col = df.columns[0]
        out = pd.DataFrame({
            "date": datetime.strptime(ymd, "%Y%m%d").date().isoformat(),
            "stock_code": df[code_col].astype(str).str.zfill(6),
            "market": market,
            "close": pd.to_numeric(df["종가"], errors="coerce"),
            "change_pct": pd.to_numeric(df["등락률"], errors="coerce"),
            "trading_value": pd.to_numeric(df.get("거래대금"), errors="coerce"),
        })
        frames.append(out)
    if not frames:
        return None
    day = pd.concat(frames, ignore_index=True)
    # 휴장일에 0만 채워 오는 경우 방지
    if (day["trading_value"].fillna(0).sum() <= 0) and (day["close"].fillna(0).sum() <= 0):
        return None
    return day


def update_prices(start: str | None = None, max_days: int | None = None) -> dict:
    """start(YYYY-MM-DD)부터 오늘까지 저장되지 않은 거래일만 받는다."""
    have = saved_price_days()
    skip = load_non_trading_days()
    if start is None:
        start = (max(have) if have else C.PRICE_START.replace("-", ""))
        start = datetime.strptime(start, "%Y%m%d").date().isoformat()
    d = date.fromisoformat(start)
    today = date.today()
    fetched = 0
    last_ok = max(have) if have else None
    try:
        while d <= today:
            ymd = d.strftime("%Y%m%d")
            if d.weekday() < 5 and ymd not in have and ymd not in skip:
                day = fetch_day(ymd)
                if day is None:
                    if d < today:  # 오늘은 장 마감 전일 수 있으니 휴장으로 확정하지 않음
                        add_non_trading_day(ymd)
                else:
                    p = daily_price_path(ymd)
                    p.parent.mkdir(parents=True, exist_ok=True)
                    day.to_csv(p, index=False)
                    fetched += 1
                    last_ok = ymd
                    if max_days and fetched >= max_days:
                        break
            d += timedelta(days=1)
        through = datetime.strptime(last_ok, "%Y%m%d").date().isoformat() if last_ok else None
        mark_source("prices", True, data_through=through, note=f"+{fetched} days")
        return {"ok": True, "fetched_days": fetched, "through": through}
    except Exception as e:
        through = datetime.strptime(last_ok, "%Y%m%d").date().isoformat() if last_ok else None
        mark_source("prices", False, error=repr(e), data_through=through)
        return {"ok": False, "error": repr(e), "fetched_days": fetched}


def update_stock_list() -> dict:
    """가장 최근 저장 거래일 기준 종목 목록. 이름은 새 종목만 조회한다."""
    from .store import load_prices
    try:
        have = saved_price_days()
        if not have:
            raise RuntimeError("주가 데이터가 없습니다. 먼저 prices 단계를 실행하세요.")
        latest = max(have)
        latest_iso = datetime.strptime(latest, "%Y%m%d").date().isoformat()
        day = load_prices(since=latest_iso)
        day = day[day["date"].astype(str) == latest_iso]

        old = pd.read_csv(C.STOCKS_CSV, dtype=str) if C.STOCKS_CSV.exists() else pd.DataFrame(
            columns=["stock_code", "company_name", "market", "listing_status", "market_cap", "trading_value", "as_of"])
        names = dict(zip(old["stock_code"], old["company_name"]))
        stock = _stock()
        rows = []
        for _, r in day.iterrows():
            code = r["stock_code"]
            name = names.get(code)
            if not name or name == "nan":
                name = stock.get_market_ticker_name(code)
                time.sleep(0.05)
            rows.append({"stock_code": code, "company_name": name, "market": r["market"],
                         "listing_status": "listed", "trading_value": r["trading_value"], "as_of": latest_iso})
        new = pd.DataFrame(rows)

        # 시가총액(선택 데이터). 실패해도 목록은 저장
        try:
            cap = stock.get_market_cap(latest, market="ALL")
            cap = cap.reset_index()
            cap_map = dict(zip(cap.iloc[:, 0].astype(str).str.zfill(6), cap["시가총액"]))
            new["market_cap"] = new["stock_code"].map(cap_map)
        except Exception:
            new["market_cap"] = None

        gone = old[~old["stock_code"].isin(new["stock_code"])].copy()
        if len(gone):
            gone["listing_status"] = "not_trading"   # 상장폐지·거래정지·시장 이전 등. 원인은 구분하지 않음
        out = pd.concat([new, gone], ignore_index=True)
        out.to_csv(C.STOCKS_CSV, index=False)
        mark_source("stocks", True, data_through=latest_iso, note=f"{len(new)} listed")
        return {"ok": True, "listed": len(new)}
    except Exception as e:
        mark_source("stocks", False, error=repr(e))
        return {"ok": False, "error": repr(e)}

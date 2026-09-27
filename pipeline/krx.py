"""KRX 일별 주가·종목 목록 수집 (pykrx 사용).

KRX는 전종목 일별 조회 API를 제공하므로 하루마다 KOSPI/KOSDAQ을 조회한다.
최초 구축과 증분 업데이트 모두 이미 저장된 날짜는 건너뛴다.

최초 구축은 수천 회의 요청이 필요하므로 KOSPI와 KOSDAQ 요청을 병렬화해
전체 소요 시간을 줄인다. 서버 차단을 피하기 위해 각 요청 사이에 SLEEP을 둔다.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pandas as pd

from . import config as C
from .store import (
    add_non_trading_day,
    daily_price_path,
    load_non_trading_days,
    mark_source,
    saved_price_days,
)

SLEEP = 0.35
MARKETS = ("KOSPI", "KOSDAQ")


class KrxUnavailable(RuntimeError):
    pass


def _stock():
    """pykrx import/KRX 로그인 실패를 명확한 오류로 변환한다."""
    try:
        from pykrx import stock  # noqa: WPS433
        return stock
    except Exception as e:  # pragma: no cover - 네트워크 의존
        raise KrxUnavailable(
            f"pykrx import/KRX 로그인 실패: {e!r}. "
            "KRX_ID/KRX_PW와 비밀번호 만료 여부를 확인하세요."
        )


def _fetch_market(stock, ymd: str, market: str) -> pd.DataFrame | None:
    df = stock.get_market_ohlcv(ymd, market=market)
    time.sleep(SLEEP)
    if df is None or df.empty:
        return None

    df = df.reset_index()
    code_col = df.columns[0]
    return pd.DataFrame({
        "date": datetime.strptime(ymd, "%Y%m%d").date().isoformat(),
        "stock_code": df[code_col].astype(str).str.zfill(6),
        "market": market,
        "close": pd.to_numeric(df["종가"], errors="coerce"),
        "change_pct": pd.to_numeric(df["등락률"], errors="coerce"),
        "trading_value": pd.to_numeric(df.get("거래대금"), errors="coerce"),
    })


def fetch_day(ymd: str) -> pd.DataFrame | None:
    """하루치 KOSPI+KOSDAQ 전종목. 두 시장을 병렬 조회한다."""
    stock = _stock()

    # pykrx의 내부 HTTP 요청을 시장별 독립 작업으로 실행한다.
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_fetch_market, stock, ymd, market) for market in MARKETS]
        frames = [f.result() for f in futures]

    frames = [df for df in frames if df is not None and not df.empty]
    if not frames:
        return None

    day = pd.concat(frames, ignore_index=True)

    # 휴장일에 0만 채워 오는 경우 방지
    if (day["trading_value"].fillna(0).sum() <= 0) and (day["close"].fillna(0).sum() <= 0):
        return None
    return day


def update_prices(start: str | None = None, max_days: int | None = None) -> dict:
    """마지막 저장일 이후의 거래일만 받는다.

    start를 지정하면 그 날짜부터 재개할 수 있다.
    max_days는 최초 구축을 여러 실행으로 나눌 때 사용한다.
    """
    have = saved_price_days()
    skip = load_non_trading_days()

    if start is None:
        if have:
            # 마지막 저장일 자체는 이미 있으므로 다음 날짜부터 탐색한다.
            start_date = datetime.strptime(max(have), "%Y%m%d").date() + timedelta(days=1)
        else:
            start_date = date.fromisoformat(C.PRICE_START)
    else:
        start_date = date.fromisoformat(start)

    today = date.today()
    fetched = 0
    last_ok = max(have) if have else None

    try:
        d = start_date
        while d <= today:
            ymd = d.strftime("%Y%m%d")

            if d.weekday() < 5 and ymd not in have and ymd not in skip:
                day = fetch_day(ymd)

                if day is None:
                    if d < today:
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

        through = (
            datetime.strptime(last_ok, "%Y%m%d").date().isoformat()
            if last_ok else None
        )
        mark_source("prices", True, data_through=through, note=f"+{fetched} days")
        return {"ok": True, "fetched_days": fetched, "through": through}

    except Exception as e:
        through = (
            datetime.strptime(last_ok, "%Y%m%d").date().isoformat()
            if last_ok else None
        )
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
            columns=["stock_code", "company_name", "market", "listing_status",
                     "market_cap", "trading_value", "as_of"]
        )
        names = dict(zip(old["stock_code"], old["company_name"]))

        stock = _stock()
        rows = []
        for _, r in day.iterrows():
            code = r["stock_code"]
            name = names.get(code)
            if not name or name == "nan":
                name = stock.get_market_ticker_name(code)
                time.sleep(0.05)

            rows.append({
                "stock_code": code,
                "company_name": name,
                "market": r["market"],
                "listing_status": "listed",
                "trading_value": r["trading_value"],
                "as_of": latest_iso,
            })

        new = pd.DataFrame(rows)

        try:
            cap = stock.get_market_cap(latest, market="ALL")
            cap = cap.reset_index()
            cap_map = dict(
                zip(cap.iloc[:, 0].astype(str).str.zfill(6), cap["시가총액"])
            )
            new["market_cap"] = new["stock_code"].map(cap_map)
        except Exception:
            new["market_cap"] = None

        gone = old[~old["stock_code"].isin(new["stock_code"])].copy()
        if len(gone):
            gone["listing_status"] = "not_trading"

        out = pd.concat([new, gone], ignore_index=True)
        out.to_csv(C.STOCKS_CSV, index=False)

        mark_source("stocks", True, data_through=latest_iso, note=f"{len(new)} listed")
        return {"ok": True, "listed": len(new)}

    except Exception as e:
        mark_source("stocks", False, error=repr(e))
        return {"ok": False, "error": repr(e)}

"""KRX 데이터 (pykrx). 종목 1개 단위 조회만 한다.

- fetch_stock_prices(code, start, end): 한 종목의 일별 원주가(비수정)·등락률
- fetch_market_listing(): 종목 master용 KOSPI/KOSDAQ 종목코드·이름 (주 1회 정도, 검색 때는 호출하지 않음)

⚠ KRX 정보데이터시스템은 2025-12-27부터 회원제이며 2026-09부터 비로그인 요청을 거절한다.
  pykrx는 import 시 KRX_ID / KRX_PW 환경변수로 로그인한다. 비밀번호는 90일마다 만료된다.

전체 종목을 날짜별로 순회하는 수집 코드는 더 이상 없다.
"""
from __future__ import annotations

from datetime import date, datetime

import pandas as pd

PRICE_COLS = ["date", "close", "change_pct", "trading_value"]


class KrxUnavailable(RuntimeError):
    pass


def _stock():
    try:
        from pykrx import stock  # noqa: WPS433  (import 시 로그인)
        return stock
    except Exception as e:  # pragma: no cover - 네트워크 의존
        raise KrxUnavailable(f"pykrx import/KRX 로그인 실패: {e!r}. KRX_ID/KRX_PW와 비밀번호 만료 여부를 확인하세요.")


def _ymd(d: str) -> str:
    return d.replace("-", "")


def fetch_stock_prices(code: str, start: str, end: str) -> pd.DataFrame:
    """한 종목의 start~end(YYYY-MM-DD) 일별 원주가.

    adjusted=False: 비수정 종가. 분할·무상증자는 등락률(KRX 기준가 대비)로 역산해
    engine.detect_corp_actions 가 찾는다.
    """
    stock = _stock()
    df = stock.get_market_ohlcv(_ymd(start), _ymd(end), code, adjusted=False)
    if df is None or df.empty:
        return pd.DataFrame(columns=PRICE_COLS)
    df = df.reset_index()
    out = pd.DataFrame({
        "date": pd.to_datetime(df.iloc[:, 0]).dt.date.astype(str),
        "close": pd.to_numeric(df["종가"], errors="coerce"),
        "change_pct": pd.to_numeric(df["등락률"], errors="coerce") if "등락률" in df else float("nan"),
        "trading_value": pd.to_numeric(df["거래대금"], errors="coerce") if "거래대금" in df else float("nan"),
    })
    # 거래정지일 등 종가 0 행은 오류 데이터로 보고 제외 (0으로 계산하지 않음)
    return out[out["close"] > 0].reset_index(drop=True)


def fetch_market_listing(asof: date | None = None) -> list[dict]:
    """KOSPI·KOSDAQ 상장 종목 코드/이름/시장. master.json 생성용."""
    stock = _stock()
    ymd = (asof or date.today()).strftime("%Y%m%d")
    rows = []
    for market in ("KOSPI", "KOSDAQ"):
        tickers = stock.get_market_ticker_list(ymd, market=market)
        for t in tickers:
            try:
                name = stock.get_market_ticker_name(t)
            except Exception:
                name = None  # 이름 조회 실패는 DART 이름으로 보완
            rows.append({"code": str(t).zfill(6), "name": name, "market": market})
    return rows

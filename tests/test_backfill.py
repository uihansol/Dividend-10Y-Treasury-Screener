"""과거 가격 백필 종료 조건 테스트. 네트워크 없이 가짜 KRX를 주입한다 (가격은 가상 값)."""
from datetime import date

import pandas as pd

from pipeline import cache
from pipeline import config as C


class FakeKrx:
    """listed_from 이후 영업일만 돌려주는 가짜 KRX. 호출 기록을 남긴다."""
    def __init__(self, listed_from: str):
        self.calls, self.listed_from = [], listed_from

    def __call__(self, code, start, end):
        self.calls.append((start, end))
        days = pd.bdate_range(max(start, self.listed_from), end)
        days = [d for d in days.date.astype(str) if d != "2015-01-01"]   # 신정 휴장
        return pd.DataFrame({"date": days, "close": 10000.0, "change_pct": 0.0, "trading_value": 1e9})


def _seed(code, start, end):
    d = C.CACHE_DIR / code
    d.mkdir(parents=True)
    days = pd.bdate_range(start, end).date.astype(str)
    pd.DataFrame({"date": days, "close": 10000.0, "change_pct": 0.0, "trading_value": 1e9}).to_csv(d / "prices.csv", index=False)


def test_backfill_already_complete_skips_krx(tmp_data):
    """첫 거래일(2015-01-02)까지 있으면 KRX를 부르지 않고 done."""
    _seed("000001", "2015-01-02", "2015-03-31")
    krx = FakeKrx("2000-01-01")
    r = cache.backfill_stock_price_cache("000001", today=date(2026, 9, 28), fetch=krx)
    assert r["done"] is True and r["added"] == 0
    assert krx.calls == []


def test_backfill_reaches_first_trading_day_then_done(tmp_data):
    # 2016-01-04 → 1년 청크 시작이 2015-01-04라 floor(2015-01-01) 7일 안: floor까지 늘려 2015-01-02를 받는다.
    _seed("000002", "2016-01-04", "2016-03-31")
    krx = FakeKrx("2000-01-01")
    r = cache.backfill_stock_price_cache("000002", today=date(2026, 9, 28), fetch=krx)
    assert r["done"] is True and r["added"] > 0
    assert cache.load_prices("000002")["date"].min() == "2015-01-02"
    r2 = cache.backfill_stock_price_cache("000002", today=date(2026, 9, 28), fetch=krx)
    assert r2["done"] is True and len(krx.calls) == 1   # 두 번째 호출은 KRX 조회 없음


def test_backfill_recent_listing_stops_on_empty_chunk(tmp_data):
    """2015년 이후 상장 종목: 상장 전 구간이 비면 done (예전에는 매 실행 12회 빈 조회)."""
    _seed("000003", "2024-08-30", "2024-12-31")
    krx = FakeKrx("2024-08-30")
    r = cache.backfill_stock_price_cache("000003", today=date(2026, 9, 28), fetch=krx)
    assert r["done"] is True and r["added"] == 0
    assert len(krx.calls) == 1


def test_backfill_continues_while_chunks_have_data(tmp_data):
    _seed("000004", "2020-06-01", "2020-12-31")
    krx = FakeKrx("2000-01-01")
    r = cache.backfill_stock_price_cache("000004", today=date(2026, 9, 28), fetch=krx)
    assert r["done"] is False and r["added"] > 0

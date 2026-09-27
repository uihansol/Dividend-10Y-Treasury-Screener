"""검색·캐시·중복요청·look-ahead 테스트. 네트워크 없이 가짜 수집 함수를 주입한다.
(가격·배당 숫자는 동작 검증용 가상 값이며 실제 기업 데이터가 아니다)"""
from datetime import date, timedelta

import pandas as pd
import pytest

from pipeline import config as C
from pipeline.master import search

MASTER = {
    "005930": {"code": "005930", "name": "삼성전자", "market": "KOSPI", "corp_code": "00126380", "aliases": ["삼전"]},
    "009150": {"code": "009150", "name": "삼성전기", "market": "KOSPI", "corp_code": "00126371", "aliases": []},
    "006400": {"code": "006400", "name": "삼성SDI", "market": "KOSPI", "corp_code": "00126362", "aliases": []},
    "005380": {"code": "005380", "name": "현대차", "market": "KOSPI", "corp_code": "00164742", "aliases": ["현대자동차"]},
}


# ------------------------------------------------------------------ 검색
def test_search_name():
    assert search("삼성전자", MASTER)[0]["code"] == "005930"


def test_search_hyundai_short_and_formal():
    assert search("현대차", MASTER)[0]["code"] == "005380"
    assert search("현대자동차", MASTER)[0]["code"] == "005380"


def test_search_code():
    assert search("005930", MASTER)[0]["name"] == "삼성전자"


def test_search_prefix_lists_candidates():
    codes = [s["code"] for s in search("삼성", MASTER)]
    assert set(codes) == {"005930", "009150", "006400"}


def test_search_none():
    assert search("존재하지않는종목", MASTER) == []
    assert search("", MASTER) == []


# ------------------------------------------------------------------ 캐시
@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(C, "CACHE_DIR", tmp_path / "cache" / "stocks")
    monkeypatch.setattr(C, "CACHE_INDEX_JSON", tmp_path / "cache" / "index.json")
    monkeypatch.setattr(C, "SOURCES_JSON", tmp_path / "sources.json")
    monkeypatch.setattr(C, "US10Y_CSV", tmp_path / "us10y" / "dgs10.csv")
    monkeypatch.setattr(C, "CORP_ACTIONS_OVERRIDE_CSV", tmp_path / "none.csv")
    (tmp_path / "us10y").mkdir()
    days = pd.bdate_range("2016-01-01", "2026-09-25")
    pd.DataFrame({"date": days.date.astype(str), "us10y": 3.0}).to_csv(C.US10Y_CSV, index=False)
    return tmp_path


class FakeKrx:
    """요청된 기간만 돌려주는 가짜 KRX. 호출 기록을 남긴다."""
    def __init__(self, last_day: date):
        self.calls, self.last_day = [], last_day

    def __call__(self, code, start, end):
        self.calls.append((code, start, end))
        days = pd.bdate_range(start, min(date.fromisoformat(end), self.last_day))
        return pd.DataFrame({"date": days.date.astype(str), "close": 50000.0, "change_pct": 0.0,
                             "trading_value": 1e9})


class FakeDart:
    def __init__(self):
        self.calls = []

    def __call__(self, corp_code, start_year, end_year, log, today=None):
        from pipeline.dart import needs_fetch
        new, log = [], dict(log)
        for y in range(end_year, start_year - 1, -1):
            for p in ("FY", "Q3", "H1", "Q1"):
                key = f"{y}-{p}"
                if not needs_fetch(key, log, today):
                    continue
                self.calls.append(key)
                log[key] = {"status": "ok", "fetched_at": f"{today.isoformat()}T20:00:00+09:00"}
                cum = {"Q1": 361, "H1": 722, "Q3": 1083, "FY": 1444}[p]
                conf = {"Q1": f"{y}-05-15", "H1": f"{y}-08-14", "Q3": f"{y}-11-14", "FY": f"{y + 1}-03-10"}[p]
                new.append({"fiscal_year": y, "period": p, "cum_dps": cum, "confirmed_date": conf,
                            "basis_date": None, "rcept_no": conf.replace("-", "") + "000001"})
        return new, log


def test_first_then_incremental(tmp_data, monkeypatch):
    from pipeline.cache import analyze_stock
    monkeypatch.setattr(C, "REFRESH_COOLDOWN_SEC", 0)
    krx, dart = FakeKrx(date(2026, 9, 24)), FakeDart()
    r1 = analyze_stock("005930", master=MASTER, today=date(2026, 9, 25),
                       fetch_prices=krx, fetch_dividends=dart, update_us=False)
    # 최초: 2016-01-01부터 전체 기간 1회
    assert krx.calls == [("005930", "2016-01-01", "2026-09-25")]
    assert r1["metadata"]["steps"]["prices"]["mode"] == "initial"
    n_dart_first = len(dart.calls)
    assert n_dart_first > 40                                  # 2015~2026 보고서
    assert r1["summary"]["dps"] is not None and r1["multiple_stats"] is not None

    krx.last_day = date(2026, 9, 25)
    r2 = analyze_stock("005930", master=MASTER, today=date(2026, 9, 27),
                       fetch_prices=krx, fetch_dividends=dart, update_us=False)
    # 두 번째: 마지막 저장일 다음 날부터만
    assert krx.calls[1] == ("005930", "2026-09-25", "2026-09-27")
    assert r2["metadata"]["steps"]["prices"]["mode"] == "incremental"
    assert r2["metadata"]["price_through"] == "2026-09-25"
    # 이미 받은 배당 보고서는 다시 요청하지 않음 (2026 Q3는 아직 due 전)
    assert len(dart.calls) == n_dart_first


def test_duplicate_requests_use_cache(tmp_data, monkeypatch):
    from pipeline.cache import analyze_stock
    monkeypatch.setattr(C, "REFRESH_COOLDOWN_SEC", 600)
    krx, dart = FakeKrx(date(2026, 9, 25)), FakeDart()
    for _ in range(3):
        analyze_stock("005930", master=MASTER, fetch_prices=krx, fetch_dividends=dart, update_us=False)
    assert len(krx.calls) == 1                                  # 전체 기간은 한 번만


def test_other_stock_only_fetches_itself(tmp_data, monkeypatch):
    from pipeline.cache import analyze_stock
    krx, dart = FakeKrx(date(2026, 9, 25)), FakeDart()
    analyze_stock("005380", master=MASTER, fetch_prices=krx, fetch_dividends=dart, update_us=False)
    assert {c[0] for c in krx.calls} == {"005380"}
    assert (C.CACHE_DIR / "005380" / "analysis.json").exists()
    assert not (C.CACHE_DIR / "005930").exists()


def test_krx_failure_uses_existing_cache(tmp_data, monkeypatch):
    from pipeline.cache import analyze_stock, DataUnavailable
    monkeypatch.setattr(C, "REFRESH_COOLDOWN_SEC", 0)
    good, dart = FakeKrx(date(2026, 9, 24)), FakeDart()
    analyze_stock("005930", master=MASTER, today=date(2026, 9, 25), fetch_prices=good,
                  fetch_dividends=dart, update_us=False)

    def broken(*a, **k):
        raise RuntimeError("KRX down")
    r = analyze_stock("005930", master=MASTER, today=date(2026, 9, 27), fetch_prices=broken,
                      fetch_dividends=dart, update_us=False)
    assert "prices" in r["metadata"]["last_error"]
    assert r["summary"]["price"] == 50000.0                   # 기존 캐시로 계산
    with pytest.raises(DataUnavailable):                       # 캐시가 전혀 없으면 오류
        analyze_stock("009150", master=MASTER, fetch_prices=broken, fetch_dividends=dart, update_us=False)


def test_history_has_no_lookahead(tmp_data):
    from pipeline.cache import analyze_stock
    krx, dart = FakeKrx(date(2026, 9, 25)), FakeDart()
    r = analyze_stock("005930", master=MASTER, today=date(2026, 9, 25), fetch_prices=krx,
                      fetch_dividends=dart, update_us=False)
    s = r["series"]
    i = s["d"].index("2025-03-07")   # 2024 사업보고서(2025-03-10 접수) 전: 2024 Q3까지 + 2023 기말
    j = s["d"].index("2025-03-10")   # 접수일부터 2024 연간 DPS
    assert s["dps"][i] == 1083 + 361
    assert s["dps"][j] == 1444
    assert s["d"][0] >= "2016-09-01"   # 최근 10년만


def test_index_lists_only_viewed(tmp_data):
    from pipeline.cache import analyze_stock
    from pipeline.store import read_json
    analyze_stock("005930", master=MASTER, fetch_prices=FakeKrx(date(2026, 9, 25)),
                  fetch_dividends=FakeDart(), update_us=False)
    idx = read_json(C.CACHE_INDEX_JSON)
    assert [s["code"] for s in idx["stocks"]] == ["005930"]

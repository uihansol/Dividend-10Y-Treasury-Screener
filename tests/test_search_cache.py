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


# ------------------------------------------------------------------ 단계별 실행 (Actions step ①~④)
def test_staged_run_equals_single_run(tmp_data, monkeypatch):
    from pipeline.cache import analyze_stock
    kw = dict(master=MASTER, today=date(2026, 9, 25), update_us=False)
    krx, dart = FakeKrx(date(2026, 9, 25)), FakeDart()
    for st in ("prices", "dividends", "us10y"):
        state = analyze_stock("005930", stage=st, fetch_prices=krx, fetch_dividends=dart, **kw)
        assert not state["errors"]
    staged = analyze_stock("005930", stage="compute", **kw)
    assert staged["metadata"]["steps"]["prices"]["mode"] == "initial"
    assert not (C.CACHE_DIR / "005930" / ".run.json").exists()
    assert len(krx.calls) == 1

    monkeypatch.setattr(C, "CACHE_DIR", tmp_data / "other")
    single = analyze_stock("005930", fetch_prices=FakeKrx(date(2026, 9, 25)), fetch_dividends=FakeDart(), **kw)
    assert staged["summary"] == single["summary"] and staged["series"] == single["series"]


def test_staged_failure_is_carried_to_compute(tmp_data, monkeypatch):
    from pipeline.cache import analyze_stock
    monkeypatch.setattr(C, "REFRESH_COOLDOWN_SEC", 0)
    kw = dict(master=MASTER, update_us=False)
    analyze_stock("005930", fetch_prices=FakeKrx(date(2026, 9, 24)), fetch_dividends=FakeDart(),
                  today=date(2026, 9, 25), **kw)

    def broken(*a, **k):
        raise RuntimeError("KRX down")
    analyze_stock("005930", stage="prices", fetch_prices=broken, today=date(2026, 9, 27), **kw)
    r = analyze_stock("005930", stage="compute", today=date(2026, 9, 27), **kw)
    assert "prices" in r["metadata"]["last_error"]
    assert r["summary"]["price"] == 50000.0                     # 기존 캐시로 계산


def test_recompute_cached_is_offline_and_only_cached(tmp_data):
    from pipeline.cache import analyze_stock, recompute_cached
    analyze_stock("005930", master=MASTER, fetch_prices=FakeKrx(date(2026, 9, 25)),
                  fetch_dividends=FakeDart(), today=date(2026, 9, 25), update_us=False)
    before = (C.CACHE_DIR / "005930" / "analysis.json").read_text()
    # 미국10Y가 바뀜 → 배수만 달라져야 한다 (수집 함수 없이)
    days = pd.bdate_range("2016-01-01", "2026-09-25")
    pd.DataFrame({"date": days.date.astype(str), "us10y": 4.0}).to_csv(C.US10Y_CSV, index=False)
    r = recompute_cached(master=MASTER)
    assert r == {"recomputed": 1, "failed": None}
    import json
    a0, a1 = json.loads(before), json.loads((C.CACHE_DIR / "005930" / "analysis.json").read_text())
    assert a1["summary"]["multiple"] == pytest.approx(a0["summary"]["multiple"] * 3 / 4, rel=1e-3)
    assert a1["metadata"]["last_attempt"] == a0["metadata"]["last_attempt"]   # 화면 폴링 기준은 그대로
    assert sorted(p.name for p in C.CACHE_DIR.iterdir()) == ["005930"]


# ------------------------------------------------------------------ 미국 10년물
def test_fred_and_treasury_parsers():
    from pipeline.fred import _clean, parse_fred_csv, parse_treasury_csv
    f = _clean(parse_fred_csv("observation_date,DGS10\n2026-09-23,4.12\n2026-09-24,.\n2026-09-25,4.15\n"))
    assert [(d.isoformat(), v) for d, v in zip(f["date"], f["us10y"])] == [("2026-09-23", 4.12), ("2026-09-25", 4.15)]
    t = _clean(parse_treasury_csv('Date,"1 Mo","2 Yr","10 Yr","30 Yr"\n09/25/2026,4.3,3.9,4.15,4.7\n'
                                  '09/24/2026,4.3,3.9,4.13,4.7\n'))
    assert sorted((d.isoformat(), v) for d, v in zip(t["date"], t["us10y"])) == [("2026-09-24", 4.13),
                                                                                ("2026-09-25", 4.15)]


def test_us10y_falls_back_and_appends_only_new(tmp_data):
    from pipeline.fred import load_us10y, update_us10y
    from pipeline.store import read_json
    n0 = len(load_us10y())
    seen = []

    def fred_down(start, timeout, attempts):
        seen.append(("fred", start, attempts))
        raise TimeoutError("read timed out")

    def treasury(start, timeout, attempts):
        seen.append(("treasury", start, attempts))
        return pd.DataFrame({"date": ["2026-09-24", "2026-09-25", "2026-09-28"], "us10y": ["9.9", "9.9", "4.2"]})
    r = update_us10y(quick=True, sources=[("fred_csv", fred_down), ("treasury", treasury)])
    assert r["ok"] and r["source"] == "treasury" and r["added"] == 1       # 저장된 날짜는 덮어쓰지 않음
    assert seen[0][1] == date(2026, 9, 18) and seen[0][2] == 1             # 마지막 저장일-7일부터, quick=1회
    us = load_us10y()
    assert len(us) == n0 + 1 and us["us10y"].iloc[-1] == 4.2
    assert read_json(C.SOURCES_JSON)["us10y"]["data_through"] == "2026-09-28"

    def down(*a):
        raise ConnectionError("x")
    r = update_us10y(sources=[("fred_csv", down), ("treasury", down)])
    assert not r["ok"] and len(load_us10y()) == n0 + 1                     # 실패해도 기존 데이터 유지
    assert read_json(C.SOURCES_JSON)["us10y"]["last_error"]


def test_search_real_master_file():
    """저장소의 실제 data/stocks/master.json (+aliases.json)으로 검색. 네트워크 없음."""
    from pipeline.master import load_master
    m = load_master()
    if not m:
        pytest.skip("master.json 없음")
    assert search("삼성전자", m)[0]["code"] == "005930"
    assert search("현대차", m)[0]["code"] == "005380"
    assert search("005930", m)[0]["name"] == "삼성전자"
    assert search("삼전", m)[0]["code"] == "005930"
    assert search("존재하지않는종목", m) == []

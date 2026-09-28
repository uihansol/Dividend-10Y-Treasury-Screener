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


def test_search_chosung_exact():
    assert search("ㅅㅅㅈㅈ", MASTER)[0]["code"] == "005930"


def test_search_chosung_prefix_lists_candidates():
    codes = [s["code"] for s in search("ㅅㅅ", MASTER)]
    assert set(codes) == {"005930", "009150", "006400"}


def test_search_chosung_alias():
    assert search("ㅎㄷㅈㄷㅊ", MASTER)[0]["code"] == "005380"


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


def test_recompute_only_call_preserves_prior_fetch_error(tmp_data):
    """백그라운드 과거 시세 보완(backfill)은 --stage compute만 반복 호출해 재계산한다(새 수집 없음).
    직전 실행에서 dividends 수집이 실패했다면, 이 재계산 전용 호출들 때문에 그 실패 사실이
    metadata.json에서 조용히 사라지면 안 된다 — 그러면 last_error가 null로 보여 배당 데이터가
    없는 건지 수집이 실패한 건지 구분할 수 없다(남화산업 111710에서 실제로 일어난 문제)."""
    from pipeline.cache import analyze_stock

    def broken_dividends(*a, **k):
        raise RuntimeError("ConnectTimeout")

    kw = dict(master=MASTER, today=date(2026, 9, 25), update_us=False)
    analyze_stock("005930", stage="prices", fetch_prices=FakeKrx(date(2026, 9, 25)), **kw)
    analyze_stock("005930", stage="dividends", fetch_dividends=broken_dividends, **kw)
    analyze_stock("005930", stage="us10y", **kw)
    r1 = analyze_stock("005930", stage="compute", **kw)
    assert "dividends" in r1["metadata"]["last_error"]
    assert not (C.CACHE_DIR / "005930" / ".run.json").exists()   # 첫 compute 후 삭제됨

    # backfill 루프가 하듯 .run.json 없이 --stage compute만 다시 호출
    r2 = analyze_stock("005930", stage="compute", **kw)
    assert "dividends" in r2["metadata"]["last_error"]


def test_annual_chart_expected_dps_matches_bomb_tag_basis():
    """배당 탭 차트의 마지막(잠정) 막대에 붙는 '예상 DPS'는 폭탄 태그가 쓰는 값(exp.value)과
    같아야 한다 — 태그와 차트가 다른 값을 보여주면 일관된 판단을 할 수 없다(광주신세계 037710:
    예년엔 기말배당만 있다가 올해 처음 중간배당이 생긴 경우)."""
    from pipeline.cache import _with_expected_dps
    from pipeline.engine import DividendReport as R
    from pipeline.engine import annual_breakdown, build_fiscal_years, dividend_tags, expected_dps_asof

    rows = [
        R(2024, "FY", 2200, date(2024, 12, 31), date(2025, 3, 12), ""),
        R(2025, "FY", 2400, date(2025, 12, 31), date(2026, 3, 16), ""),
        R(2026, "H1", 1800, date(2026, 6, 30), date(2026, 8, 14), ""),
    ]
    yrs = build_fiscal_years(rows, [])
    asof = date(2026, 9, 28)
    exp = expected_dps_asof(asof, yrs)
    annual = _with_expected_dps(annual_breakdown(yrs, asof), exp)

    cur = annual[-1]
    assert cur["year"] == 2026 and cur["provisional"] is True
    assert cur["total"] == 1800            # 실제 확정된 중간배당(잠정, 빗금 막대)
    assert cur["expected"] == exp.value == 4200.0   # 폭탄 태그가 쓰는 값과 동일한 막대 추가분
    assert all(r["expected"] is None for r in annual[:-1])

    tags = dividend_tags(yrs, asof, exp)
    assert tags["bomb"] is True             # 그 예상 DPS가 실제로 태그 기준을 넘긴다는 것도 같이 확인


def test_annual_chart_shows_expected_dps_even_without_this_years_interim_data():
    """메가스터디(072870)처럼 올해 확정된 분기보고서가 모두 0(공시된 배당이 아직 없음)이면
    annual_breakdown()은 그 해 행을 아예 안 만든다 — 그러면 홈 화면 태그·배당수익률이 쓰는 예상
    DPS(이 경우 '올해 확정분 없음'으로 전년도 연간 DPS를 그대로 씀)가 차트엔 전혀 안 보인다.
    그 해 행이 없으면 실적 0·예상치만 있는 행을 새로 붙여야 한다."""
    from pipeline.cache import _with_expected_dps
    from pipeline.engine import DividendReport as R
    from pipeline.engine import annual_breakdown, build_fiscal_years, expected_dps_asof

    rows = [
        R(2024, "FY", 800, date(2024, 12, 31), date(2025, 3, 20), ""),
        R(2025, "FY", 1320, date(2025, 12, 31), date(2026, 3, 19), ""),
        R(2026, "Q1", 0, date(2026, 3, 31), date(2026, 5, 15), ""),
        R(2026, "H1", 0, date(2026, 6, 30), date(2026, 8, 14), ""),
    ]
    yrs = build_fiscal_years(rows, [])
    asof = date(2026, 9, 28)
    exp = expected_dps_asof(asof, yrs)
    base = annual_breakdown(yrs, asof)
    assert [r["year"] for r in base] == [2024, 2025]   # 기존 로직은 2026 행을 만들지 않는다

    annual = _with_expected_dps(base, exp)
    assert [r["year"] for r in annual] == [2024, 2025, 2026]
    cur = annual[-1]
    assert cur["provisional"] is True and cur["total"] == 0.0
    assert cur["expected"] == exp.value == 1320.0   # 전년도 연간 DPS를 그대로 예상치로


def test_annual_chart_no_duplicate_row_once_target_year_itself_is_confirmed():
    """exp가 가리키는 해(latest_fy + 1)가 이미 사업보고서로 확정돼 확정 목록에 들어가 있으면
    (예: as_of/valuation_date가 갈라진 드문 경우) 잠정·예상치 행을 따로 덧붙이지 않는다."""
    from pipeline.cache import _with_expected_dps
    from pipeline.engine import DividendReport as R
    from pipeline.engine import annual_breakdown, build_fiscal_years, expected_dps_asof

    rows = [R(2024, "FY", 800, date(2024, 12, 31), date(2025, 3, 20), ""),
           R(2025, "FY", 1320, date(2025, 12, 31), date(2026, 3, 19), "")]
    yrs = build_fiscal_years(rows, [])
    asof = date(2026, 4, 1)
    base = annual_breakdown(yrs, asof)
    # exp가 latest_fy=2024로 보게(2025 확정 전 시점) 만들어 target=2025가 base엔 이미 확정 상태로 있게 한다
    exp = expected_dps_asof(date(2026, 1, 1), yrs)
    assert exp.latest_fy == 2024
    annual = _with_expected_dps(base, exp)
    assert [r["year"] for r in annual] == [2024, 2025]   # 2025가 이미 확정 행 — 추가로 안 붙음
    assert annual[-1]["provisional"] is False and annual[-1]["expected"] is None


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

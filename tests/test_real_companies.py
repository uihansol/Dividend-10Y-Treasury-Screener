"""실제 기업 검증 (요구사항 28). 실제로 받은 data/ 가 있을 때만 실행된다.

  pytest tests/test_real_companies.py -s

- 불변 조건(invariant)은 자동으로 검사한다.
- DART 원문과 대조할 값은 표로 출력한다. 대조한 기댓값을 tests/real_expectations.csv 에
  적어 두면(stock_code,fiscal_year,annual_dps_current_basis) 그 값과도 자동 비교한다.
  ※ 기댓값을 추측으로 채우지 말 것. DART 공시에서 직접 확인한 숫자만 적는다.

대상 (상황 ①~⑦)
  005930 삼성전자  : 분기배당(④), 2018-05-04 50:1 액면분할(⑦)
  017670 SK텔레콤  : 분기배당(④), 2021년 인적분할·액면분할(⑦, 자동 탐지 검토용)
  아래 CASES에 중간배당 없는 기업(①), 중간배당 기업(②③), 배당 중단(⑤), 급변(⑥) 종목을
  직접 추가해서 쓴다.
"""
from datetime import date

import pandas as pd
import pytest

from pipeline import config as C
from pipeline.build import load_reports
from pipeline.engine import (CorpAction, adjusted_prices, build_fiscal_years, detect_corp_actions,
                             expected_dps_asof, persistence, round_ratio)
from pipeline.store import load_prices

CASES = ["005930", "017670"]

pytestmark = pytest.mark.skipif(not C.DART_REPORTS_CSV.exists() or not any(C.PRICES_DIR.rglob("*.*")),
                                reason="실제 데이터가 아직 없음 (python -m pipeline.update ... 먼저 실행)")


@pytest.fixture(scope="module")
def prices():
    return load_prices()


@pytest.fixture(scope="module")
def reports():
    return load_reports()


def _setup(code, prices, reports):
    g = prices[prices["stock_code"] == code].sort_values("date")
    ev = detect_corp_actions(list(g["date"]), list(g["close"]), list(g["change_pct"]), C.CORP_ACTION_THRESHOLD)
    actions = [CorpAction(d, round_ratio(r)) for d, r, _, _ in ev]
    return g, actions, build_fiscal_years(reports.get(code, []), actions)


@pytest.mark.parametrize("code", CASES)
def test_invariants(code, prices, reports):
    g, actions, years = _setup(code, prices, reports)
    assert len(g) > 0, "주가 없음"
    assert years, "배당 데이터 없음"
    for fy in years.values():
        assert "interim_exceeds_annual" not in fy.flags, f"{fy.year}: 중간배당 누계 > 연간"
        if fy.fy_total is not None:
            assert fy.final_dps >= 0
    # look-ahead: 사업보고서 접수 전날에는 그 연도가 쓰이면 안 된다
    for fy in years.values():
        if fy.fy_confirmed:
            e = expected_dps_asof(fy.fy_confirmed, years)
            before = expected_dps_asof(date.fromordinal(fy.fy_confirmed.toordinal() - 1), years)
            assert e.latest_fy >= fy.year
            assert before is None or before.latest_fy < fy.year


def test_samsung_split_detected(prices, reports):
    g, actions, _ = _setup("005930", prices, reports)
    if g["date"].min() > date(2018, 5, 4):
        pytest.skip("2018년 주가 없음")
    hit = [a for a in actions if a.date == date(2018, 5, 4)]
    assert hit and hit[0].ratio == 50.0


@pytest.mark.parametrize("code", CASES)
def test_print_for_manual_check(code, prices, reports):
    g, actions, years = _setup(code, prices, reports)
    print(f"\n=== {code} | 탐지된 주식수 이벤트: {[(a.date.isoformat(), a.ratio) for a in actions]}")
    print("연도  중간·분기(현재기준)  기말  연간  확정일  flags")
    for y in sorted(years):
        fy = years[y]
        print(y, round(fy.interim_total(), 2), fy.final_dps and round(fy.final_dps, 2),
              fy.fy_total and round(fy.fy_total, 2), fy.fy_confirmed, fy.flags)
    as_of = g["date"].max()
    e = expected_dps_asof(as_of, years, C.EXPECTED_DPS_MODE)
    print("기준일", as_of, "예상 DPS", e and round(e.value, 2), [(c.label, round(c.dps, 2)) for c in (e.components if e else [])])
    print("지속성", persistence(years, as_of, e.value if e else None))


def test_against_expectations(prices, reports):
    p = C.ROOT / "tests" / "real_expectations.csv"
    if not p.exists():
        pytest.skip("tests/real_expectations.csv 없음")
    exp = pd.read_csv(p, dtype={"stock_code": str})
    for r in exp.itertuples():
        _, _, years = _setup(r.stock_code, prices, reports)
        got = years[int(r.fiscal_year)].fy_total
        assert got == pytest.approx(float(r.annual_dps_current_basis), rel=1e-3), (r.stock_code, r.fiscal_year)

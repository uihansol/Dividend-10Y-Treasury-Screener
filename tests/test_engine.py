"""STEP 1: 계산 엔진 규칙 검증.

여기 쓰인 숫자는 **규칙 검증용 가상 시나리오**이며 실제 기업 데이터가 아니다.
(요구사항 27의 STEP 1 '샘플 데이터로 계산 엔진 구현'에 해당)
실제 기업 검증은 tests/test_real_companies.py 에서 실제로 받은 데이터로 수행한다.
"""
from datetime import date as D

import pytest

from pipeline.engine import (
    CorpAction, DividendReport, build_fiscal_years, daily_series, detect_corp_actions,
    dividend_yield, expected_dps_asof, history_stats, persistence, round_ratio,
    us10y_asof, us10y_multiple, adjusted_prices,
)


def R(fy, period, dps, basis, confirmed, ref=""):
    return DividendReport(fy, period, dps, basis, confirmed, ref)


# --- 요구사항 2의 예시 숫자 ----------------------------------------------------------
def test_spec_example_yield_and_multiple():
    y = dividend_yield(1000, 15000)
    assert y == pytest.approx(6.6667, abs=1e-4)
    m = us10y_multiple(round(y, 2), 5.18)
    assert m == pytest.approx(1.29, abs=0.005)


def test_invalid_inputs_are_na():
    assert dividend_yield(1000, 0) is None
    assert dividend_yield(1000, -5) is None
    assert dividend_yield(-1, 1000) is None
    assert us10y_multiple(5.0, 0.0) is None
    assert us10y_multiple(5.0, -0.1) is None
    assert us10y_multiple(None, 4.0) is None


# --- 가상 기업 A: 기말배당만 (① 중간배당 없는 기업) -------------------------------------
A = [
    R(2024, "FY", 600, D(2024, 12, 31), D(2025, 3, 14)),
    R(2025, "FY", 700, D(2025, 12, 31), D(2026, 3, 13)),
]


def test_case1_no_interim():
    yrs = build_fiscal_years(A, [])
    e = expected_dps_asof(D(2026, 9, 25), yrs)
    assert e.value == 700 and e.latest_fy == 2025
    assert [c.kind for c in e.components] == ["final"]


def test_lookahead_before_filing_uses_older_year():
    yrs = build_fiscal_years(A, [])
    # 2026-03-12: 2025 사업보고서(3/13 접수) 이전 → 2024 연간 DPS
    assert expected_dps_asof(D(2026, 3, 12), yrs).value == 600
    assert expected_dps_asof(D(2026, 3, 13), yrs).value == 700
    # 2015년 이전 정보 없음 → N/A
    assert expected_dps_asof(D(2025, 1, 2), yrs) is None


# --- 가상 기업 B: 중간배당 (② 현재년도 중간배당 확정, ③ 기말까지 확정) ---------------------
B = [
    R(2025, "H1", 300, D(2025, 6, 30), D(2025, 8, 14)),
    R(2025, "Q3", 300, D(2025, 9, 30), D(2025, 11, 14)),
    R(2025, "FY", 1000, D(2025, 12, 31), D(2026, 3, 16)),   # 기말 700
    R(2026, "H1", 300, D(2026, 6, 30), D(2026, 8, 14)),
]


def test_case2_interim_confirmed_this_year():
    yrs = build_fiscal_years(B, [])
    e = expected_dps_asof(D(2026, 9, 25), yrs)
    assert e.value == 1000  # 2025 기말 700 + 2026 중간 300 (요구사항 1 예시)
    kinds = {c.kind: c.dps for c in e.components}
    assert kinds == {"final": 700, "interim": 300}


def test_case3_annual_confirmed_rule3_default():
    yrs = build_fiscal_years(B, [])
    # 2025 사업보고서 제출 직후, 2026 중간배당 확정 전
    e = expected_dps_asof(D(2026, 4, 1), yrs)
    assert e.value == 1000
    assert [c.kind for c in e.components] == ["interim", "final"]


def test_interim_lookahead():
    yrs = build_fiscal_years(B, [])
    # 2026 중간배당(8/14 접수) 직전에는 반영되지 않아야 한다
    assert expected_dps_asof(D(2026, 8, 13), yrs).value == 1000   # annual 모드: 2025 연간
    assert expected_dps_asof(D(2026, 8, 13), yrs, mode="final_only").value == 700


# --- 가상 기업 C: 분기배당 (④) --------------------------------------------------------
C = [
    R(2025, "Q1", 361, D(2025, 3, 31), D(2025, 5, 15)),
    R(2025, "H1", 722, D(2025, 6, 30), D(2025, 8, 14)),
    R(2025, "Q3", 1083, D(2025, 9, 30), D(2025, 11, 14)),
    R(2025, "FY", 1444, D(2025, 12, 31), D(2026, 3, 11)),
    R(2026, "Q1", 400, D(2026, 3, 31), D(2026, 5, 15)),
    R(2026, "H1", 800, D(2026, 6, 30), D(2026, 8, 14)),
]


def test_case4_quarterly():
    yrs = build_fiscal_years(C, [])
    assert yrs[2025].final_dps == 361
    assert yrs[2025].is_quarterly()

    # 2026-06-01: Q1만 확정 → 올해 Q1 + 전년도 Q2 + Q3 + 기말
    assert expected_dps_asof(D(2026, 6, 1), yrs).value == 400 + 361 + 361 + 361

    # 2026-09-25: H1까지 확정 → 올해 Q1+Q2 + 전년도 Q3 + 기말
    assert expected_dps_asof(D(2026, 9, 25), yrs).value == 800 + 361 + 361

    # 2026-02: 2025 사업보고서 전 → 2024 연간 없음 → N/A
    assert expected_dps_asof(D(2026, 2, 1), yrs) is None


def test_per_period_interims_detected():
    rows = [
        R(2025, "Q1", 361, D(2025, 3, 31), D(2025, 5, 15)),
        R(2025, "H1", 361, D(2025, 6, 30), D(2025, 8, 14)),
        R(2025, "Q3", 300, D(2025, 9, 30), D(2025, 11, 14)),  # 감소 → 기간분으로 간주
        R(2025, "FY", 1383, D(2025, 12, 31), D(2026, 3, 11)),
    ]
    yrs = build_fiscal_years(rows, [])
    assert "interim_values_treated_as_per_period" in yrs[2025].flags
    assert yrs[2025].interim_total() == 1022
    assert yrs[2025].final_dps == 361


# --- 가상 기업 D: 배당 중단 (⑤) · 큰 변화 (⑥) ------------------------------------------
def _annual(values: dict[int, float]):
    return [R(y, "FY", v, D(y, 12, 31), D(y + 1, 3, 15)) for y, v in values.items()]


def test_case5_suspension():
    rows = _annual({2015: 100, 2016: 100, 2017: 0, 2018: 0, 2019: 50, 2020: 50,
                    2021: 0, 2022: 60, 2023: 60, 2024: 60, 2025: 60})
    yrs = build_fiscal_years(rows, [])
    p = persistence(yrs, D(2026, 9, 1), 60)
    assert p["latest_fy"] == 2025
    assert p["paid10"] == 7 and p["known10"] == 10     # 2016~2025
    assert p["paid5"] == 4                              # 2021~2025
    assert p["suspensions"] == 2                        # 2016→2017, 2020→2021
    assert p["max_decline"] == -100.0
    assert p["cagr10"] == pytest.approx(((60 / 100) ** 0.1 - 1) * 100)
    assert p["cagr5"] == pytest.approx(((60 / 50) ** 0.2 - 1) * 100)


def test_case6_large_change():
    rows = _annual({2015: 100, 2020: 400, 2021: 150, 2025: 500})
    yrs = build_fiscal_years(rows, [])
    p = persistence(yrs, D(2026, 9, 1), 500)
    assert p["max_decline"] == pytest.approx(-62.5)      # 400 → 150
    assert p["known10"] == 3                             # 2016~2025 중 데이터 3개
    assert p["vs10y"] == pytest.approx(5.0)              # 2015 대비
    assert p["cagr5"] == pytest.approx(((500 / 400) ** 0.2 - 1) * 100)
    assert p["cagr10"] == pytest.approx(((500 / 100) ** 0.1 - 1) * 100)


# --- 가상 기업 E: 액면분할 (⑦) ---------------------------------------------------------
def test_case7_split_adjustment():
    # 2018-05-04 50:1 분할. 분할 전 연간 DPS 50,000원 → 현재 기준 1,000원
    split = [CorpAction(D(2018, 5, 4), 50.0)]
    rows = [
        R(2017, "FY", 50000, D(2017, 12, 31), D(2018, 3, 2)),
        R(2018, "FY", 1400, D(2018, 12, 31), D(2019, 3, 6)),
    ]
    yrs = build_fiscal_years(rows, split)
    assert yrs[2017].fy_total == pytest.approx(1000)
    assert yrs[2018].fy_total == pytest.approx(1400)

    dates = [D(2018, 4, 27), D(2018, 5, 4)]
    raw = [2_650_000, 51_900]
    adj = adjusted_prices(dates, raw, split)
    assert adj == pytest.approx([53_000, 51_900])
    # 분할 전날 수익률 = 원 DPS / 원주가 와 같아야 한다
    y_before = dividend_yield(expected_dps_asof(dates[0], yrs).value, adj[0])
    assert y_before == pytest.approx(50000 / 2_650_000 * 100)


def test_detect_split_from_base_price():
    dates = [D(2018, 4, 27), D(2018, 5, 4), D(2018, 5, 8)]
    closes = [2_650_000, 51_900, 52_000]
    # 5/4 기준가 53,000 대비 -2.08%
    chg = [1.65, -2.08, 0.19]
    ev = detect_corp_actions(dates, closes, chg, 0.02)
    assert len(ev) == 1 and ev[0][0] == D(2018, 5, 4)
    assert round_ratio(ev[0][1]) == 50.0


# --- 미국 10년물 as-of ----------------------------------------------------------------
def test_us10y_uses_strictly_prior_date():
    ud = [D(2026, 9, 22), D(2026, 9, 23), D(2026, 9, 25)]
    uv = [4.10, 4.12, 4.15]
    assert us10y_asof(D(2026, 9, 25), ud, uv) == (4.12, D(2026, 9, 23))  # 당일 값 사용 안 함
    assert us10y_asof(D(2026, 9, 24), ud, uv) == (4.12, D(2026, 9, 23))  # 미국 휴장·결측 → 직전값
    assert us10y_asof(D(2026, 9, 22), ud, uv) == (None, None)


# --- 역사 시계열 + 백분위 ---------------------------------------------------------------
def test_daily_series_no_lookahead_and_percentile():
    yrs = build_fiscal_years(B, [])
    dates = [D(2026, 3, 13), D(2026, 3, 16), D(2026, 8, 13), D(2026, 8, 14)]
    px = [10000, 10000, 10000, 10000]
    ud = [D(2026, 1, 2)]
    uv = [4.0]
    s = daily_series(dates, px, yrs, ud, uv)
    assert s[0].dps is None                         # 2025 사업보고서 이전, 2024 정보 없음
    assert s[1].dps == 1000 and s[1].multiple == pytest.approx(2.5)
    assert s[3].dps == 1000                          # 700 + 300
    vals = [float(i) for i in range(1, 101)]
    st = history_stats(vals, 82.0)
    assert st["p50"] == pytest.approx(50.5)
    assert st["percentile"] == pytest.approx(81.5)
    assert history_stats(vals[:5], 3.0) is None      # 표본 부족

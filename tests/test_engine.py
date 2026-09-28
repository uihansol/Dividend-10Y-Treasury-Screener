"""STEP 1: 계산 엔진 규칙 검증.

여기 쓰인 숫자는 **규칙 검증용 가상 시나리오**이며 실제 기업 데이터가 아니다.
(요구사항 27의 STEP 1 '샘플 데이터로 계산 엔진 구현'에 해당)
실제 기업 검증은 tests/test_real_companies.py 에서 실제로 받은 데이터로 수행한다.
"""
from datetime import date as D

import pytest

from pipeline.engine import (
    CorpAction, DividendReport, annual_breakdown, build_fiscal_years, daily_series, detect_corp_actions,
    dividend_tags, dividend_yield, expected_dps_asof, history_stats, persistence, round_ratio,
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


# --- 메인 목록 태그(왕관/폭탄) ----------------------------------------------------------
def test_crown_ten_years_flat_or_growing():
    """10년 연속 배당, 매해 전년 이상 → 왕관."""
    rows = _annual({y: 100 + (y - 2015) * 10 for y in range(2015, 2026)})  # 2015~2025
    yrs = build_fiscal_years(rows, [])
    asof = D(2026, 9, 1)
    exp = expected_dps_asof(asof, yrs)
    tags = dividend_tags(yrs, asof, exp)
    assert tags["crown"] is True


def test_crown_false_on_one_cut():
    """9년은 증가·유지, 딱 한 해 감소 → 왕관 아님."""
    values = {y: 100 for y in range(2015, 2026)}
    values[2020] = 80   # 2019(100) → 2020(80) 감소
    rows = _annual(values)
    yrs = build_fiscal_years(rows, [])
    asof = D(2026, 9, 1)
    exp = expected_dps_asof(asof, yrs)
    assert dividend_tags(yrs, asof, exp)["crown"] is False


def test_crown_false_on_missing_year():
    """중간에 배당 데이터가 없는(연속이 아닌) 해가 있으면 왕관 아님."""
    values = {y: 100 for y in range(2015, 2026) if y != 2019}
    rows = _annual(values)
    yrs = build_fiscal_years(rows, [])
    asof = D(2026, 9, 1)
    exp = expected_dps_asof(asof, yrs)
    assert dividend_tags(yrs, asof, exp)["crown"] is False


def test_crown_flat_dps_allowed():
    """전년과 정확히 같은 금액(감소 아님)은 왕관을 깨지 않는다."""
    rows = _annual({y: 100 for y in range(2015, 2026)})
    yrs = build_fiscal_years(rows, [])
    asof = D(2026, 9, 1)
    exp = expected_dps_asof(asof, yrs)
    assert dividend_tags(yrs, asof, exp)["crown"] is True


def test_bomb_provisional_increase_mid_year():
    """'올해' = asof가 속한 연도(2026). 올해 1분기까지 확정 금액만으로도 예상 DPS가
    전년(2025) 총액 대비 50% 이상이면 폭탄(잠정 포함)."""
    rows = [
        R(2025, "FY", 100, D(2025, 12, 31), D(2026, 3, 13)),
        R(2026, "Q1", 90, D(2026, 3, 31), D(2026, 5, 15)),   # 1분기만으로 이미 전년 총액 근접
    ]
    yrs = build_fiscal_years(rows, [])
    asof = D(2026, 6, 1)
    exp = expected_dps_asof(asof, yrs)
    # exp.value = 올해 Q1 확정분(90) + 전년도 Q1 이후분 대체(전년 기말 100, 중간 없음) = 190
    assert exp.value == pytest.approx(190)
    assert dividend_tags(yrs, asof, exp)["bomb"] is True


def test_bomb_false_under_threshold_with_this_year_data():
    """올해 데이터가 있어도 전년 대비 증가폭이 50% 미만이면 폭탄이 아니다."""
    rows = [
        R(2025, "FY", 1000, D(2025, 12, 31), D(2026, 3, 1)),
        R(2026, "Q1", 100, D(2026, 3, 31), D(2026, 4, 15)),
    ]
    yrs = build_fiscal_years(rows, [])
    asof = D(2026, 5, 1)
    exp = expected_dps_asof(asof, yrs)
    # exp.value = 100(올해 Q1) + 1000(전년 기말, 전년은 중간배당 없음) = 1100 → 전년(1000) 대비 +10%
    assert exp.value == pytest.approx(1100)
    assert dividend_tags(yrs, asof, exp)["bomb"] is False


def test_bomb_false_right_after_annual_report_with_no_new_year_data():
    """사업보고서가 막 나와 전년도 총액이 크게 늘었어도, '올해'(asof.year) 자체의 정보가
    아직 하나도 없으면(같은 값을 그대로 이어받은 상태) 폭탄으로 보지 않는다 — 전년도 자기 자신과
    비교하는 셈이 되기 때문. 올해 첫 분기 공시가 나와야 비교할 수 있다."""
    rows = _annual({2024: 100, 2025: 150})  # 2025 사업연도(+50%) 보고서가 2026-03-13 접수
    yrs = build_fiscal_years(rows, [])
    asof = D(2026, 4, 1)   # 올해(2026) 분기 공시는 아직 없음
    exp = expected_dps_asof(asof, yrs)
    assert exp.value == 150     # 올해 정보가 없어 전년(2025) 확정치를 그대로 예상치로 사용
    assert dividend_tags(yrs, asof, exp)["bomb"] is False


def test_bomb_false_prior_year_not_yet_confirmed():
    """1~3월 등 전년도 사업보고서가 아직 나오지 않아 비교할 전년도 확정치가 없으면 폭탄이 아니다."""
    rows = _annual({2024: 100, 2025: 150})
    yrs = build_fiscal_years(rows, [])
    asof = D(2026, 2, 1)  # 2025 사업보고서(3/13 접수) 전: 전년도(2025) 미확정
    exp = expected_dps_asof(asof, yrs)
    assert dividend_tags(yrs, asof, exp)["bomb"] is False


def test_bomb_false_zero_baseline():
    """전년도 확정 배당이 0이면(무배당→배당) 비율이 무의미하므로 폭탄으로 보지 않는다."""
    rows = [
        R(2025, "FY", 0, D(2025, 12, 31), D(2026, 3, 1)),      # 전년도(2025) 무배당
        R(2026, "Q1", 50, D(2026, 3, 31), D(2026, 4, 15)),     # 올해 배당 시작
    ]
    yrs = build_fiscal_years(rows, [])
    asof = D(2026, 5, 1)
    exp = expected_dps_asof(asof, yrs)
    assert exp.value == pytest.approx(50)
    assert dividend_tags(yrs, asof, exp)["bomb"] is False


def test_tags_empty_without_dividend_history():
    assert dividend_tags({}, D(2026, 9, 1), None) == {"crown": False, "bomb": False}


# --- 배당 차트의 올해 잠정 행 (annual_breakdown provisional row) -----------------------------
def test_annual_breakdown_appends_provisional_current_year():
    rows = [
        R(2025, "FY", 100, D(2025, 12, 31), D(2026, 3, 13)),
        R(2026, "Q1", 40, D(2026, 3, 31), D(2026, 5, 15)),
        R(2026, "H1", 90, D(2026, 6, 30), D(2026, 8, 14)),
    ]
    yrs = build_fiscal_years(rows, [])
    out = annual_breakdown(yrs, D(2026, 9, 1))
    assert [r["year"] for r in out] == [2025, 2026]
    assert out[0]["provisional"] is False and out[0]["total"] == 100
    cur = out[1]
    assert cur["provisional"] is True
    assert cur["interim"] == 90 and cur["final"] == 0.0 and cur["total"] == 90
    assert cur["confirmed"] == "2026-08-14"


def test_annual_breakdown_no_provisional_row_without_this_year_data():
    """올해 아직 아무 공시도 없으면(예: 1월) 잠정 행을 붙이지 않는다."""
    rows = _annual({2024: 100, 2025: 150})
    yrs = build_fiscal_years(rows, [])
    out = annual_breakdown(yrs, D(2026, 2, 1))
    assert [r["year"] for r in out] == [2024]   # 2025는 아직 미확정(3/15 접수), 2026은 데이터 없음


def test_annual_breakdown_no_duplicate_once_annual_report_filed():
    """올해 사업보고서가 이미 나왔으면(확정 목록에 이미 있으면) 잠정 행을 따로 붙이지 않는다."""
    rows = _annual({2024: 100, 2025: 150})
    yrs = build_fiscal_years(rows, [])
    out = annual_breakdown(yrs, D(2026, 4, 1))
    assert [r["year"] for r in out] == [2024, 2025]
    assert all(r["provisional"] is False for r in out)


def test_annual_breakdown_respects_lookahead():
    """asof 이후에 확정된 공시는(아직 미래 정보이므로) 잠정 행에 포함하지 않는다."""
    rows = [
        R(2025, "FY", 100, D(2025, 12, 31), D(2026, 3, 13)),
        R(2026, "Q1", 40, D(2026, 3, 31), D(2026, 5, 15)),
    ]
    yrs = build_fiscal_years(rows, [])
    out = annual_breakdown(yrs, D(2026, 4, 1))  # Q1 접수(5/15) 전
    assert [r["year"] for r in out] == [2025]


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


# --- 사용자 규칙 예시 그대로: 전년도 Q1·Q2·Q3·기말 300씩 -------------------------------
EX = [
    R(2025, "Q1", 300, D(2025, 3, 31), D(2025, 5, 15)),
    R(2025, "H1", 600, D(2025, 6, 30), D(2025, 8, 14)),
    R(2025, "Q3", 900, D(2025, 9, 30), D(2025, 11, 14)),
    R(2025, "FY", 1200, D(2025, 12, 31), D(2026, 3, 11)),
    R(2026, "Q1", 400, D(2026, 3, 31), D(2026, 5, 15)),
    R(2026, "H1", 800, D(2026, 6, 30), D(2026, 8, 14)),
    R(2026, "Q3", 1200, D(2026, 9, 30), D(2026, 11, 13)),
    R(2026, "FY", 1600, D(2026, 12, 31), D(2027, 3, 10)),
]


@pytest.mark.parametrize("t,expected", [
    (D(2026, 4, 1), 1200),    # 올해 확정분 없음 → 전년도 연간
    (D(2026, 5, 15), 1300),   # Q1만 확정: 400 + 300 + 300 + 300
    (D(2026, 8, 14), 1400),   # H1까지: 800 + 300 + 300
    (D(2026, 11, 13), 1500),  # Q3까지: 1200 + 300
    (D(2027, 3, 10), 1600),   # 사업보고서: 실제 연간 DPS
    (D(2026, 11, 12), 1400),  # look-ahead: Q3 보고서 접수 전날
])
def test_user_substitute_rule(t, expected):
    e = expected_dps_asof(t, build_fiscal_years(EX, []))
    assert e.value == pytest.approx(expected)


# --- 요구사항: 1분기·반기·3분기·결산 확정분을 전부 개별 항목으로 보여준다 --------------------
# (예상 DPS 구성이 "~까지 확정 배당" 한 줄로 뭉쳐 보이지 않고, 실제 확정된 보고서 종류마다
#  자기 접수일·접수번호를 가진 별도 줄로 나오는지 확인한다)
D_QTR = [
    R(2025, "Q1", 100, D(2025, 3, 31), D(2025, 5, 15)),
    R(2025, "H1", 250, D(2025, 6, 30), D(2025, 8, 14)),   # 2분기분 150
    R(2025, "Q3", 400, D(2025, 9, 30), D(2025, 11, 14)),  # 3분기분 150
    R(2025, "FY", 550, D(2025, 12, 31), D(2026, 3, 12)),  # 기말 150
    R(2026, "Q1", 120, D(2026, 3, 31), D(2026, 5, 15)),
    R(2026, "H1", 260, D(2026, 6, 30), D(2026, 8, 14)),   # 2분기분 140
]


def test_all_four_report_types_shown_separately():
    yrs = build_fiscal_years(D_QTR, [])
    e = expected_dps_asof(D(2026, 9, 1), yrs)   # 올해 반기까지 확정, 3분기는 아직
    rows = [(c.fiscal_year, c.kind, round(c.dps, 2), c.confirmed_date) for c in e.components]
    assert rows == [
        (2026, "interim", 120.0, D(2026, 5, 15)),   # 올해 1분기
        (2026, "interim", 140.0, D(2026, 8, 14)),   # 올해 반기(2분기분)
        (2025, "interim", 150.0, D(2025, 11, 14)),  # 전년도 3분기(미확정분 대체)
        (2025, "final", 150.0, D(2026, 3, 12)),     # 전년도 기말
    ]
    assert e.value == pytest.approx(120 + 140 + 150 + 150)
    assert "prior_year_unconfirmed_periods_filled" in e.flags


def test_only_q1_confirmed_still_breaks_down_prior_year_by_report():
    yrs = build_fiscal_years(D_QTR, [])
    e = expected_dps_asof(D(2026, 6, 1), yrs)   # 올해 1분기만 확정
    rows = [(c.fiscal_year, c.kind, round(c.dps, 2)) for c in e.components]
    # 올해 1분기 + (전년도 반기분 대체 + 전년도 3분기분 대체, 따로) + 전년도 기말: 1·2·3분기·결산이 모두 한 줄씩
    assert rows == [
        (2026, "interim", 120.0),
        (2025, "interim", 150.0),  # 전년도 2분기분(반기 누계 250 - 1분기 누계 100)
        (2025, "interim", 150.0),  # 전년도 3분기분(3분기 누계 400 - 반기 누계 250)
        (2025, "final", 150.0),
    ]
    assert e.value == pytest.approx(120 + 150 + 150 + 150)


def test_zero_increment_period_is_not_shown():
    """분기 보고서가 있어도 그 분기에 새로 확정된 배당이 없으면(증분 0) 줄을 만들지 않는다."""
    rows = [
        R(2025, "H1", 300, D(2025, 6, 30), D(2025, 8, 14)),
        R(2025, "Q3", 300, D(2025, 9, 30), D(2025, 11, 14)),   # 3분기분 0원
        R(2025, "FY", 1000, D(2025, 12, 31), D(2026, 3, 16)),
    ]
    yrs = build_fiscal_years(rows, [])
    e = expected_dps_asof(D(2026, 9, 1), yrs)
    assert [c.kind for c in e.components] == ["interim", "final"]
    assert e.components[0].dps == pytest.approx(300)

"""'현금·현물배당결정' 수시공시 선반영 테스트.

정기보고서(분기·반기·사업보고서)는 배당 결정 후 45~90일 뒤에 나온다. 그 사이엔 이 수시공시가
이미 실제 배당금을 공개하고 있는데도 기존 코드는 이를 무시하고 정기보고서만 기다렸다(예:
KT&G가 8월에 분기배당 2,000원을 결정해도 11월 3분기보고서가 나오기 전까지는 화면이 전년도
결산 배당만 기준으로 계산했다). 이 파일은 그 선반영 로직을 검증한다.

HTML 샘플은 실제 KT&G 공시(2026-08-06, rcept_no 20260806800319)에서 확인한 금융위 표준
서식(현금·현물배당결정)의 항목 번호·구조를 그대로 쓴, 축약된 버전이다.
"""
from datetime import date as D

import pytest

from pipeline.cache import _provisional_reports, to_reports
from pipeline.dart import fetch_dividend_announcements, parse_dvd_decision
from pipeline.engine import DividendReport, build_fiscal_years, expected_dps_asof


def _span(label: str, value: str) -> str:
    return f'<td><span>{label}</span></td><td><span class="xforms_input">{value}</span></td>'


def _dvd_html(*, kind="현금배당", common="2,000", basis="2026-08-21") -> str:
    return f"""<html><body><table>
    <tr>{_span("1. 배당구분", "분기배당")}</tr>
    <tr>{_span("2. 배당종류", kind)}</tr>
    <tr><td rowspan="2"><span>3. 1주당 배당금(원)</span></td>
        <td><span>보통주식</span></td><td><span class="xforms_input">{common}</span></td></tr>
    <tr><td><span>종류주식</span></td><td><span class="xforms_input">-</span></td></tr>
    <tr>{_span("6. 배당기준일", basis)}</tr>
    <tr>{_span("10. 이사회결의일(결정일)", "2026-08-06")}</tr>
    </table></body></html>"""


# ------------------------------------------------------------------ HTML 파싱
def test_parse_dvd_decision_reads_common_share_amount_and_basis_date():
    r = parse_dvd_decision(_dvd_html())
    assert r == {"amount": 2000.0, "basis_date": "2026-08-21"}


def test_parse_dvd_decision_ignores_in_kind_dividend():
    assert parse_dvd_decision(_dvd_html(kind="현물배당")) is None


def test_parse_dvd_decision_ignores_zero_or_missing_amount():
    assert parse_dvd_decision(_dvd_html(common="-")) is None
    assert parse_dvd_decision("<html><body>표 없음</body></html>") is None


# ------------------------------------------------------------------ 공시 목록 조회 (가짜 DartClient)
class FakeDartClient:
    def __init__(self, list_rows, docs):
        self.list_rows, self.docs, self.calls = list_rows, docs, []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "list.json":
            return _Resp(json_body={"status": "000", "list": self.list_rows})
        if path == "document.xml":
            return _Resp(zip_body={f'{params["rcept_no"]}.xml': self.docs[params["rcept_no"]]})
        raise AssertionError(path)


class _Resp:
    def __init__(self, json_body=None, zip_body=None):
        self._json = json_body
        if zip_body:
            import io
            import zipfile
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                for name, text in zip_body.items():
                    zf.writestr(name, text)
            self.content = buf.getvalue()

    def json(self):
        return self._json


def test_fetch_dividend_announcements_only_fetches_dvd_decision_docs():
    rows = [
        {"rcept_no": "20260806800319", "rcept_dt": "20260806", "report_nm": "현금ㆍ현물배당결정              "},
        {"rcept_no": "20260806800311", "rcept_dt": "20260806",
         "report_nm": "현금ㆍ현물배당을위한주주명부폐쇄(기준일)결정              "},
        {"rcept_no": "20260922000256", "rcept_dt": "20260922", "report_nm": "주요사항보고서(자기주식취득결정)"},
    ]
    client = FakeDartClient(rows, {"20260806800319": _dvd_html()})
    new, log = fetch_dividend_announcements("00244455", 2026, {}, today=D(2026, 9, 27), client=client)
    assert [c[0] for c in client.calls] == ["list.json", "document.xml"]   # 배당결정 공시만 원문 조회
    assert new == [{"rcept_no": "20260806800319", "confirmed_date": "2026-08-06",
                    "basis_date": "2026-08-21", "amount": 2000.0}]
    # log 자체가 파싱 결과(금액 등)를 들고 있어야 한다 — cache._provisional_reports가
    # 원문을 다시 열지 않고 div["announcements"](=이 log)에서 바로 읽는다.
    assert log["20260806800319"] == {**new[0], "status": "ok", "fetched_at": log["20260806800319"]["fetched_at"],
                                     "report_nm": rows[0]["report_nm"]}


def test_fetch_dividend_announcements_skips_already_logged():
    rows = [{"rcept_no": "20260806800319", "rcept_dt": "20260806", "report_nm": "현금ㆍ현물배당결정"}]
    client = FakeDartClient(rows, {"20260806800319": _dvd_html()})
    log = {"20260806800319": {"status": "ok", "fetched_at": "2026-08-07T00:00:00+09:00",
                              "confirmed_date": "2026-08-06", "basis_date": "2026-08-21",
                              "amount": 2000.0, "rcept_no": "20260806800319"}}
    new, log2 = fetch_dividend_announcements("00244455", 2026, log, today=D(2026, 9, 27), client=client)
    assert new == [] and [c[0] for c in client.calls] == ["list.json"]   # 원문 재조회 없음
    # 제목만 보강한다(자회사 공시 판별용). 금액 등 파싱 결과는 그대로.
    assert log2 == {k: {**v, "report_nm": "현금ㆍ현물배당결정"} for k, v in log.items()}


def test_fetch_dividend_announcements_refetches_legacy_incomplete_log_entry():
    """status="ok"인데 금액이 없는 예전 기록(마이그레이션 이전 버전이 남긴 것)은 다시 읽는다."""
    rows = [{"rcept_no": "20260806800319", "rcept_dt": "20260806", "report_nm": "현금ㆍ현물배당결정"}]
    client = FakeDartClient(rows, {"20260806800319": _dvd_html()})
    legacy_log = {"20260806800319": {"status": "ok", "fetched_at": "2026-08-07T00:00:00+09:00"}}
    new, log2 = fetch_dividend_announcements("00244455", 2026, legacy_log, today=D(2026, 9, 27), client=client)
    assert new == [{"rcept_no": "20260806800319", "confirmed_date": "2026-08-06",
                    "basis_date": "2026-08-21", "amount": 2000.0}]
    assert log2["20260806800319"]["amount"] == 2000.0


# ------------------------------------------------------------------ 캐시 병합(_provisional_reports)
def _periodic(fy, period, cum, confirmed):
    return {"fiscal_year": fy, "period": period, "cum_dps": cum, "confirmed_date": confirmed,
           "basis_date": None, "rcept_no": f"{confirmed.replace('-', '')}000001"}


def test_provisional_report_adds_amount_on_top_of_periodic_cumulative():
    div = {
        "reports": [_periodic(2026, "Q1", 0, "2026-05-15"), _periodic(2026, "H1", 0, "2026-08-14")],
        "announcements": {"20260806800319": {"status": "ok", "confirmed_date": "2026-08-06",
                                             "basis_date": "2026-08-21", "amount": 2000.0,
                                             "rcept_no": "20260806800319"}},
    }
    out = _provisional_reports(div)
    assert len(out) == 1
    p = out[0]
    # 8/6 결정분이 8/14 반기보고서엔 0 → 이 회사는 이런 배당을 다음(3분기) 보고서에 싣는다.
    assert (p.fiscal_year, p.period, p.cum_dps) == (2026, "PROV_Q3", 2000.0)  # 0(직전 누계) + 2000
    assert p.confirmed_date == D(2026, 8, 6) and p.basis_date == D(2026, 8, 21)


def test_provisional_report_stacks_on_existing_nonzero_cumulative():
    div = {
        "reports": [_periodic(2026, "Q1", 300, "2026-05-15")],
        "announcements": {"r1": {"status": "ok", "confirmed_date": "2026-08-06",
                                "basis_date": "2026-08-21", "amount": 200.0, "rcept_no": "r1"}},
    }
    assert _provisional_reports(div)[0].cum_dps == pytest.approx(500.0)  # 300(1분기) + 200


def test_provisional_report_dropped_once_periodic_report_supersedes_it():
    """수시공시 이후(또는 같은 날) 확정된 정기보고서가 이미 있으면 그 정기보고서가 반영한 것으로 본다."""
    div = {
        "reports": [_periodic(2026, "Q3", 2000, "2026-11-14")],  # 수시공시보다 늦게 확정된 정기보고서
        "announcements": {"r1": {"status": "ok", "confirmed_date": "2026-08-06",
                                "basis_date": "2026-08-21", "amount": 2000.0, "rcept_no": "r1"}},
    }
    assert _provisional_reports(div) == []


def test_provisional_report_kept_when_periodic_report_covers_period_but_shows_zero():
    """서호전기(065710) 사고: 반기보고서(결산기준일 6/30)가 중간배당 공시(배당기준일도 6/30) 이틀
    뒤에 나왔지만, 그 표의 누계가 아직 0이다(공시 금액이 반영 안 됨). 결산기준일만 보고 '반영됨'으로
    치면 잠정치가 사라지고 그 해가 통째로 화면에서 안 보인다 — 표의 누계가 실제로 공시 금액만큼
    늘어났을 때만 '반영됨'으로 봐야 한다."""
    div = {
        "reports": [_periodic(2026, "Q1", 0, "2026-05-15"),
                   _periodic(2026, "H1", 0, "2026-08-14")],  # 결산기준일 6/30, 누계 여전히 0
        "announcements": {"r1": {"status": "ok", "confirmed_date": "2026-08-12",
                                "basis_date": "2026-06-30", "amount": 2000.0, "rcept_no": "r1"}},
    }
    out = _provisional_reports(div)
    assert len(out) == 1
    assert (out[0].fiscal_year, out[0].period, out[0].cum_dps) == (2026, "PROV_Q3", 2000.0)


def test_year_end_dividend_announced_next_year_is_not_current_year_provisional():
    """서호전기(065710): 2025년 기말배당 공시는 2026-02-27에 접수된다. 접수일 연도(2026)로
    분류하면 올해 중간배당 잠정치와 뒤섞인다 — 1~3월 결정분은 직전 사업연도 결산배당이다."""
    div = {
        "reports": [_periodic(2025, "FY", 6000, "2026-03-19")],   # 2025년 사업보고서로 이미 확정
        "announcements": {
            "fy2025": {"status": "ok", "confirmed_date": "2026-02-27",
                      "basis_date": "2025-12-31", "amount": 4000.0, "rcept_no": "fy2025"},
        },
    }
    assert _provisional_reports(div) == []


def test_to_reports_includes_provisional_alongside_periodic():
    div = {
        "reports": [_periodic(2025, "FY", 6000, "2026-03-18"), _periodic(2026, "Q1", 0, "2026-05-15")],
        "announcements": {"r1": {"status": "ok", "confirmed_date": "2026-08-06",
                                "basis_date": "2026-08-21", "amount": 2000.0, "rcept_no": "r1"}},
    }
    periods = {(r.fiscal_year, r.period) for r in to_reports(div)}
    assert periods == {(2025, "FY"), (2026, "Q1"), (2026, "PROV_H1")}


# ------------------------------------------------------------------ 정정공시로 밀린 접수일
def test_corrected_reports_count_from_filing_deadline_not_correction_date():
    """현대차(005380) 원 데이터: 2015~2020 사업보고서가 2022-02-17 정정본 접수번호로만 남아,
    2015~2022년 2월 시가배당률이 통째로 비고 그 뒤 2년간은 이미 공시된 배당 대신 2020년 값을 썼다."""
    div = {"reports": [_periodic(y, "FY", v, "2022-02-17") for y, v in
                       ((2015, 4000), (2016, 4000), (2017, 4000), (2018, 4000), (2019, 4000), (2020, 3000))]
               + [_periodic(2021, "FY", 5000, "2024-03-14"), _periodic(2022, "FY", 7000, "2024-03-14")]}
    fy = {r.fiscal_year: r.confirmed_date for r in to_reports(div) if r.period == "FY"}
    assert fy[2015] == D(2016, 3, 30)          # 결산일 + 90일
    assert fy[2021] == D(2022, 3, 31)
    years = build_fiscal_years(to_reports(div), [])
    assert expected_dps_asof(D(2016, 6, 1), years).value == 4000
    assert expected_dps_asof(D(2022, 6, 1), years).value == 5000
    assert expected_dps_asof(D(2023, 6, 1), years).value == 7000


def test_reports_filed_on_time_keep_their_own_date():
    div = {"reports": [_periodic(2025, "FY", 6000, "2026-03-18"), _periodic(2026, "Q1", 300, "2026-05-15")]}
    got = {(r.fiscal_year, r.period): r.confirmed_date for r in to_reports(div)}
    assert got == {(2025, "FY"): D(2026, 3, 18), (2026, "Q1"): D(2026, 5, 15)}


def test_corrected_interim_report_capped_at_sixty_days():
    div = {"reports": [_periodic(2024, "H1", 500, "2025-01-20")]}
    assert to_reports(div)[0].confirmed_date == D(2024, 8, 29)   # 6/30 + 60일


# ------------------------------------------------------------------ 같은 해 공시가 여럿일 때 (원 데이터 기반)
def _ann(conf, basis, amount, rno=None):
    return {"status": "ok", "confirmed_date": conf, "basis_date": basis, "amount": amount,
            "rcept_no": rno or f"{conf.replace('-', '')}{int(amount)}"}


def _anns(*items):
    # DART 목록처럼 최신 공시가 앞에 오도록 넣는다 — 예전 코드는 이 순서 때문에 가장 오래된 공시가 남았다.
    return {a["rcept_no"]: a for a in sorted(items, key=lambda a: a["confirmed_date"], reverse=True)}


def test_gwangju_shinsegae_uses_this_years_dividends_not_last_years_final():
    """광주신세계(037710) 원 데이터. 2/11 공시(2,400원, 기준일 3/31)는 2025년 결산배당(배당절차 개선으로
    기준일이 결정 뒤)이고, 5/13·8/14 공시(각 600원)는 올해 1분기·반기 배당으로 정기보고서에 이미 실렸다.
    예전에는 셋 다 2026년 'PROV' 하나로 겹쳐 가장 오래된 2,400원이 남아 올해 누계가 2,400원이 됐다."""
    div = {
        "reports": [_periodic(2025, "Q1", 0, "2025-05-15"), _periodic(2025, "H1", 0, "2025-08-14"),
                    _periodic(2025, "Q3", 0, "2025-11-14"), _periodic(2025, "FY", 2400, "2026-03-16"),
                    _periodic(2026, "Q1", 600, "2026-05-15"), _periodic(2026, "H1", 1200, "2026-08-14")],
        "announcements": _anns(_ann("2026-02-11", "2026-03-31", 2400.0), _ann("2026-05-13", "2026-05-29", 600.0),
                               _ann("2026-08-14", "2026-08-31", 600.0)),
    }
    assert _provisional_reports(div) == []
    years = build_fiscal_years(to_reports(div), [])
    assert years[2026].interim_cum_asof(D(2026, 9, 28))[0] == 1200.0
    assert expected_dps_asof(D(2026, 9, 28), years).value == pytest.approx(1200 + 2400)


def test_uncovered_announcements_stack_in_period_order():
    """정기보고서가 아직 없는 두 공시(1분기 600, 반기 600)는 누계로 쌓이고, 더 늦은 기간이 최신이다
    (예전에는 공시마다 따로 600으로 계산돼 목록 순서에 따라 하나만 남았다)."""
    div = {"reports": [_periodic(2025, "FY", 2400, "2026-03-23")],
           "announcements": _anns(_ann("2026-05-12", "2026-05-27", 600.0), _ann("2026-07-14", "2026-07-29", 600.0))}
    out = [(r.period, r.cum_dps) for r in _provisional_reports(div)]
    assert out == [("PROV_Q1", 600.0), ("PROV_H1", 1200.0)]
    years = build_fiscal_years(to_reports(div), [])
    cum, rec = years[2026].interim_cum_asof(D(2026, 9, 28))
    assert (cum, rec[0]) == (1200.0, "PROV_H1")
    assert years[2026].interim_cum_asof(D(2026, 6, 1))[0] == 600.0   # 7/14 공시 전에는 1분기분만


def test_same_period_alternatives_dropped_once_periodic_report_reflects_one():
    """하나금융(086790): 같은 반기에 559.48·886.26·1,155원 공시. 반기보고서(2,300 = 1,145+1,155)가 그중
    하나를 이미 반영했으면 그 기간은 정기보고서를 믿는다(예전에는 나머지가 잠정치로 남아 💣가 붙었다)."""
    div = {
        "reports": [_periodic(2026, "Q1", 1145, "2026-05-15"), _periodic(2026, "H1", 2300, "2026-08-14")],
        "announcements": _anns(_ann("2026-04-24", "2026-05-11", 1145.0), _ann("2026-07-23", "2026-08-10", 559.48),
                               _ann("2026-07-23", "2026-08-06", 886.26), _ann("2026-07-24", "2026-08-10", 1155.0)),
    }
    assert _provisional_reports(div) == []


def test_re_announcement_of_year_end_dividend_is_not_this_years_q1():
    """HK이노엔(195940)·SKC(069960): 결산배당을 2~3월에 공시하고 4월에 같은 기준일·금액으로 다시 공시했다.
    두 번째 공시만 보면 '4월 결정·기준일 3/31 → 1분기'로 오인되므로, 같은 기준일·금액은 가장 이른
    결정일 하나로 합친다."""
    div = {"reports": [_periodic(2025, "FY", 410, "2026-03-18"), _periodic(2026, "Q1", 0, "2026-05-14"),
                       _periodic(2026, "H1", 0, "2026-08-14")],
           "announcements": _anns(_ann("2026-03-10", "2026-03-31", 410.0), _ann("2026-04-24", "2026-03-31", 410.0))}
    assert _provisional_reports(div) == []


def test_lagging_reporter_provisional_is_placed_in_next_period_so_fill_is_not_doubled():
    """SKC(069960): 작년엔 반기보고서 0, 3분기보고서 500(8월 결정분이 3분기에 실림). 올해 8/5 결정분
    500원도 반기보고서엔 0이다. 이를 반기 잠정치로 두면 전년도 3분기 500원까지 '미확정분 대체'로
    더해져 이중 계산된다(2,650) — 3분기 잠정치로 둬야 작년 합계와 같은 2,150이 된다."""
    div = {
        "reports": [_periodic(2025, "Q1", 0, "2025-05-15"), _periodic(2025, "H1", 0, "2025-08-14"),
                    _periodic(2025, "Q3", 500, "2025-11-14"), _periodic(2025, "FY", 2150, "2026-03-18"),
                    _periodic(2026, "Q1", 0, "2026-05-15"), _periodic(2026, "H1", 0, "2026-08-14")],
        "announcements": _anns(_ann("2026-02-11", "2026-04-03", 1650.0), _ann("2026-08-05", "2026-09-30", 500.0)),
    }
    assert [(r.period, r.cum_dps) for r in _provisional_reports(div)] == [("PROV_Q3", 500.0)]
    e = expected_dps_asof(D(2026, 9, 28), build_fiscal_years(to_reports(div), []))
    assert e.value == pytest.approx(500 + 1650)


def test_lag_predicted_from_last_year_before_this_years_report_arrives():
    """아직 반기보고서가 안 나온 시점이라도, 작년에 같은 기간 보고서엔 없고 다음 기간에 실렸다면
    처음부터 다음 기간 잠정치로 둔다(8/6~8/14 사이에 잠깐 이중 계산되는 것을 막는다)."""
    div = {"reports": [_periodic(2025, "H1", 0, "2025-08-14"), _periodic(2025, "Q3", 1400, "2025-11-14"),
                       _periodic(2025, "FY", 6000, "2026-03-18"), _periodic(2026, "Q1", 0, "2026-05-15")],
           "announcements": _anns(_ann("2026-08-06", "2026-08-21", 2000.0))}
    assert [(r.period, r.cum_dps) for r in _provisional_reports(div)] == [("PROV_Q3", 2000.0)]


def test_later_periodic_report_outranks_older_uncovered_provisional():
    """잠정치는 무조건 최신이 아니다: 그 뒤 기간의 정기보고서가 나오면 그 보고서를 쓴다."""
    years = build_fiscal_years([
        DividendReport(2026, "PROV_H1", 1200.0, D(2026, 7, 29), D(2026, 7, 14), "p"),
        DividendReport(2026, "Q3", 1500.0, D(2026, 9, 30), D(2026, 11, 14), "q3"),
    ], [])
    assert years[2026].interim_cum_asof(D(2026, 10, 1))[1][0] == "PROV_H1"
    assert years[2026].interim_cum_asof(D(2026, 11, 20))[1][0] == "Q3"


def test_subsidiary_dividend_filings_are_excluded():
    """CJ(001040)가 공시한 CJ제일제당 분기배당처럼 지주회사가 대신 낸 자회사 배당결정은 제외하고,
    예전에 'ok'로 받아 둔 기록도 덮어쓴다."""
    rows = [{"rcept_no": "20260810800001", "report_nm": "현금ㆍ현물배당결정(자회사의 주요경영사항)"},
            {"rcept_no": "20260209800001", "report_nm": "현금ㆍ현물배당결정"}]
    client = FakeDartClient(rows, {"20260209800001": _dvd_html()})
    old = {"20260810800001": {"status": "ok", "fetched_at": "x", "confirmed_date": "2026-08-10",
                              "basis_date": "2026-08-31", "amount": 1500.0, "rcept_no": "20260810800001"}}
    new, log = fetch_dividend_announcements("00258801", 2026, old, today=D(2026, 9, 27), client=client)
    assert log["20260810800001"]["status"] == "subsidiary"
    assert [c[1].get("rcept_no") for c in client.calls if c[0] == "document.xml"] == ["20260209800001"]
    assert all(r.ref != "20260810800001" for r in _provisional_reports({"reports": [], "announcements": log}))


# ------------------------------------------------------------------ engine.expected_dps_asof + PROV
def test_expected_dps_uses_provisional_announcement_before_periodic_report():
    """KT&G와 같은 상황: 올해 1분기·반기는 0(정기보고서 확정), 8월에 분기배당 2,000원을
    수시공시로 결정(아직 3분기보고서 전) → 예상 DPS에 이 2,000원이 반영돼야 한다."""
    div = {
        "reports": [
            _periodic(2025, "Q3", 1400, "2025-11-14"),
            _periodic(2025, "FY", 6000, "2026-03-18"),
            _periodic(2026, "Q1", 0, "2026-05-15"),
            _periodic(2026, "H1", 0, "2026-08-14"),
        ],
        "announcements": {"r1": {"status": "ok", "confirmed_date": "2026-08-06",
                                "basis_date": "2026-08-21", "amount": 2000.0, "rcept_no": "r1"}},
    }
    years = build_fiscal_years(to_reports(div), [])
    e = expected_dps_asof(D(2026, 9, 27), years)
    assert e.value == pytest.approx(2000 + 4600)             # 올해 잠정 2,000 + 전년도 기말 4,600
    assert "provisional_dividend_used" in e.flags
    kinds = [(c.kind, round(c.dps, 2)) for c in e.components]
    assert kinds == [("provisional", 2000.0), ("final", 4600.0)]


def test_expected_dps_prefers_periodic_report_once_it_arrives():
    """3분기보고서가 나오면(같은 공시 내용을 확정) 잠정치는 사라지고 정기보고서 값을 쓴다."""
    div = {
        "reports": [
            _periodic(2025, "Q3", 1400, "2025-11-14"), _periodic(2025, "FY", 6000, "2026-03-18"),
            _periodic(2026, "Q1", 0, "2026-05-15"), _periodic(2026, "H1", 0, "2026-08-14"),
            _periodic(2026, "Q3", 2000, "2026-11-16"),   # 정기보고서 확정
        ],
        "announcements": {"r1": {"status": "ok", "confirmed_date": "2026-08-06",
                                "basis_date": "2026-08-21", "amount": 2000.0, "rcept_no": "r1"}},
    }
    years = build_fiscal_years(to_reports(div), [])
    e = expected_dps_asof(D(2026, 11, 20), years)
    assert e.value == pytest.approx(2000 + 4600)
    assert "provisional_dividend_used" not in e.flags
    assert [c.kind for c in e.components] == ["interim", "final"]   # PROV 아님, 정상 3분기 확정


# ------------------------------------------------------------------ analyze_stock 통합
def test_analyze_stock_reflects_provisional_announcement(tmp_data, monkeypatch):
    import pandas as pd

    from pipeline import config as C
    from pipeline.cache import analyze_stock
    _D = D

    class FakeKrx:
        def __call__(self, code, start, end):
            days = pd.bdate_range(start, min(_D.fromisoformat(end), _D(2026, 9, 25)))
            return pd.DataFrame({"date": days.date.astype(str), "close": 100000.0, "change_pct": 0.0,
                                 "trading_value": 1e9})

    class FakeDart:
        def __call__(self, corp_code, start_year, end_year, log, today=None):
            new, log = [], dict(log)
            base = [(2025, "FY", 6000.0, "2026-03-18"), (2025, "Q3", 1400.0, "2025-11-14"),
                   (2026, "Q1", 0.0, "2026-05-15"), (2026, "H1", 0.0, "2026-08-14")]
            for y, p, cum, conf in base:
                key = f"{y}-{p}"
                if key in log:
                    continue
                log[key] = {"status": "ok", "fetched_at": f"{conf}T20:00:00+09:00"}
                new.append({"fiscal_year": y, "period": p, "cum_dps": cum, "confirmed_date": conf,
                           "basis_date": None, "rcept_no": conf.replace("-", "") + "000001"})
            return new, log

    def fake_announcements(corp_code, year, log, today=None, client=None):
        if "r1" in log:
            return [], log
        log = {**log, "r1": {"status": "ok", "confirmed_date": "2026-08-06",
                             "basis_date": "2026-08-21", "amount": 2000.0, "rcept_no": "r1"}}
        return [{"rcept_no": "r1", "confirmed_date": "2026-08-06", "basis_date": "2026-08-21",
                 "amount": 2000.0}], log

    monkeypatch.setattr(C, "REFRESH_COOLDOWN_SEC", 0)
    r = analyze_stock("005930", master={"005930": {"code": "005930", "name": "삼성전자", "market": "KOSPI",
                                                    "corp_code": "00126380"}},
                      today=_D(2026, 9, 27), fetch_prices=FakeKrx(), fetch_dividends=FakeDart(),
                      fetch_announcements=fake_announcements, update_us=False)
    assert r["summary"]["dps"] == pytest.approx(6600.0)
    assert any("이사회 결정" in c["label"] or c["confirmed"] == "2026-08-06" for c in r["components"])

    # 판별 규칙이 바뀌어(예: 자회사 공시 제외) 이 공시가 빠지면, 종목별 새로고침 없이 일괄 재확인으로 반영된다.
    from pipeline.cache import refresh_announcements_cached

    def drop_as_subsidiary(corp_code, year, log, today=None, client=None):
        return [], {**log, "r1": {"status": "subsidiary", "fetched_at": "x", "report_nm": "자회사"}}

    master = {"005930": {"code": "005930", "name": "삼성전자", "market": "KOSPI", "corp_code": "00126380"}}
    res = refresh_announcements_cached(master, today=_D(2026, 9, 27), fetch=drop_as_subsidiary)
    assert res["refreshed"] == 1 and res["failed"] is None
    import json
    a = json.loads((C.CACHE_DIR / "005930" / "analysis.json").read_text())
    assert a["summary"]["dps"] == pytest.approx(6000.0)   # 잠정 2,000이 빠지고 전년도 연간 DPS로

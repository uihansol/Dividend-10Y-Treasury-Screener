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
from pipeline.engine import build_fiscal_years, expected_dps_asof


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
    assert log["20260806800319"] == {**new[0], "status": "ok", "fetched_at": log["20260806800319"]["fetched_at"]}


def test_fetch_dividend_announcements_skips_already_logged():
    rows = [{"rcept_no": "20260806800319", "rcept_dt": "20260806", "report_nm": "현금ㆍ현물배당결정"}]
    client = FakeDartClient(rows, {"20260806800319": _dvd_html()})
    log = {"20260806800319": {"status": "ok", "fetched_at": "2026-08-07T00:00:00+09:00"}}
    new, log2 = fetch_dividend_announcements("00244455", 2026, log, today=D(2026, 9, 27), client=client)
    assert new == [] and [c[0] for c in client.calls] == ["list.json"]   # 원문 재조회 없음
    assert log2 == log


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
    assert (p.fiscal_year, p.period, p.cum_dps) == (2026, "PROV", 2000.0)  # 0(직전 누계) + 2000
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


def test_to_reports_includes_provisional_alongside_periodic():
    div = {
        "reports": [_periodic(2025, "FY", 6000, "2026-03-18"), _periodic(2026, "Q1", 0, "2026-05-15")],
        "announcements": {"r1": {"status": "ok", "confirmed_date": "2026-08-06",
                                "basis_date": "2026-08-21", "amount": 2000.0, "rcept_no": "r1"}},
    }
    periods = {(r.fiscal_year, r.period) for r in to_reports(div)}
    assert periods == {(2025, "FY"), (2026, "Q1"), (2026, "PROV")}


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

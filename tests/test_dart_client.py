"""DartClient.get()의 연결 오류 재시도. 네트워크 없이 세션을 가짜로 바꿔 테스트한다.

남화산업(111710) 사고: alotMatter 정기보고서 조회(한 종목 최초 조회 시 최대 약 48회 순차 호출) 중
한 번만 ConnectTimeout이 나도 재시도가 없어 배당 이력 전체가 빠졌다."""
import pytest
import requests

from pipeline.dart import DART_RETRIES, DartClient


class _Resp:
    def __init__(self, status=200, json_data=None):
        self.status_code = status
        self._json = json_data or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def json(self):
        return self._json


class _FlakySession:
    """처음 fail_times번은 연결 타임아웃, 그다음엔 성공하는 가짜 세션."""
    def __init__(self, fail_times: int):
        self.fail_times, self.calls = fail_times, 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise requests.exceptions.ConnectTimeout("boom")
        return _Resp(200, {"status": "000"})


class _AlwaysBadStatusSession:
    def __init__(self):
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        return _Resp(500)


def _client(monkeypatch, session):
    client = DartClient(key="testkey")
    client.s = session
    monkeypatch.setattr("pipeline.dart.time.sleep", lambda *_: None)
    return client


def test_get_retries_transient_connection_error_then_succeeds(monkeypatch):
    session = _FlakySession(fail_times=DART_RETRIES - 1)
    client = _client(monkeypatch, session)
    r = client.get("alotMatter.json", corp_code="00245694", bsns_year="2024", reprt_code="11011")
    assert r.json()["status"] == "000"
    assert session.calls == DART_RETRIES


def test_get_raises_after_exhausting_retries(monkeypatch):
    session = _FlakySession(fail_times=DART_RETRIES + 5)
    client = _client(monkeypatch, session)
    with pytest.raises(requests.exceptions.ConnectTimeout):
        client.get("alotMatter.json", corp_code="00245694", bsns_year="2024", reprt_code="11011")
    assert session.calls == DART_RETRIES


def test_get_does_not_retry_http_status_error(monkeypatch):
    """연결 자체는 됐지만 4xx/5xx 응답이면(예: DART 서버 오류) 재시도하지 않는다 —
    연결 단계 오류만 재시도 대상이다."""
    session = _AlwaysBadStatusSession()
    client = _client(monkeypatch, session)
    with pytest.raises(requests.exceptions.HTTPError):
        client.get("alotMatter.json", corp_code="00245694", bsns_year="2024", reprt_code="11011")
    assert session.calls == 1

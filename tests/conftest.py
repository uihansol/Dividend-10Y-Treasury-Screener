"""여러 테스트 파일이 함께 쓰는 fixture."""
import pandas as pd
import pytest

from pipeline import config as C


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """캐시·공통데이터 경로를 임시 디렉터리로 돌리고, 네트워크가 필요한 기본 조회 함수(수시공시
    확인)는 '아무것도 없음'으로 응답하게 해 둔다. 각 테스트는 필요하면 이 기본값을 덮어쓴다."""
    monkeypatch.setattr(C, "CACHE_DIR", tmp_path / "cache" / "stocks")
    monkeypatch.setattr(C, "CACHE_INDEX_JSON", tmp_path / "cache" / "index.json")
    monkeypatch.setattr(C, "SOURCES_JSON", tmp_path / "sources.json")
    monkeypatch.setattr(C, "US10Y_CSV", tmp_path / "us10y" / "dgs10.csv")
    monkeypatch.setattr(C, "CORP_ACTIONS_OVERRIDE_CSV", tmp_path / "none.csv")
    (tmp_path / "us10y").mkdir()
    days = pd.bdate_range("2016-01-01", "2026-09-25")
    pd.DataFrame({"date": days.date.astype(str), "us10y": 3.0}).to_csv(C.US10Y_CSV, index=False)
    monkeypatch.setattr("pipeline.dart.fetch_dividend_announcements",
                        lambda corp_code, year, log, today=None, client=None: ([], log))
    return tmp_path

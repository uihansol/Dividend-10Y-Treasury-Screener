"""미국 10년물(FRED DGS10) 수집.

1) data/us10y.csv(과거 데이터) 읽기 → 2) 최신 값 요청 → 3) 성공 시 새 날짜만 추가
4) 실패하면 기존 파일 그대로 두고 sources.json에 오류만 기록 (사이트는 기존 데이터로 동작)

- FRED_API_KEY가 있으면 공식 API(series/observations)로 마지막 저장일 이후만 요청
- 없으면 FRED 그래프 CSV 다운로드(키 불필요)를 받아 새 날짜만 병합 (DGS10 전체 약 300KB)
- 값이 '.'(결측)인 날은 저장하지 않는다. 0으로 채우지 않는다.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

import pandas as pd
import requests

from . import config as C
from .store import mark_source

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10"
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"


def load_us10y() -> pd.DataFrame:
    if not C.US10Y_CSV.exists():
        return pd.DataFrame(columns=["date", "us10y"])
    df = pd.read_csv(C.US10Y_CSV)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.dropna().sort_values("date")


def _fetch_api(start: date) -> pd.DataFrame:
    r = requests.get(FRED_API_URL, params={
        "series_id": "DGS10", "api_key": C.FRED_API_KEY, "file_type": "json",
        "observation_start": start.isoformat(),
    }, timeout=30)
    r.raise_for_status()
    obs = r.json().get("observations", [])
    df = pd.DataFrame([(o["date"], o["value"]) for o in obs], columns=["date", "us10y"])
    return df


def _fetch_csv() -> pd.DataFrame:
    r = requests.get(FRED_CSV_URL, timeout=60, headers={"User-Agent": "kr-div-us10y/1.0"})
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    # 첫 열이 날짜(열 이름이 DATE 또는 observation_date로 바뀐 적이 있어 위치로 읽음)
    df = df.iloc[:, :2]
    df.columns = ["date", "us10y"]
    return df


def update_us10y() -> dict:
    old = load_us10y()
    last = old["date"].max() if len(old) else None
    try:
        if C.FRED_API_KEY:
            start = (last - timedelta(days=7)) if last else date(2015, 1, 1)
            new = _fetch_api(start)
        else:
            new = _fetch_csv()
        new["us10y"] = pd.to_numeric(new["us10y"], errors="coerce")  # '.' → NaN
        new = new.dropna()
        new["date"] = pd.to_datetime(new["date"]).dt.date
        new = new[new["date"] >= date(2015, 1, 1)]
        if last is not None:
            new = new[new["date"] > last]
        merged = pd.concat([old, new], ignore_index=True).drop_duplicates("date").sort_values("date")
        C.US10Y_CSV.parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(C.US10Y_CSV, index=False)
        through = merged["date"].max().isoformat() if len(merged) else None
        mark_source("us10y", True, data_through=through, note=f"+{len(new)} rows")
        return {"ok": True, "added": len(new), "through": through}
    except Exception as e:  # 네트워크·형식 오류 모두 기존 데이터 유지
        mark_source("us10y", False, error=repr(e))
        return {"ok": False, "error": repr(e)}

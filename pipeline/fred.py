"""미국 10년물(FRED DGS10) 수집. 모든 종목이 공통으로 쓰는 data/us10y/dgs10.csv 하나만 관리한다.

1) 기존 파일 읽기 → 2) 마지막 저장일 이후만 요청 → 3) 성공 시 새 날짜만 추가
4) 모든 출처가 실패하면 기존 파일 그대로 두고 sources.json에 오류만 기록 (사이트는 기존 데이터로 동작)

출처(앞에서부터 시도)
 - FRED_API_KEY가 있으면 공식 API(series/observations)
 - FRED 그래프 CSV (키 불필요). cosd로 필요한 기간만 받는다.
 - 미 재무부 Daily Treasury Par Yield Curve CSV의 '10 Yr' (DGS10의 원천 데이터, 같은 값)
   GitHub Actions에서 fred.stlouisfed.org 응답이 끊기는 경우가 있어 둔 대체 경로.
- 값이 '.'(결측)인 날은 저장하지 않는다. 0으로 채우지 않는다.
"""
from __future__ import annotations

import io
import time
from datetime import date, timedelta

import pandas as pd
import requests

from . import config as C
from .store import mark_source

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API_URL = "https://api.stlouisfed.org/fred/series/observations"
TREASURY_CSV_URL = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                    "daily-treasury-rates.csv/{year}/all")
FIRST_DATE = date(2015, 1, 1)
UA = {"User-Agent": "Mozilla/5.0 (dividend-10y personal screener)"}


def load_us10y() -> pd.DataFrame:
    if not C.US10Y_CSV.exists():
        return pd.DataFrame(columns=["date", "us10y"])
    df = pd.read_csv(C.US10Y_CSV)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df.dropna().sort_values("date")


def _get(url: str, params: dict, timeout, attempts: int) -> requests.Response:
    last_err = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=timeout, headers=UA)
            r.raise_for_status()
            return r
        except Exception as e:  # pragma: no cover - 네트워크 의존
            last_err = e
            if i + 1 < attempts:
                time.sleep(3 * (i + 1))
    raise last_err


def _fetch_api(start: date, timeout, attempts) -> pd.DataFrame:
    r = _get(FRED_API_URL, {"series_id": "DGS10", "api_key": C.FRED_API_KEY, "file_type": "json",
                            "observation_start": start.isoformat()}, timeout, attempts)
    obs = r.json().get("observations", [])
    return pd.DataFrame([(o["date"], o["value"]) for o in obs], columns=["date", "us10y"])


def parse_fred_csv(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text))
    # 첫 열이 날짜(열 이름이 DATE 또는 observation_date로 바뀐 적이 있어 위치로 읽음)
    df = df.iloc[:, :2]
    df.columns = ["date", "us10y"]
    return df


def _fetch_csv(start: date, timeout, attempts) -> pd.DataFrame:
    r = _get(FRED_CSV_URL, {"id": "DGS10", "cosd": start.isoformat()}, timeout, attempts)
    return parse_fred_csv(r.text)


def parse_treasury_csv(text: str) -> pd.DataFrame:
    """재무부 CSV: Date(MM/DD/YYYY), ..., "10 Yr", ... → date, us10y"""
    df = pd.read_csv(io.StringIO(text))
    col = next(c for c in df.columns if c.replace(" ", "").lower() in ("10yr", "10year"))
    return pd.DataFrame({"date": pd.to_datetime(df.iloc[:, 0], format="%m/%d/%Y").dt.date.astype(str),
                         "us10y": df[col]})


def _fetch_treasury(start: date, timeout, attempts) -> pd.DataFrame:
    frames = []
    for y in range(start.year, date.today().year + 1):
        r = _get(TREASURY_CSV_URL.format(year=y), {"type": "daily_treasury_yield_curve",
                                                    "field_tdr_date_value": str(y), "page": "", "_format": "csv"},
                 timeout, attempts)
        frames.append(parse_treasury_csv(r.text))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["date", "us10y"])


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["us10y"] = pd.to_numeric(df["us10y"], errors="coerce")  # '.' → NaN
    df = df.dropna()
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df[df["date"] >= FIRST_DATE]


def update_us10y(quick: bool = False, sources: list | None = None) -> dict:
    """quick=True: 종목 분석 중 호출용. 출처마다 짧은 timeout으로 한 번씩만 시도한다.
    sources: 테스트용 주입 [(이름, fetch(start, timeout, attempts)), ...]"""
    old = load_us10y()
    last = old["date"].max() if len(old) else None
    start = (last - timedelta(days=7)) if last else FIRST_DATE
    timeout, attempts = ((5, 20), 1) if quick else ((10, 90), 3)
    if sources is None:
        sources = ([("fred_api", _fetch_api)] if C.FRED_API_KEY else []) + \
                  [("fred_csv", _fetch_csv), ("treasury", _fetch_treasury)]
    errors = []
    for name, fetch in sources:
        try:
            new = _clean(fetch(start, timeout, attempts))
        except Exception as e:  # 네트워크·형식 오류 → 다음 출처
            errors.append(f"{name}: {e!r}"[:300])
            continue
        if last is not None:
            new = new[new["date"] > last]
        merged = pd.concat([old, new], ignore_index=True) if len(old) else new
        merged = merged.drop_duplicates("date").sort_values("date")
        C.US10Y_CSV.parent.mkdir(parents=True, exist_ok=True)
        merged.to_csv(C.US10Y_CSV, index=False)
        through = merged["date"].max().isoformat() if len(merged) else None
        mark_source("us10y", True, data_through=through, note=f"{name} +{len(new)} rows")
        return {"ok": True, "source": name, "added": int(len(new)), "through": through,
                "errors": errors or None}
    mark_source("us10y", False, error=" | ".join(errors))
    return {"ok": False, "error": " | ".join(errors)}

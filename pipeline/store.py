"""파일 저장·로드 도우미와 데이터 출처 상태(sources.json) 관리."""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

from . import config as C

KST = timezone(timedelta(hours=9))


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- sources.json
def load_sources() -> dict:
    if C.SOURCES_JSON.exists():
        return json.loads(C.SOURCES_JSON.read_text(encoding="utf-8"))
    return {}


def mark_source(name: str, ok: bool, *, data_through: str | None = None,
                note: str | None = None, error: str | None = None) -> None:
    """업데이트 시도 결과를 기록. 실패해도 마지막 성공 정보는 지우지 않는다."""
    s = load_sources()
    cur = s.get(name, {})
    cur["last_attempt"] = now_kst()
    if ok:
        cur["last_success"] = cur["last_attempt"]
        cur["last_error"] = None
        if data_through:
            cur["data_through"] = data_through
    else:
        cur["last_error"] = (error or "unknown error")[:500]
    if note is not None:
        cur["note"] = note
    s[name] = cur
    C.SOURCES_JSON.parent.mkdir(parents=True, exist_ok=True)
    C.SOURCES_JSON.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- prices
PRICE_COLS = ["date", "stock_code", "market", "close", "change_pct", "trading_value"]


def daily_price_path(ymd: str) -> Path:
    return C.PRICES_DAILY_DIR / ymd[:4] / f"{ymd}.csv"


def saved_price_days() -> set[str]:
    """이미 저장된 거래일(YYYYMMDD). 연도 parquet + 일별 CSV."""
    days: set[str] = set()
    for p in C.PRICES_DIR.glob("*.parquet"):
        df = pd.read_parquet(p, columns=["date"])
        days |= set(pd.to_datetime(df["date"]).dt.strftime("%Y%m%d").unique())
    for p in C.PRICES_DAILY_DIR.glob("*/*.csv"):
        days.add(p.stem)
    return days


HOLIDAYS_CSV = C.PRICES_DIR / "non_trading_days.csv"


def load_non_trading_days() -> set[str]:
    if HOLIDAYS_CSV.exists():
        return set(pd.read_csv(HOLIDAYS_CSV, dtype=str)["date"])
    return set()


def add_non_trading_day(ymd: str) -> None:
    days = load_non_trading_days() | {ymd}
    HOLIDAYS_CSV.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"date": sorted(days)}).to_csv(HOLIDAYS_CSV, index=False)


def load_prices(since: str | None = None) -> pd.DataFrame:
    frames = []
    for p in sorted(C.PRICES_DIR.glob("*.parquet")):
        if since and p.stem < since[:4]:
            continue
        frames.append(pd.read_parquet(p))
    for p in sorted(C.PRICES_DAILY_DIR.glob("*/*.csv")):
        if since and p.stem < since.replace("-", ""):
            continue
        frames.append(pd.read_csv(p, dtype={"stock_code": str}))
    if not frames:
        return pd.DataFrame(columns=PRICE_COLS)
    df = pd.concat(frames, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["stock_code"] = df["stock_code"].astype(str).str.zfill(6)
    df = df.drop_duplicates(["date", "stock_code"], keep="last")
    return df


def compact_year(year: str) -> int:
    """완료된 연도의 일별 CSV를 {year}.parquet 하나로 합친다(저장소 용량 절약)."""
    files = sorted((C.PRICES_DAILY_DIR / year).glob("*.csv"))
    if not files:
        return 0
    df = pd.concat([pd.read_csv(p, dtype={"stock_code": str}) for p in files], ignore_index=True)
    target = C.PRICES_DIR / f"{year}.parquet"
    if target.exists():
        df = pd.concat([pd.read_parquet(target), df], ignore_index=True)
    df = df.drop_duplicates(["date", "stock_code"], keep="last").sort_values(["date", "stock_code"])
    df.to_parquet(target, index=False, compression="zstd")
    for p in files:
        p.unlink()
    (C.PRICES_DAILY_DIR / year).rmdir()
    return len(df)

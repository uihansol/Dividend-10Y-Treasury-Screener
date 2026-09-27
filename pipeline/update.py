"""데이터 업데이트 실행기.

사용 예
  python -m pipeline.update all                 # 매일: 10Y → 주가 → 종목 → 배당 → 계산
  python -m pipeline.update prices --start 2016-01-01 --max-days 400   # 최초 구축(나눠서 실행 가능)
  python -m pipeline.update dividends           # DART (하루 예산만큼 이어 받기)
  python -m pipeline.update build               # 계산만 다시
  python -m pipeline.update compact --year 2025 # 끝난 연도 일별 CSV → parquet

각 단계는 실패해도 다음 단계로 넘어가며, build는 저장된 기존 데이터로 항상 실행된다(원칙 5).
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["all", "us10y", "prices", "stocks", "dividends", "build", "compact"])
    ap.add_argument("--start", help="prices 시작일 YYYY-MM-DD")
    ap.add_argument("--max-days", type=int, help="prices 이번 실행에서 받을 최대 거래일 수")
    ap.add_argument("--year", help="compact 대상 연도")
    ap.add_argument("--codes", help="dividends 대상 종목코드(쉼표). 테스트용")
    a = ap.parse_args(argv)

    from . import config as C  # noqa: F401  (.env 로드 후 import)
    from .build import build
    from .dart import update_dividends
    from .fred import update_us10y
    from .krx import update_prices, update_stock_list
    from .store import compact_year

    results = {}
    steps = ["us10y", "prices", "stocks", "dividends", "build"] if a.step == "all" else [a.step]
    for s in steps:
        if s == "us10y":
            results[s] = update_us10y()
        elif s == "prices":
            results[s] = update_prices(a.start, a.max_days)
        elif s == "stocks":
            results[s] = update_stock_list()
        elif s == "dividends":
            results[s] = update_dividends(a.codes.split(",") if a.codes else None)
        elif s == "compact":
            year = a.year or str(date.today().year - 1)
            results[s] = {"rows": compact_year(year)}
        elif s == "build":
            try:
                results[s] = {"ok": True, **build()}
            except Exception as e:
                results[s] = {"ok": False, "error": repr(e)}
        print(f"[{s}] {json.dumps(results[s], ensure_ascii=False, default=str)}", flush=True)

    # 새해 첫 실행 때 지난해 일별 파일 자동 압축
    if a.step == "all":
        from .config import PRICES_DAILY_DIR
        for d in PRICES_DAILY_DIR.glob("*"):
            if d.is_dir() and d.name < str(date.today().year):
                print(f"[compact] {d.name}: {compact_year(d.name)} rows")

    return 0 if results.get("build", {"ok": True}).get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())

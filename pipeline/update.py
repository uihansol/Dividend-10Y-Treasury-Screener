"""데이터 업데이트 실행기.

모드
  python -m pipeline.update initial       # 최초 1회: 과거 데이터 전체 구축
  python -m pipeline.update incremental   # 매일: 마지막 저장일 이후만 갱신
  python -m pipeline.update all           # 기존 호환용: initial과 같은 전체 순서 실행

개별 단계
  python -m pipeline.update prices --start 2016-01-01 --max-days 400
  python -m pipeline.update dividends
  python -m pipeline.update build
  python -m pipeline.update compact --year 2025
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


def _run_steps(steps, a, update_us10y, update_prices, update_stock_list,
               update_dividends, build, compact_year, C):
    results = {}
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
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "step",
        choices=["initial", "incremental", "all", "us10y", "prices", "stocks",
                 "dividends", "build", "compact"],
    )
    ap.add_argument("--start", help="prices 시작일 YYYY-MM-DD (initial 재시작용)")
    ap.add_argument("--max-days", type=int, help="prices 이번 실행에서 받을 최대 거래일 수")
    ap.add_argument("--year", help="compact 대상 연도")
    ap.add_argument("--codes", help="dividends 대상 종목코드(쉼표). 테스트용")
    a = ap.parse_args(argv)

    from . import config as C
    from .build import build
    from .dart import update_dividends
    from .fred import update_us10y
    from .krx import update_prices, update_stock_list
    from .store import compact_year

    if a.step in ("initial", "all"):
        steps = ["us10y", "prices", "stocks", "dividends", "build"]
    elif a.step == "incremental":
        # 모든 수집기는 저장된 마지막 날짜/로그를 기준으로 증분 갱신한다.
        steps = ["us10y", "prices", "stocks", "dividends", "build"]
    else:
        steps = [a.step]

    results = _run_steps(
        steps, a, update_us10y, update_prices, update_stock_list,
        update_dividends, build, compact_year, C
    )

    # 새해 첫 실행 때 지난해 일별 파일 자동 압축
    if a.step in ("initial", "incremental", "all"):
        from .config import PRICES_DAILY_DIR
        current_year = str(date.today().year)
        for d in PRICES_DAILY_DIR.glob("*"):
            if d.is_dir() and d.name < current_year:
                print(f"[compact] {d.name}: {compact_year(d.name)} rows", flush=True)

    return 0 if results.get("build", {"ok": True}).get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())

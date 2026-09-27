"""실행기.

  python -m pipeline.update init              # 최초: 미국10Y + 종목 master만 (가격·배당 전체 수집 없음)
  python -m pipeline.update us10y             # 미국 10년물 증분 업데이트 + 캐시된 종목 재계산(네트워크 없음)
  python -m pipeline.update master            # 종목 master 갱신 (주 1회)
  python -m pipeline.update stock 005930      # 한 종목 분석 (캐시 없으면 최초 수집, 있으면 증분)
  python -m pipeline.update stock 005930 --force   # cooldown 무시
  python -m pipeline.update stock 005930 --stage prices|dividends|us10y|compute   # 단계별 실행 (Actions용)
  python -m pipeline.update recompute         # 캐시된 종목만 네트워크 없이 다시 계산
  python -m pipeline.update search 삼성        # 로컬 master 검색 (네트워크 없음)
  python -m pipeline.update index             # data/cache/index.json 재생성
  python -m pipeline.update rawdiv 00244455 2026 Q1   # DART alotMatter 원본 확인 (파싱 디버그용)
"""
from __future__ import annotations

import argparse
import json
import sys

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("step", choices=["init", "us10y", "master", "stock", "search", "index", "recompute",
                                     "rawdiv", "disclist"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("extra", nargs="*")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--stage", choices=["prices", "dividends", "us10y", "compute"])
    a = ap.parse_args(argv)

    from .cache import DataUnavailable, StockNotFound, analyze_stock, rebuild_index, recompute_cached
    from .fred import update_us10y
    from .master import build_master, search

    def out(name, obj):
        print(f"[{name}] {json.dumps(obj, ensure_ascii=False, default=str)}", flush=True)

    if a.step in ("init", "us10y"):
        r = update_us10y()
        out("us10y", r)
        if r.get("ok") and r.get("added"):
            out("recompute", recompute_cached())
    if a.step == "recompute":
        out("recompute", recompute_cached())
    if a.step in ("init", "master"):
        r = build_master()
        out("master", r)
        if a.step == "master" and not r.get("ok"):
            return 1
    if a.step == "init":
        out("index", {"stocks": len(rebuild_index()["stocks"])})
    if a.step == "index":
        out("index", {"stocks": len(rebuild_index()["stocks"])})
    if a.step == "search":
        out("search", [(s["code"], s["name"], s["market"]) for s in search(a.arg or "")])
    if a.step == "rawdiv":
        from . import config as C
        from .dart import DartClient
        corp_code = a.arg
        year, period = a.extra[0], a.extra[1]
        j = DartClient().get("alotMatter.json", corp_code=corp_code, bsns_year=year,
                             reprt_code=C.REPRT_CODES[period]).json()
        out("rawdiv", j)
    if a.step == "disclist":
        from .dart import DartClient
        corp_code, bgn, end = a.arg, a.extra[0], a.extra[1]
        j = DartClient().get("list.json", corp_code=corp_code, bgn_de=bgn, end_de=end, page_count=100).json()
        out("disclist", [(x.get("rcept_no"), x.get("rcept_dt"), x.get("report_nm")) for x in j.get("list", [])])
    if a.step == "stock":
        if not a.arg:
            ap.error("종목코드가 필요합니다")
        try:
            r = analyze_stock(a.arg, force=a.force, stage=a.stage)
        except StockNotFound:
            out("stock", {"ok": False, "error": f"master에 없는 종목코드: {a.arg}"})
            return 2
        except DataUnavailable as e:
            out("stock", {"ok": False, "error": str(e)})
            return 3
        if a.stage and a.stage != "compute":
            out(a.stage, {"steps": r["steps"], "errors": r["errors"] or None})
            return 0
        s = r["summary"]
        out("stock", {"ok": True, "code": s["code"], "name": s["name"], "price": s["price"], "dps": s["dps"],
                      "yield": s["yield"], "multiple": s["multiple"], "pct": s["pct"], "paid10": s["paid10"],
                      "steps": r["metadata"]["steps"], "errors": r["metadata"]["last_error"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())

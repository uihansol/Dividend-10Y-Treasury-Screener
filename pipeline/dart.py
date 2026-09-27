"""OpenDART 배당 데이터 수집.

사용 API (모두 OpenDART 개발가이드에 있는 공식 엔드포인트)
 - 고유번호  : GET https://opendart.fss.or.kr/api/corpCode.xml   (zip 안의 CORPCODE.xml)
 - 배당에 관한 사항 : GET https://opendart.fss.or.kr/api/alotMatter.json
     요청: crtfc_key, corp_code, bsns_year(2015~), reprt_code(11013/11012/11014/11011)
     응답: rcept_no, se, stock_knd, thstrm(당기), frmtrm, lwfr, stlm_dt(결산기준일)

저장 규칙
 - '주당 현금배당금' 행 중 보통주의 당기(thstrm) 값만 쓴다.
 - confirmed_date = 접수번호(rcept_no) 앞 8자리(접수일)
 - basis_date     = stlm_dt(보고기간 말일). 이 날의 주식 수 기준 금액
 - 값이 '-' 이면 0원(배당 없음), 행 자체가 없거나 013(데이터 없음)이면 '알 수 없음'으로 둔다.

호출량: 전 종목 × 11년 × 4개 보고서는 10만 건이 넘어 OpenDART 하루 한도(약 2만 건)를
넘는다. 그래서 (1) 최근 사업보고서로 배당 기업을 먼저 가려내고 (2) 배당 기업만 과거를
채우며 (3) 하루 예산(DART_DAILY_BUDGET)을 넘으면 멈췄다가 다음 실행에서 이어 받는다.
"""
from __future__ import annotations

import io
import time
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

import pandas as pd
import requests

from . import config as C
from .store import mark_source, now_kst

BASE = "https://opendart.fss.or.kr/api"
PERIOD_END = {"Q1": (3, 31), "H1": (6, 30), "Q3": (9, 30), "FY": (12, 31)}
LOG_COLS = ["stock_code", "corp_code", "fiscal_year", "period", "status", "fetched_at"]
REP_COLS = ["stock_code", "corp_code", "fiscal_year", "period", "cum_dps", "basis_date",
            "confirmed_date", "rcept_no", "stock_knd", "fetched_at"]
CORP_CODES_CSV = C.DIV_DIR / "corp_codes.csv"


class BudgetExhausted(RuntimeError):
    pass


class DartClient:
    def __init__(self, key: str, budget: int):
        if not key:
            raise RuntimeError("DART_API_KEY가 없습니다 (.env 또는 GitHub Secret).")
        self.key, self.budget, self.calls = key, budget, 0
        self.s = requests.Session()

    def get(self, path: str, **params):
        if self.calls >= self.budget:
            raise BudgetExhausted(f"오늘 예산 {self.budget}건 사용")
        self.calls += 1
        r = self.s.get(f"{BASE}/{path}", params={"crtfc_key": self.key, **params}, timeout=30)
        r.raise_for_status()
        time.sleep(0.07)
        return r


def update_corp_codes(client: DartClient) -> pd.DataFrame:
    r = client.get("corpCode.xml")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml)
    rows = []
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            rows.append({"corp_code": el.findtext("corp_code").strip(),
                         "corp_name": (el.findtext("corp_name") or "").strip(),
                         "stock_code": sc.zfill(6),
                         "modify_date": (el.findtext("modify_date") or "").strip()})
    df = pd.DataFrame(rows)
    CORP_CODES_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(CORP_CODES_CSV, index=False)
    return df


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if s in ("", "-", "−"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def parse_alot(rows: list[dict]) -> dict | None:
    """alotMatter 응답에서 보통주 주당 현금배당금(당기) 1건을 고른다."""
    cands = [r for r in rows if "주당" in (r.get("se") or "") and "현금배당금" in (r.get("se") or "").replace(" ", "")]
    if not cands:
        return None
    common = [r for r in cands if "보통" in (r.get("stock_knd") or "")]
    pick = common[0] if common else (cands[0] if len(cands) == 1 else None)
    if pick is None:
        return None
    v = _num(pick.get("thstrm"))
    if v is None:
        return None
    rno = pick.get("rcept_no", "")
    return {"cum_dps": v, "rcept_no": rno,
            "confirmed_date": datetime.strptime(rno[:8], "%Y%m%d").date().isoformat() if len(rno) >= 8 else None,
            "basis_date": pick.get("stlm_dt") or None, "stock_knd": pick.get("stock_knd", ""),
            "frmtrm": _num(pick.get("frmtrm")), "lwfr": _num(pick.get("lwfr"))}


def _load(path, cols):
    if path.exists():
        return pd.read_csv(path, dtype={"stock_code": str, "corp_code": str})
    return pd.DataFrame(columns=cols)


def _due(year: int, period: str, today: date) -> bool:
    """보고서가 나왔을 법한 시점인지 (보고기간 말 + 14일 이후)."""
    m, d = PERIOD_END[period]
    return today >= date(year, m, d) + timedelta(days=14)


def _retry_window(year: int, period: str, today: date) -> bool:
    """'데이터 없음'을 다시 확인할 기간. 보고기간 말 + 200일이 지나면 확정으로 본다."""
    m, d = PERIOD_END[period]
    return today <= date(year, m, d) + timedelta(days=200)


def update_dividends(codes: list[str] | None = None) -> dict:
    today = date.today()
    try:
        client = DartClient(C.DART_API_KEY, C.DART_DAILY_BUDGET)
    except Exception as e:
        mark_source("dividends", False, error=repr(e))
        return {"ok": False, "error": repr(e)}

    log = _load(C.DART_FETCH_LOG_CSV, LOG_COLS)
    reps = _load(C.DART_REPORTS_CSV, REP_COLS)
    new_log, new_reps = [], []
    status_msg = "complete"

    try:
        corp = update_corp_codes(client) if (not CORP_CODES_CSV.exists() or today.weekday() == 0) \
            else pd.read_csv(CORP_CODES_CSV, dtype=str)
        stocks = pd.read_csv(C.STOCKS_CSV, dtype=str) if C.STOCKS_CSV.exists() else pd.DataFrame(columns=["stock_code"])
        listed = stocks[stocks["listing_status"] == "listed"]["stock_code"] if len(stocks) else pd.Series(dtype=str)
        cmap = dict(zip(corp["stock_code"], corp["corp_code"]))
        # DART 고유번호가 있는 종목 = 보통주 (우선주 코드는 corpCode에 없다)
        targets = [c for c in (codes or listed.tolist()) if c in cmap]

        done = {(r.stock_code, int(r.fiscal_year), r.period): r.status for r in log.itertuples()}
        last_try = {(r.stock_code, int(r.fiscal_year), r.period): r.fetched_at for r in log.itertuples()}

        def fetch(code: str, year: int, period: str):
            r = client.get("alotMatter.json", corp_code=cmap[code], bsns_year=str(year),
                           reprt_code=C.REPRT_CODES[period])
            j = r.json()
            st = j.get("status", "")
            if st == "020":
                raise BudgetExhausted("OpenDART 요청 제한(020)")
            if st not in ("000", "013"):
                raise RuntimeError(f"DART {st}: {j.get('message')}")
            parsed = parse_alot(j.get("list", [])) if st == "000" else None
            status = "ok" if parsed else ("no_data" if st == "013" else "no_row")
            new_log.append({"stock_code": code, "corp_code": cmap[code], "fiscal_year": year,
                            "period": period, "status": status, "fetched_at": now_kst()})
            done[(code, year, period)] = status
            if parsed:
                new_reps.append({"stock_code": code, "corp_code": cmap[code], "fiscal_year": year,
                                 "period": period, **{k: parsed[k] for k in
                                 ("cum_dps", "basis_date", "confirmed_date", "rcept_no", "stock_knd")},
                                 "fetched_at": now_kst()})
            return parsed

        def need(code, year, period) -> bool:
            if not _due(year, period, today):
                return False
            st = done.get((code, year, period))
            if st is None:
                return True
            if st == "ok":
                return False
            # no_data / no_row: 공시 대기 중일 수 있으니 기간 내에서 하루 한 번 재시도
            if not _retry_window(year, period, today):
                return False
            lt = str(last_try.get((code, year, period), ""))[:10]
            return lt != today.isoformat()

        cur = today.year
        last_fy = cur - 1 if _due(cur - 1, "FY", today) else cur - 2

        # (1) 배당 기업 가려내기: 최근 사업보고서 (당기·전기·전전기 중 하나라도 > 0)
        payers = set()
        known = reps[reps["period"] == "FY"].groupby("stock_code")["cum_dps"].max() if len(reps) else pd.Series(dtype=float)
        payers |= set(known[known > 0].index)
        for code in targets:
            if need(code, last_fy, "FY"):
                p = fetch(code, last_fy, "FY")
                if p and max(p["cum_dps"] or 0, p["frmtrm"] or 0, p["lwfr"] or 0) > 0:
                    payers.add(code)

        # (2) 배당 기업: 진행 중 연도 → 최근 연도 → 과거 순으로 채움
        years = list(range(cur, C.DART_FIRST_YEAR - 1, -1))
        for y in years:
            for code in sorted(payers):
                for period in ("FY", "Q3", "H1", "Q1"):
                    if need(code, y, period):
                        fetch(code, y, period)
    except BudgetExhausted as e:
        status_msg = f"partial: {e}"
    except Exception as e:
        _save(log, new_log, reps, new_reps)
        mark_source("dividends", False, error=repr(e), note=f"{client.calls} calls")
        return {"ok": False, "error": repr(e), "calls": client.calls}

    _save(log, new_log, reps, new_reps)
    mark_source("dividends", True, data_through=today.isoformat(),
                note=f"{status_msg}; {client.calls} calls; +{len(new_reps)} reports")
    return {"ok": True, "calls": client.calls, "added": len(new_reps), "status": status_msg}


def _save(log, new_log, reps, new_reps):
    C.DIV_DIR.mkdir(parents=True, exist_ok=True)
    if new_log:
        log = pd.concat([log, pd.DataFrame(new_log)], ignore_index=True)
        log = log.drop_duplicates(["stock_code", "fiscal_year", "period"], keep="last")
    log.to_csv(C.DART_FETCH_LOG_CSV, index=False)
    if new_reps:
        reps = pd.concat([reps, pd.DataFrame(new_reps)], ignore_index=True)
        reps = reps.drop_duplicates(["stock_code", "fiscal_year", "period"], keep="last")
    reps.to_csv(C.DART_REPORTS_CSV, index=False)

"""OpenDART 배당 데이터. 기업 1곳 단위 조회만 한다.

사용 API (OpenDART 개발가이드의 공식 엔드포인트)
 - 고유번호 : GET https://opendart.fss.or.kr/api/corpCode.xml   (master 생성 때만, zip 안의 CORPCODE.xml)
 - 배당에 관한 사항 : GET https://opendart.fss.or.kr/api/alotMatter.json
     요청: crtfc_key, corp_code, bsns_year(2015~), reprt_code(11013/11012/11014/11011)

저장 규칙
 - '주당 현금배당금' 행 중 보통주의 당기(thstrm) 값
 - confirmed_date = 접수번호(rcept_no) 앞 8자리(접수일)  → look-ahead 방지 기준
 - basis_date     = stlm_dt(보고기간 말일)
 - 값이 '-' 이면 0원, 행이 없거나 013(데이터 없음)이면 '알 수 없음'

한 기업 최초 조회 ≈ 12년 × 4개 보고서 = 약 48회 호출. 이미 'ok'로 받은 보고서는 다시 요청하지 않는다.
"""
from __future__ import annotations

import io
import time
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

import requests

from . import config as C
from .store import now_kst

BASE = "https://opendart.fss.or.kr/api"
PERIOD_END = {"Q1": (3, 31), "H1": (6, 30), "Q3": (9, 30), "FY": (12, 31)}


class DartError(RuntimeError):
    pass


class DartClient:
    def __init__(self, key: str | None = None):
        key = key if key is not None else C.DART_API_KEY
        if not key:
            raise DartError("DART_API_KEY가 없습니다 (.env 또는 GitHub Secret).")
        self.key, self.calls = key, 0
        self.s = requests.Session()

    def get(self, path: str, **params):
        self.calls += 1
        r = self.s.get(f"{BASE}/{path}", params={"crtfc_key": self.key, **params}, timeout=30)
        r.raise_for_status()
        time.sleep(0.07)
        return r


def fetch_corp_codes(client: DartClient) -> dict[str, dict]:
    """상장사 stock_code → {corp_code, corp_name}. master.json 생성 때만 호출."""
    r = client.get("corpCode.xml")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(zf.read(zf.namelist()[0]))
    out = {}
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if sc:
            out[sc.zfill(6)] = {"corp_code": el.findtext("corp_code").strip(),
                                "corp_name": (el.findtext("corp_name") or "").strip()}
    return out


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
            "basis_date": pick.get("stlm_dt") or None, "stock_knd": pick.get("stock_knd", "")}


def _due(year: int, period: str, today: date) -> bool:
    """보고서가 나왔을 법한 시점인지 (보고기간 말 + 14일 이후)."""
    m, d = PERIOD_END[period]
    return today >= date(year, m, d) + timedelta(days=14)


def _retry_window(year: int, period: str, today: date) -> bool:
    """'데이터 없음'을 다시 확인할 기간. 보고기간 말 + 200일이 지나면 없음으로 확정."""
    m, d = PERIOD_END[period]
    return today <= date(year, m, d) + timedelta(days=200)


def needs_fetch(key: str, log: dict, today: date) -> bool:
    year, period = int(key[:4]), key[5:]
    if not _due(year, period, today):
        return False
    rec = log.get(key)
    if rec is None:
        return True
    if rec["status"] == "ok":
        return False
    if not _retry_window(year, period, today):
        return False
    return str(rec.get("fetched_at", ""))[:10] != today.isoformat()  # 하루 한 번만 재확인


def fetch_stock_dividends(corp_code: str, start_year: int, end_year: int, log: dict,
                          today: date | None = None, client: DartClient | None = None) -> tuple[list[dict], dict]:
    """한 기업의 start_year~end_year 배당 보고서 중 아직 받지 않은(또는 재확인할) 것만 조회.

    log: {"2025-FY": {"status": "ok|no_data|no_row", "fetched_at": ...}, ...}  (캐시에 저장된 조회 기록)
    반환: (새로 받은 보고서 목록, 갱신된 log)
    """
    today = today or date.today()
    client = client or DartClient()
    log = dict(log)
    new = []
    for y in range(end_year, start_year - 1, -1):
        for period in ("FY", "Q3", "H1", "Q1"):
            key = f"{y}-{period}"
            if not needs_fetch(key, log, today):
                continue
            j = client.get("alotMatter.json", corp_code=corp_code, bsns_year=str(y),
                           reprt_code=C.REPRT_CODES[period]).json()
            st = j.get("status", "")
            if st == "020":
                raise DartError("OpenDART 요청 한도 초과(020)")
            if st not in ("000", "013"):
                raise DartError(f"DART {st}: {j.get('message')}")
            parsed = parse_alot(j.get("list", [])) if st == "000" else None
            log[key] = {"status": "ok" if parsed else ("no_data" if st == "013" else "no_row"),
                        "fetched_at": now_kst()}
            if parsed:
                new.append({"fiscal_year": y, "period": period, **parsed})
    return new, log

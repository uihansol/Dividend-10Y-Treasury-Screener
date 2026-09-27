"""OpenDART 배당 데이터. 기업 1곳 단위 조회만 한다.

사용 API (OpenDART 개발가이드의 공식 엔드포인트)
 - 고유번호 : GET https://opendart.fss.or.kr/api/corpCode.xml   (master 생성 때만, zip 안의 CORPCODE.xml)
 - 배당에 관한 사항 : GET https://opendart.fss.or.kr/api/alotMatter.json
     요청: crtfc_key, corp_code, bsns_year(2015~), reprt_code(11013/11012/11014/11011)
 - 공시 목록 : GET https://opendart.fss.or.kr/api/list.json (corp_code, bgn_de, end_de)
 - 공시 원문 : GET https://opendart.fss.or.kr/api/document.xml (rcept_no) → ZIP 안에 HTML

저장 규칙
 - '주당 현금배당금' 행 중 보통주의 당기(thstrm) 값
 - confirmed_date = 접수번호(rcept_no) 앞 8자리(접수일)  → look-ahead 방지 기준
 - basis_date     = stlm_dt(보고기간 말일)
 - 값이 '-' 이면 0원, 행이 없거나 013(데이터 없음)이면 '알 수 없음'

한 기업 최초 조회 ≈ 12년 × 4개 보고서 = 약 48회 호출. 이미 'ok'로 받은 보고서는 다시 요청하지 않는다.

수시공시(현금·현물배당결정) 선반영
 - 정기보고서(분기·반기·사업보고서)는 이사회 배당결정 후 45~90일 뒤에 나온다. 그 사이에는
   '현금ㆍ현물배당결정' 수시공시가 이미 실제 배당금을 공개하고 있는데, 우리는 이걸 무시하고
   있었다(look-ahead 방지를 정기보고서 접수일 기준으로만 걸었기 때문). 이제 이 수시공시도
   읽어서, 아직 정기보고서에 반영 안 된 올해분을 '잠정(PROV)' 값으로 미리 쓴다.
 - '현금·현물배당결정'은 금융위 표준 서식이라 항목 번호(1. 배당구분, 3. 1주당 배당금(원),
   6. 배당기준일 등)가 회사마다 고정돼 있다. 그 항목 번호 뒤에 오는 첫 xforms_input 값을 읽는다.
 - confirmed_date는 이 공시의 접수일(rcept_no 앞 8자리) — 정기보고서보다 이르지만 여전히
   '실제로 공개된' 날짜이므로 look-ahead는 아니다. 정기보고서가 나중에 이 기간을 확정하면
   그 값이 우선한다(같은 해에 더 늦은 확정일을 가진 정기보고서가 있으면 잠정값은 버린다).
"""
from __future__ import annotations

import io
import re
import time
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

import requests

from . import config as C
from .store import now_kst

BASE = "https://opendart.fss.or.kr/api"
# 배당 parser 규칙이 바뀌면 기존 no_row 캐시도 다시 읽도록 버전을 올린다.
DIVIDEND_PARSER_VERSION = 2
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


def _norm_text(v) -> str:
    """DART 표 안의 일반/비표준 공백을 제거한다."""
    return re.sub(r"\\s+", "", str(v or "")).replace("\\u00a0", "")


def parse_alot(rows: list[dict]) -> dict | None:
    """alotMatter 응답에서 보통주 주당 현금배당금(당기) 1건을 고른다.

    회사에 따라 stock_knd가 '보통주', '보통주식'처럼 달라지거나 비어 있는 경우가
    있다. 기존 코드는 후보가 2개 이상이고 stock_knd로 보통주를 특정하지 못하면
    None을 반환했는데, KCC처럼 이 형식 차이가 있는 회사에서 전체 배당 이력이
    누락될 수 있다.
    """
    cands = []
    for r in rows:
        se = _norm_text(r.get("se"))
        if "주당" in se and "현금배당금" in se:
            cands.append(r)
    if not cands:
        cands = [r for r in rows if "현금배당" in _norm_text(r.get("se")) and "주당" in _norm_text(r.get("se"))]
    if not cands:
        return None

    def is_common(r: dict) -> bool:
        return "보통" in _norm_text(r.get("stock_knd"))

    common = [r for r in cands if is_common(r)]
    if common:
        pick = common[0]
    elif len(cands) == 1:
        pick = cands[0]
    else:
        # stock_knd가 비어 있는 응답에서는 첫 번째 유효한 주당배당금 행을 사용한다.
        # DART allotMatter의 표준 순서는 보통주 → 종류주식이다.
        valid = [r for r in cands if _num(r.get("thstrm")) is not None]
        pick = valid[0] if valid else None
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
    if rec.get("parser_version") != DIVIDEND_PARSER_VERSION:
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
                        "fetched_at": now_kst(), "parser_version": DIVIDEND_PARSER_VERSION}
            if parsed:
                new.append({"fiscal_year": y, "period": period, **parsed})
    return new, log


# ---------------------------------------------------------------- 수시공시(현금·현물배당결정)
def _is_dvd_decision(report_nm: str) -> bool:
    """'현금·현물배당결정'류 공시만 고른다. '...배당을위한주주명부폐쇄...결정'처럼
    금액이 없는 관련 공시(기준일만 잡는 공시)는 제외한다."""
    name = re.sub(r"\s+", "", report_nm or "")
    return "배당결정" in name and ("현금" in name or "현물" in name) and "명부폐쇄" not in name


def _field_after(html: str, label: str) -> str | None:
    """'N. 항목명' 다음에 나오는 첫 xforms_input 칸의 값. 금융위 표준 서식이라 항목 번호가
    회사마다 고정돼 있다."""
    m = re.search(re.escape(label) + r'.*?xforms_input[^>]*>\s*([^<]*?)\s*<', html, re.S)
    if not m:
        return None
    v = m.group(1).strip()
    return v or None


def parse_dvd_decision(html: str) -> dict | None:
    """'현금·현물배당결정' 수시공시 원문(HTML)에서 보통주 1주당 배당금·배당기준일을 뽑는다.
    현물배당이면(배당종류에 '현금'이 없으면) None. 금액이 없거나 0이어도 None."""
    kind = _field_after(html, "2. 배당종류")
    if kind is not None and "현금" not in kind:
        return None
    common = _field_after(html, "3. 1주당 배당금(원)") or _field_after(html, "2. 1주당 배당금(원)")
    if common is None:
        return None
    v = _num(common)
    if v is None or v <= 0:
        return None
    basis = _field_after(html, "6. 배당기준일") or _field_after(html, "5. 배당기준일")
    basis_iso = basis if basis and re.match(r"^\d{4}-\d{2}-\d{2}$", basis) else None
    return {"amount": v, "basis_date": basis_iso}


def fetch_dividend_announcements(corp_code: str, year: int, log: dict, today: date | None = None,
                                 client: DartClient | None = None) -> tuple[list[dict], dict]:
    """올해 '현금·현물배당결정' 수시공시를 찾아 아직 안 받은 것만 원문을 읽는다.

    정기보고서(분기·반기·사업보고서)는 배당 결정 후 45~90일 뒤에 나오는데, 그 사이에는
    이 수시공시가 이미 실제 금액을 공개하고 있다. cache.to_reports()에서 이 결과를
    '아직 정기보고서로 확정되지 않은 잠정값(PROV)'으로 합친다.

    log: {"<rcept_no>": {"status": "ok|no_row", "fetched_at": ..., (status가 ok면) "confirmed_date",
         "basis_date", "amount"}}  — 조회 기록과 파싱 결과를 함께 들고 있어야 cache._provisional_reports가
         (원문을 다시 열지 않고) 그대로 쓸 수 있다.
    반환: (새로 받은 공시 목록[{"rcept_no","confirmed_date","basis_date","amount"}], 갱신된 log)
    """
    today = today or date.today()
    client = client or DartClient()
    log = dict(log)
    j = client.get("list.json", corp_code=corp_code, bgn_de=f"{year}0101",
                   end_de=today.strftime("%Y%m%d"), page_count=100).json()
    st = j.get("status", "")
    if st == "020":
        raise DartError("OpenDART 요청 한도 초과(020)")
    if st not in ("000", "013"):
        raise DartError(f"DART {st}: {j.get('message')}")
    new = []
    for row in j.get("list", []) if st == "000" else []:
        if not _is_dvd_decision(row.get("report_nm", "")):
            continue
        rcept_no = row.get("rcept_no", "")
        rec = log.get(rcept_no, {})
        # status가 "ok"인데 amount가 없으면(예전 버전에서 파싱 결과를 안 남기던 시절의 기록) 다시 읽는다.
        already = rec.get("status") == "no_row" or (rec.get("status") == "ok" and "amount" in rec)
        if not rcept_no or already:
            continue
        r = client.get("document.xml", rcept_no=rcept_no)
        try:
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            html = zf.read(zf.namelist()[0]).decode("utf-8", errors="replace")
            parsed = parse_dvd_decision(html)
        except Exception:  # pragma: no cover - 원문 형식이 예상과 다른 극히 드문 경우
            parsed = None
        if parsed:
            entry = {"rcept_no": rcept_no,
                    "confirmed_date": datetime.strptime(rcept_no[:8], "%Y%m%d").date().isoformat(),
                    "basis_date": parsed["basis_date"], "amount": parsed["amount"]}
            log[rcept_no] = {"status": "ok", "fetched_at": now_kst(), **entry}
            new.append(entry)
        else:
            log[rcept_no] = {"status": "no_row", "fetched_at": now_kst()}
    return new, log

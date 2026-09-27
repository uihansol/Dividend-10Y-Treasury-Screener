"""종목 master (검색용 목록, 가격 아님).

data/stocks/master.json  = {"005930": {"code","name","market","corp_code","aliases":[...]}, ...}
data/stocks/aliases.json = {"삼전": "005930", ...}  사람이 직접 관리 (선택)

검색은 이 로컬 파일만 쓴다. KRX/DART 목록 요청은 build_master()에서만(주 1회 정도) 일어난다.
"""
from __future__ import annotations

import re
import unicodedata

from . import config as C
from .store import mark_source, now_kst, read_json, write_json


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\s()\-·.,&]", "", s)


def build_master(listing: list[dict] | None = None, corp: dict[str, dict] | None = None) -> dict:
    """KRX 상장 목록 + DART 고유번호를 합쳐 master.json 저장. 인자는 테스트용 주입."""
    try:
        if listing is None:
            from .krx import fetch_market_listing
            listing = fetch_market_listing()
        if corp is None:
            from .dart import DartClient, fetch_corp_codes
            corp = fetch_corp_codes(DartClient())
        master = {}
        for r in listing:
            code = r["code"]
            c = corp.get(code)
            if c is None:
                continue  # DART 고유번호가 없는 종목(우선주·ETF·스팩 일부 등)은 배당 계산 불가 → 제외
            name = r.get("name") or c["corp_name"]
            aliases = sorted({a for a in (c["corp_name"],) if a and a != name})
            master[code] = {"code": code, "name": name, "market": r["market"],
                            "corp_code": c["corp_code"], "aliases": aliases}
        if len(master) < 100 and listing and len(listing) > 1000:
            raise RuntimeError(f"master가 비정상적으로 작음({len(master)}) — 기존 파일 유지")
        write_json(C.MASTER_JSON, {"updated_at": now_kst(), "stocks": master}, compact=True)
        mark_source("master", True, data_through=now_kst()[:10], note=f"{len(master)} stocks")
        return {"ok": True, "count": len(master)}
    except Exception as e:
        mark_source("master", False, error=repr(e))
        return {"ok": False, "error": repr(e)}


def load_master() -> dict[str, dict]:
    m = read_json(C.MASTER_JSON, {"stocks": {}})["stocks"]
    for alias, code in (read_json(C.ALIASES_JSON, {}) or {}).items():
        if code in m and alias not in m[code]["aliases"]:
            m[code] = {**m[code], "aliases": m[code]["aliases"] + [alias]}
    return m


def search(q: str, master: dict[str, dict] | None = None, limit: int = 10) -> list[dict]:
    """종목명·별칭·코드 검색. 정확히 일치 > 앞부분 일치 > 포함 순. 네트워크 호출 없음."""
    master = master if master is not None else load_master()
    nq = _norm(q)
    if not nq:
        return []
    scored = []
    for code, s in master.items():
        names = [_norm(s["name"])] + [_norm(a) for a in s.get("aliases", [])]
        if nq == code or any(n == nq for n in names):
            rank = 0
        elif code.startswith(nq) or any(n.startswith(nq) for n in names):
            rank = 1
        elif any(nq in n for n in names):
            rank = 2
        else:
            continue
        scored.append((rank, len(s["name"]), s["name"], s))
    scored.sort(key=lambda x: x[:3])
    return [s for *_, s in scored[:limit]]

"""작은 파일 입출력 도우미와 공통 데이터(10Y·master) 업데이트 상태 기록."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as C

KST = timezone(timedelta(hours=9))


def now_kst() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def read_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def write_json(path: Path, obj, compact: bool = False) -> None:
    """임시 파일에 쓴 뒤 교체(중간에 죽어도 반쯤 쓴 파일이 남지 않음)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) if compact \
        else json.dumps(obj, ensure_ascii=False, indent=1)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def mark_source(name: str, ok: bool, *, data_through: str | None = None,
                note: str | None = None, error: str | None = None) -> None:
    """업데이트 시도 결과 기록. 실패해도 마지막 성공 정보는 지우지 않는다."""
    s = read_json(C.SOURCES_JSON, {})
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
    write_json(C.SOURCES_JSON, s)

import { useEffect, useMemo, useRef, useState } from "react";
import { searchMaster } from "../lib/data";
import type { MasterStock } from "../lib/types";

/** 로컬 master 자동완성. 입력 중에는 네트워크 요청이 없고, 선택한 순간 onPick만 호출한다. */
export function SearchBox({ master, onPick, autoFocus }: {
  master: MasterStock[] | null; onPick: (s: MasterStock) => void; autoFocus?: boolean;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(0);
  const ref = useRef<HTMLDivElement>(null);
  const results = useMemo(() => (master ? searchMaster(master, q, 10) : []), [master, q]);

  useEffect(() => {
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const pick = (s: MasterStock) => { setQ(""); setOpen(false); onPick(s); };

  return (
    <div className="search" ref={ref}>
      <label className="search-label" htmlFor="stock-search">종목 검색</label>
      <div className="search-field">
        <input id="stock-search" type="search" autoComplete="off" autoFocus={autoFocus}
          placeholder={master ? "종목명 또는 코드 (예: 삼성전자, 005930)" : "종목 목록 불러오는 중…"}
          value={q} disabled={!master}
          role="combobox" aria-expanded={open && results.length > 0} aria-controls="search-list"
          onChange={(e) => { setQ(e.target.value); setOpen(true); setHi(0); }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, results.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, 0)); }
            else if (e.key === "Enter" && results[hi]) { e.preventDefault(); pick(results[hi]); }
            else if (e.key === "Escape") setOpen(false);
          }} />
        <span className="search-icon" aria-hidden>⌕</span>
      </div>
      {open && q.trim() !== "" && (
        <ul id="search-list" className="search-list" role="listbox">
          {results.length === 0 && <li className="search-empty">일치하는 종목이 없습니다</li>}
          {results.map((s, i) => (
            <li key={s.code} role="option" aria-selected={i === hi} className={i === hi ? "on" : ""}
              onMouseEnter={() => setHi(i)} onMouseDown={(e) => { e.preventDefault(); pick(s); }}>
              <span className="s-name">{s.name}</span>
              <span className="s-code">{s.code}</span>
              <span className="s-mkt">{s.market}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

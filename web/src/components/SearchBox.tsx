import { useEffect, useMemo, useRef, useState } from "react";
import { searchMaster } from "../lib/data";
import type { MasterStock } from "../lib/types";

type MultiMatch = { term: string; stock: MasterStock | null };

/** 로컬 master 자동완성. 입력 중에는 네트워크 요청이 없고, 선택한 순간 onPick만 호출한다.
 * onAddMultiple이 주어지고 쉼표로 여러 종목명을 입력하면(예: "삼성전자, SK하이닉스") 각 항목을
 * 따로 찾아 한 번에 추가할 수 있는 모드로 바뀐다(한 종목 상세로 이동하는 onPick과는 다른 동작). */
export function SearchBox({ master, onPick, onAddMultiple, autoFocus }: {
  master: MasterStock[] | null; onPick: (s: MasterStock) => void;
  onAddMultiple?: (stocks: MasterStock[]) => void; autoFocus?: boolean;
}) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [hi, setHi] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  const multiTerms = useMemo(
    () => (onAddMultiple && q.includes(",") ? q.split(",").map((t) => t.trim()).filter(Boolean) : null),
    [q, onAddMultiple]);
  const isMulti = !!multiTerms && multiTerms.length > 1;
  const multiMatches = useMemo<MultiMatch[]>(
    () => (isMulti && master ? multiTerms!.map((term) => ({ term, stock: searchMaster(master, term, 1)[0] ?? null })) : []),
    [isMulti, multiTerms, master]);
  const matchedStocks = useMemo(() => multiMatches.map((m) => m.stock).filter((s): s is MasterStock => s !== null), [multiMatches]);

  const results = useMemo(() => (master && !isMulti ? searchMaster(master, q, 10) : []), [master, q, isMulti]);

  useEffect(() => {
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const pick = (s: MasterStock) => { setQ(""); setOpen(false); onPick(s); };
  const addAll = () => {
    if (matchedStocks.length === 0) return;
    onAddMultiple!(matchedStocks);
    setQ("");
    setOpen(false);
  };

  return (
    <div className="search" ref={ref}>
      <label className="search-label" htmlFor="stock-search">종목 검색</label>
      <div className="search-field">
        <input id="stock-search" type="search" autoComplete="off" autoFocus={autoFocus}
          placeholder={master ? "종목명·코드·초성 (쉼표로 여러 종목 한꺼번에 추가, 예: 삼성전자, SK하이닉스)" : "종목 목록 불러오는 중…"}
          value={q} disabled={!master}
          role="combobox" aria-expanded={open && (isMulti || results.length > 0)} aria-controls="search-list"
          onChange={(e) => { setQ(e.target.value); setOpen(true); setHi(0); }}
          onFocus={() => setOpen(true)}
          onKeyDown={(e) => {
            if (isMulti) {
              if (e.key === "Enter") { e.preventDefault(); addAll(); }
              else if (e.key === "Escape") setOpen(false);
              return;
            }
            if (e.key === "ArrowDown") { e.preventDefault(); setHi((h) => Math.min(h + 1, results.length - 1)); }
            else if (e.key === "ArrowUp") { e.preventDefault(); setHi((h) => Math.max(h - 1, 0)); }
            else if (e.key === "Enter" && results[hi]) { e.preventDefault(); pick(results[hi]); }
            else if (e.key === "Escape") setOpen(false);
          }} />
        <span className="search-icon" aria-hidden>⌕</span>
      </div>
      {open && isMulti && (
        <ul id="search-list" className="search-list search-list-multi" role="listbox">
          {multiMatches.map((m, i) => (
            <li key={i} className="search-multi-row">
              {m.stock
                ? <><span className="s-name">{m.stock.name}</span><span className="s-code">{m.stock.code}</span><span className="s-mkt">{m.stock.market}</span></>
                : <span className="search-multi-miss">"{m.term}" 일치하는 종목 없음</span>}
            </li>
          ))}
          <li className="search-multi-actions">
            <button type="button" className="search-multi-add-btn" disabled={matchedStocks.length === 0}
              onMouseDown={(e) => { e.preventDefault(); addAll(); }}>
              {matchedStocks.length}개 종목 최근 조회 목록에 추가
            </button>
          </li>
        </ul>
      )}
      {open && !isMulti && q.trim() !== "" && (
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

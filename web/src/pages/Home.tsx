import { useMemo, useState } from "react";
import type { CacheIndex, MasterStock } from "../lib/types";
import { mult, pct, won, NA } from "../lib/format";
import { loadRecent, removeRecent, removeRecentMany, type RecentEntry } from "../lib/recent";
import { BandGauge } from "../components/BandGauge";
import { SearchBox } from "../components/SearchBox";

type MetricKey = "yield" | "multiple" | "paid10" | "pct";
type SortDirection = "asc" | "desc";
type SortState = { key: MetricKey; direction: SortDirection };
type ColumnOrder = MetricKey[];

const DEFAULT_COLUMNS: ColumnOrder = ["yield", "multiple", "paid10", "pct"];
const COLUMN_ORDER_KEY = "dividend-10y-home-column-order-v1";
const SORT_KEY = "dividend-10y-home-sort-v1";

const COLUMN_LABELS: Record<MetricKey, string> = {
  yield: "배당률",
  multiple: "배당/10Y",
  paid10: "10년 배당",
  pct: "역사적 위치",
};

function loadColumnOrder(): ColumnOrder {
  try {
    const raw = localStorage.getItem(COLUMN_ORDER_KEY);
    if (!raw) return DEFAULT_COLUMNS;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return DEFAULT_COLUMNS;
    const valid = parsed.filter((x): x is MetricKey => DEFAULT_COLUMNS.includes(x));
    return valid.length === DEFAULT_COLUMNS.length && new Set(valid).size === DEFAULT_COLUMNS.length ? valid : DEFAULT_COLUMNS;
  } catch {
    return DEFAULT_COLUMNS;
  }
}

function loadSort(): SortState {
  try {
    const raw = localStorage.getItem(SORT_KEY);
    if (!raw) return { key: "multiple", direction: "desc" };
    const parsed = JSON.parse(raw);
    if (DEFAULT_COLUMNS.includes(parsed?.key) && (parsed?.direction === "asc" || parsed?.direction === "desc")) return parsed;
  } catch {}
  return { key: "multiple", direction: "desc" };
}

export function Home({ master, index, indexError, onPick }: {
  master: MasterStock[] | null; index: CacheIndex | null; indexError: string | null; onPick: (s: MasterStock) => void;
}) {
  const [columns, setColumns] = useState<ColumnOrder>(() => loadColumnOrder());
  const [editingColumns, setEditingColumns] = useState(false);
  const [sort, setSort] = useState<SortState>(() => loadSort());
  const [recent, setRecent] = useState<RecentEntry[]>(() => loadRecent());
  const [editingRecent, setEditingRecent] = useState(false);
  const [selectedRecent, setSelectedRecent] = useState<Set<string>>(() => new Set());
  const [swipedRecent, setSwipedRecent] = useState<string | null>(null);

  const saveColumns = (next: ColumnOrder) => {
    setColumns(next);
    localStorage.setItem(COLUMN_ORDER_KEY, JSON.stringify(next));
  };

  const moveColumn = (index: number, delta: number) => {
    const next = [...columns];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    saveColumns(next);
  };

  const resetColumns = () => saveColumns(DEFAULT_COLUMNS);

  const toggleSort = (key: MetricKey) => {
    const next: SortState = sort.key === key
      ? { key, direction: sort.direction === "asc" ? "desc" : "asc" }
      : { key, direction: "desc" };
    setSort(next);
    localStorage.setItem(SORT_KEY, JSON.stringify(next));
  };

  const rows = useMemo(() => {
    const recentCodes = new Set(recent.map((item) => item.code));
    const r = (index?.stocks ?? []).filter((stock) => recentCodes.has(stock.code));
    const value = (row: typeof r[number], key: MetricKey): number => {
      if (key === "yield") return row.yield ?? -Infinity;
      if (key === "multiple") return row.multiple ?? -Infinity;
      if (key === "paid10") return row.paid10 ?? -Infinity;
      return row.pct ?? -Infinity;
    };
    r.sort((a, b) => {
      const diff = value(a, sort.key) - value(b, sort.key);
      if (diff !== 0) return sort.direction === "asc" ? diff : -diff;
      return a.name.localeCompare(b.name, "ko");
    });
    return r;
  }, [index, sort]);

  const deleteRecent = (code: string) => {
    if (!window.confirm("최근 조회 목록에서 삭제할까요?")) return;
    setRecent(removeRecent(code));
    setSelectedRecent((prev) => { const next = new Set(prev); next.delete(code); return next; });
    setSwipedRecent(null);
  };
  const toggleRecentSelection = (code: string) => {
    setSelectedRecent((prev) => { const next = new Set(prev); if (next.has(code)) next.delete(code); else next.add(code); return next; });
  };
  const deleteSelectedRecent = () => {
    if (selectedRecent.size === 0) return;
    if (!window.confirm("선택한 " + selectedRecent.size + "개 종목을 최근 조회 목록에서 삭제할까요?")) return;
    setRecent(removeRecentMany(selectedRecent));
    setSelectedRecent(new Set());
    setEditingRecent(false);
  };
  const toggleRecentEdit = () => {
    setEditingRecent((v) => !v);
    setSelectedRecent(new Set());
    setSwipedRecent(null);
  };
  const us = index?.sources?.us10y;

  return (
    <main className="page">
      <header className="masthead">
        <h1>배당수익률 ÷ 미국 10년물</h1>
        <dl className="headline-figures">
          <div><dt>미국 10년물 데이터</dt><dd>{us?.data_through ?? NA}</dd></div>
          <div><dt>조회한 종목</dt><dd>{index ? `${index.stocks.length}개` : NA}</dd></div>
        </dl>
      </header>

      <section className="search-wrap">
        <SearchBox master={master} onPick={onPick} autoFocus />
        <p className="note">
          처음 조회하는 종목은 2016년부터 가격·배당을 모으느라 1~3분 걸립니다. 한 번 조회한 종목은 저장해 두고 다음부터는 새 데이터만 확인합니다.
        </p>
        {recent.length > 0 && (
          <div className="recent-section">
            <div className="recent-head">
              <div className="recent-title"><span className="recent-label">최근 조회</span><span className="recent-count">{recent.length}개</span></div>
              <div className="recent-actions">
                {editingRecent && selectedRecent.size > 0 && <button type="button" className="recent-delete-btn" onClick={deleteSelectedRecent}>선택 삭제 ({selectedRecent.size})</button>}
                <button type="button" className={"recent-edit-btn" + (editingRecent ? " on" : "")} onClick={toggleRecentEdit}>{editingRecent ? "완료" : "편집"}</button>
              </div>
            </div>
            {editingRecent && <p className="recent-hint">삭제할 종목을 체크한 뒤 <strong>선택 삭제</strong>를 누르세요.</p>}
            <div className="recent-list">
              {recent.map((r) => (
                <div key={r.code} className={"recent-swipe-row" + (swipedRecent === r.code ? " swiped" : "")}
                  onTouchStart={(e) => { if (!editingRecent) (e.currentTarget as HTMLElement).dataset.touchX = String(e.touches[0].clientX); }}
                  onTouchEnd={(e) => { if (editingRecent) return; const el = e.currentTarget as HTMLElement; const startX = Number(el.dataset.touchX); const delta = startX - e.changedTouches[0].clientX; if (delta > 45) setSwipedRecent(r.code); else if (delta < -45) setSwipedRecent(null); delete el.dataset.touchX; }}>
                  <div className="recent-swipe-content">
                    {editingRecent && <input type="checkbox" className="recent-check" checked={selectedRecent.has(r.code)} onChange={() => toggleRecentSelection(r.code)} aria-label={r.name + " 선택"} />}
                    <a href={editingRecent ? undefined : "#/stock/" + r.code} className="recent-chip">{r.name} <span className="code">{r.code}</span></a>
                  </div>
                  <button type="button" className="recent-swipe-delete" onClick={() => deleteRecent(r.code)} aria-label={r.name + " 삭제"}>삭제</button>
                </div>
              ))}
            </div>
            {!editingRecent && <p className="recent-swipe-hint">모바일에서는 종목을 왼쪽으로 밀어 삭제할 수 있습니다.</p>}
          </div>
        )}      </section>

      <section>
        <div className="list-head">
          <h2>조회한 종목</h2>
          <div className="list-actions">
            <button type="button" className={`column-edit-btn${editingColumns ? " on" : ""}`} onClick={() => setEditingColumns((v) => !v)} aria-expanded={editingColumns}>
              편집
            </button>
            {editingColumns && (
              <div className="column-editor" role="dialog" aria-label="지표 순서 편집">
                <div className="column-editor-head"><strong>지표 순서</strong><button type="button" className="column-reset" onClick={resetColumns}>기본 순서</button></div>
                <p>▲ ▼ 버튼으로 메인 표의 지표 순서를 바꿀 수 있습니다.</p>
                <ol>
                  {columns.map((key, i) => <li key={key}><span>{COLUMN_LABELS[key]}</span><span className="column-move">
                    <button type="button" onClick={() => moveColumn(i, -1)} disabled={i === 0} aria-label={`${COLUMN_LABELS[key]} 위로`}>▲</button>
                    <button type="button" onClick={() => moveColumn(i, 1)} disabled={i === columns.length - 1} aria-label={`${COLUMN_LABELS[key]} 아래로`}>▼</button>
                  </span></li>)}
                </ol>
              </div>
            )}
          </div>
        </div>
        {indexError && <p className="err">목록을 불러오지 못했습니다: {indexError}</p>}
        {index && rows.length === 0 && <p className="empty-msg">아직 조회한 종목이 없습니다. 위에서 종목을 검색해 보세요.</p>}
        {rows.length > 0 && (
          <div className="table-wrap">
            <table className="grid">
              <thead><tr>
                <th className="stick">종목</th><th className="hide-sm">시장</th>
                <th className="num hide-sm">주가</th><th className="num hide-sm">예상 DPS</th>
                {columns.map((key) => (
                  <th key={key} className={`num metric-th${key === "multiple" ? " key" : ""}`}>
                    <div className="metric-title-wrap">
                      <button type="button" className="metric-title" onClick={() => toggleSort(key)} aria-label={`${COLUMN_LABELS[key]} 정렬`}>
                        {COLUMN_LABELS[key]} <span className="sort-indicator" aria-hidden="true">{sort.key === key ? (sort.direction === "asc" ? "↑" : "↓") : "↕"}</span>
                      </button>
                    </div>
                  </th>
                ))}
                <th className="hide-sm">기준일</th>
              </tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.code} onClick={() => { window.location.hash = `#/stock/${r.code}`; }}>
                    <td className="stick">
                      <a href={`#/stock/${r.code}`} className="name">{r.name}</a>
                      <span className="code">{r.code}<span className="show-sm"> {r.market}</span></span>
                    </td>
                    <td className="hide-sm">{r.market}</td>
                    <td className="num hide-sm">{won(r.price)}</td>
                    <td className="num hide-sm">{r.has_div_data ? won(r.dps) : NA}</td>
                    {columns.map((key) => {
                      if (key === "yield") return <td key={key} className="num">{pct(r.yield)}</td>;
                      if (key === "multiple") return <td key={key} className="num key">{mult(r.multiple)}</td>;
                      if (key === "paid10") return <td key={key} className="num">{r.paid10 == null ? NA : `${r.paid10}/10`}</td>;
                      return <td key={key}><BandGauge pct={r.pct} /></td>;
                    })}
                    <td className="hide-sm num">{r.price_date}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {index?.rules && (
        <details className="rules">
          <summary>계산 규칙과 데이터 사용 기준</summary>
          <ol>{index.rules.map((r, i) => <li key={i}>{r}</li>)}</ol>
        </details>
      )}
    </main>
  );
}

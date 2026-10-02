import { useEffect, useMemo, useRef, useState } from "react";
import type { CacheIndex, MasterStock } from "../lib/types";
import { mult, pct, won, NA } from "../lib/format";
import { loadRecent, pushRecent, removeRecent, removeRecentMany, type RecentEntry } from "../lib/recent";
import { cachedLiveIndex, fetchLiveIndex, type LiveIndex } from "../lib/liveIndex";
import { disableSync, enableSync, hasEditAccess, joinSync, loadEditKey, loadSyncCode, syncPull, syncPush } from "../lib/sync";
import { codeToKorean } from "../lib/syncWords";
import { BandGauge } from "../components/BandGauge";
import { SearchBox } from "../components/SearchBox";

type MetricKey = "yield" | "multiple" | "paid10" | "pct";
type SortDirection = "asc" | "desc";
type SortState = { key: MetricKey; direction: SortDirection };
type ColumnOrder = MetricKey[];

const DEFAULT_COLUMNS: ColumnOrder = ["yield", "multiple", "paid10", "pct"];
const COLUMN_ORDER_KEY = "dividend-10y-home-column-order-v1";
const SORT_KEY = "dividend-10y-home-sort-v1";
const NAME_SORT_KEY = "dividend-10y-home-name-sort-v1";

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

function loadNameSort(): SortDirection | null {
  try {
    const raw = localStorage.getItem(NAME_SORT_KEY);
    if (raw === "asc" || raw === "desc") return raw;
  } catch {}
  return null;
}

export function Home({ master, index, indexError, onPick }: {
  master: MasterStock[] | null; index: CacheIndex | null; indexError: string | null; onPick: (s: MasterStock) => void;
}) {
  const [columns, setColumns] = useState<ColumnOrder>(() => loadColumnOrder());
  const [editingColumns, setEditingColumns] = useState(false);
  const [sort, setSort] = useState<SortState>(() => loadSort());
  const [nameSortDirection, setNameSortDirection] = useState<SortDirection | null>(() => loadNameSort());
  const [recent, setRecent] = useState<RecentEntry[]>(() => loadRecent());
  const [editingRecent, setEditingRecent] = useState(false);
  const [selectedRecent, setSelectedRecent] = useState<Set<string>>(() => new Set());
  const [swipedRecent, setSwipedRecent] = useState<string | null>(null);
  const [syncCode, setSyncCode] = useState<string | null>(() => loadSyncCode());
  const [syncEditKey, setSyncEditKey] = useState<string | null>(() => loadEditKey());
  const [syncReadOnly, setSyncReadOnly] = useState(() => loadSyncCode() !== null && !hasEditAccess());
  const [syncOpen, setSyncOpen] = useState(false);
  const [syncShowEditKey, setSyncShowEditKey] = useState(false);
  const [syncJoinCode, setSyncJoinCode] = useState("");
  const [syncJoinEditKey, setSyncJoinEditKey] = useState("");
  const [syncBusy, setSyncBusy] = useState(false);
  const [syncError, setSyncError] = useState<string | null>(null);

  // 앱 진입 시 한 번: 동기화 중이면 다른 기기에서 생긴 변경을 끌어와 합친다. 동기화 중이 아니면 아무것도 안 함.
  useEffect(() => {
    syncPull().then((merged) => { if (merged) setRecent(merged); }).catch(() => { /* 로컬 목록은 그대로 */ });
  }, []);
  // '조회한 종목' 새로고침: KRX 전종목 잠정 시세로 주가·배당률·배수만 빠르게 덮어쓴다(역사적 위치는 확정값 유지).
  const [live, setLive] = useState<Extract<LiveIndex, { status: "live" }> | null>(() => cachedLiveIndex());
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);
  const refreshLive = () => {
    setLiveLoading(true);
    setLiveError(null);
    fetchLiveIndex()
      .then((res) => {
        if (res.status === "live") setLive(res);
        else setLiveError(`잠정 시세를 가져오지 못했습니다 (${res.reason}).`);
      })
      .catch((e) => setLiveError(`잠정 시세를 가져오지 못했습니다 (${(e as Error).message}).`))
      .finally(() => setLiveLoading(false));
  };
  const longPressTimer = useRef<number | null>(null);
  const longPressTriggered = useRef(false);

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

  const toggleNameSort = () => {
    setNameSortDirection((v) => {
      const next = v === "asc" ? "desc" : "asc";
      localStorage.setItem(NAME_SORT_KEY, next);
      return next;
    });
  };

  const toggleSort = (key: MetricKey) => {
    const next: SortState = sort.key === key
      ? { key, direction: sort.direction === "asc" ? "desc" : "asc" }
      : { key, direction: "desc" };
    setSort(next);
    setNameSortDirection(null);
    localStorage.setItem(SORT_KEY, JSON.stringify(next));
    localStorage.removeItem(NAME_SORT_KEY);
  };

  const rows = useMemo(() => {
    const recentCodes = new Set(recent.map((item) => item.code));
    const r = (index?.stocks ?? []).filter((stock) => recentCodes.has(stock.code)).map((stock) => {
      const lv = live?.rows[stock.code];
      // 확정 데이터가 더 최신이면 잠정값을 쓰지 않는다
      return lv && lv.price_date >= stock.price_date ? { ...stock, ...lv, isLive: true } : { ...stock, isLive: false };
    });
    const value = (row: typeof r[number], key: MetricKey): number => {
      if (key === "yield") return row.yield ?? -Infinity;
      if (key === "multiple") return row.multiple ?? -Infinity;
      if (key === "paid10") return row.paid10 ?? -Infinity;
      return row.pct ?? -Infinity;
    };
    r.sort((a, b) => {
      if (nameSortDirection !== null) return nameSortDirection === "asc"
        ? a.name.localeCompare(b.name, "ko")
        : b.name.localeCompare(a.name, "ko");
      const diff = value(a, sort.key) - value(b, sort.key);
      if (diff !== 0) return sort.direction === "asc" ? diff : -diff;
      return a.name.localeCompare(b.name, "ko");
    });
    return r;
  }, [index, sort, recent, nameSortDirection, live]);

  const deleteRecent = (code: string) => {
    if (!window.confirm("최근 조회 목록에서 삭제할까요?")) return;
    const next = removeRecent(code);
    setRecent(next);
    syncPush(next);
    setSelectedRecent((prev) => { const next = new Set(prev); next.delete(code); return next; });
    setSwipedRecent(null);
  };
  const toggleRecentSelection = (code: string) => {
    setSelectedRecent((prev) => { const next = new Set(prev); if (next.has(code)) next.delete(code); else next.add(code); return next; });
  };
  const deleteSelectedRecent = () => {
    if (selectedRecent.size === 0) return;
    if (!window.confirm("선택한 " + selectedRecent.size + "개 종목을 최근 조회 목록에서 삭제할까요?")) return;
    const next = removeRecentMany(selectedRecent);
    setRecent(next);
    syncPush(next);
    setSelectedRecent(new Set());
    setEditingRecent(false);
  };

  const addMultipleRecent = (stocks: MasterStock[]) => {
    let next = recent;
    for (const s of [...stocks].reverse()) next = pushRecent({ code: s.code, name: s.name, market: s.market });
    setRecent(next);
    syncPush(next);
  };

  const handleEnableSync = () => {
    setSyncBusy(true);
    setSyncError(null);
    enableSync()
      .then(({ code, editKey }) => { setSyncCode(code); setSyncEditKey(editKey); setSyncReadOnly(false); setSyncShowEditKey(true); })
      .catch((e) => setSyncError(`동기화 코드를 만들지 못했습니다 (${(e as Error).message}).`))
      .finally(() => setSyncBusy(false));
  };
  const handleJoinSync = () => {
    setSyncBusy(true);
    setSyncError(null);
    joinSync(syncJoinCode, syncJoinEditKey)
      .then(({ recent: merged, readOnly }) => {
        setRecent(merged);
        setSyncCode(loadSyncCode());
        setSyncEditKey(loadEditKey());
        setSyncReadOnly(readOnly);
        setSyncJoinCode("");
        setSyncJoinEditKey("");
      })
      .catch((e) => setSyncError(`코드를 적용하지 못했습니다 (${(e as Error).message}).`))
      .finally(() => setSyncBusy(false));
  };
  const handleDisableSync = () => {
    if (!window.confirm("이 기기의 동기화를 끌까요? (다른 기기의 목록과 코드는 그대로 남습니다)")) return;
    disableSync();
    setSyncCode(null);
    setSyncEditKey(null);
    setSyncReadOnly(false);
    setSyncError(null);
  };
  const startRowLongPress = (code: string) => {
    longPressTriggered.current = false;
    if (longPressTimer.current !== null) window.clearTimeout(longPressTimer.current);
    longPressTimer.current = window.setTimeout(() => {
      longPressTriggered.current = true;
      deleteRecent(code);
    }, 650);
  };
  const cancelRowLongPress = () => {
    if (longPressTimer.current !== null) {
      window.clearTimeout(longPressTimer.current);
      longPressTimer.current = null;
    }
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
        <div className="masthead-title"><h1>배당수익률 ÷ 미국 10년물</h1><span className="app-version" title={`배포 커밋: ${import.meta.env.VITE_APP_SHA || "개발 버전"}`}>Build #{import.meta.env.VITE_APP_BUILD || "dev"}<span className="app-version-sep">·</span>{(import.meta.env.VITE_APP_SHA || "local").slice(0, 7)}</span></div>
      </header>

      <section className="search-wrap">
        <SearchBox master={master} onPick={onPick} onAddMultiple={addMultipleRecent} autoFocus />
        <div className="sync-section">
          <button type="button" className={"sync-toggle-btn" + (syncOpen ? " on" : "")} onClick={() => { setSyncOpen((v) => !v); setSyncError(null); }}>
            {syncCode ? (syncReadOnly ? "다른 기기 목록 보는 중 (보기 전용)" : "다른 기기와 동기화 중") : "다른 기기와 최근 조회 동기화"}
          </button>
          {syncOpen && (
            <div className="sync-panel">
              {syncCode ? (
                <>
                  <p className="sync-desc">
                    <strong>보기 코드</strong> — 아는 사람은 누구나 이 목록을 볼 수 있습니다.
                  </p>
                  <div className="sync-code-row"><span className="sync-code">{codeToKorean(syncCode)}</span></div>
                  {syncEditKey ? (
                    <>
                      <p className="sync-desc">
                        <strong>편집 키</strong> — 내 다른 기기에만 입력하세요. 이 키를 아는 기기만 목록을 바꿀 수 있습니다.
                      </p>
                      {syncShowEditKey ? (
                        <div className="sync-code-row">
                          <span className="sync-code sync-code-secret">{codeToKorean(syncEditKey)}</span>
                          <button type="button" className="sync-edit-btn" onClick={() => setSyncShowEditKey(false)}>가리기</button>
                        </div>
                      ) : (
                        <button type="button" className="sync-edit-btn" onClick={() => setSyncShowEditKey(true)}>편집 키 보기</button>
                      )}
                    </>
                  ) : (
                    <p className="sync-desc sync-readonly-note">
                      보기 전용입니다 — 이 기기에서는 목록을 바꿀 수 없습니다. 편집하려면 편집 키를 알아야 합니다.
                    </p>
                  )}
                  <button type="button" className="sync-off-btn" onClick={handleDisableSync}>이 기기 동기화 끄기</button>
                </>
              ) : (
                <>
                  <p className="sync-desc">코드를 만들어 다른 기기에 입력하거나, 다른 기기에서 받은 코드를 입력하세요.</p>
                  <button type="button" className="sync-edit-btn" onClick={handleEnableSync} disabled={syncBusy}>코드 만들기</button>
                  <div className="sync-join-fields">
                    <input type="text" inputMode="text" placeholder="보기 코드 (예: 사과 호랑이 의자)" value={syncJoinCode}
                      onChange={(e) => setSyncJoinCode(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") handleJoinSync(); }} className="sync-join-input" />
                    <input type="text" inputMode="text" placeholder="편집 키 (내 기기면 입력, 그냥 보기만 하면 비워두세요)" value={syncJoinEditKey}
                      onChange={(e) => setSyncJoinEditKey(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter") handleJoinSync(); }} className="sync-join-input" />
                    <button type="button" className="sync-edit-btn" onClick={handleJoinSync} disabled={syncBusy || syncJoinCode.trim().length === 0}>코드 입력</button>
                  </div>
                </>
              )}
              {syncError && <p className="sync-error">{syncError}</p>}
            </div>
          )}
        </div>
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
          <div className="list-title">
            <h2>조회한 종목</h2>
            <button type="button" className="list-refresh" onClick={refreshLive} disabled={liveLoading}
              title="조회한 종목을 당일 주가(KRX 잠정 시세)로 빠르게 갱신합니다" aria-label={liveLoading ? "당일 주가 불러오는 중" : "당일 주가로 새로고침"}>
              <span className={`refresh-icon${liveLoading ? " spin" : ""}`} aria-hidden>⟳</span>
            </button>
          </div>
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
        {liveError && <p className="err">{liveError}</p>}
        {live && !liveError && rows.some((r) => r.isLive) && (
          <p className="live-note">
            {new Date(live.fetched_at).toLocaleTimeString("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit" })} KRX 잠정 시세({live.trade_date}) 반영 · 역사적 위치는 마지막 확정 계산 기준
          </p>
        )}
        {index && rows.length === 0 && <p className="empty-msg">아직 조회한 종목이 없습니다. 위에서 종목을 검색해 보세요.</p>}
        {rows.length > 0 && (
          <div className="table-wrap">
            <table className="grid">
              <thead><tr>
                <th className="stick"><button type="button" className="stock-name-sort" onClick={() => toggleNameSort()} aria-label="기업 이름 정렬">종목 <span className="sort-indicator" aria-hidden="true">{nameSortDirection === "asc" ? "↑" : nameSortDirection === "desc" ? "↓" : "↕"}</span></button></th><th className="hide-sm">시장</th>
                <th className="num hide-sm">주가</th><th className="num hide-sm">예상 DPS</th>
                {columns.map((key) => (
                  <th key={key} className={`num metric-th${key === "multiple" ? " key" : ""}${key === "paid10" ? " hide-sm" : ""}`}>
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
                  <tr key={r.code}
                    onClick={() => { window.location.hash = `#/stock/${r.code}`; }}>
                    <td className="stick">
                      <a href={`#/stock/${r.code}`} className="name"
                        onTouchStart={() => startRowLongPress(r.code)}
                        onTouchEnd={cancelRowLongPress}
                        onTouchMove={cancelRowLongPress}
                        onTouchCancel={cancelRowLongPress}
                        onClick={(e) => {
                          if (longPressTriggered.current) {
                            e.preventDefault();
                            e.stopPropagation();
                            longPressTriggered.current = false;
                          }
                        }}
                        onContextMenu={(e) => e.preventDefault()}>
                        <span className="name-text">{r.name}</span>
                        {r.tags.length > 0 && (
                          <span className="name-tags">
                            {r.tags.includes("crown") && (
                              <span className="tag-icon" role="img" aria-label="연속 10년 배당, 삭감 없음" title="연속 10년 배당, 삭감 없음">👑</span>
                            )}
                            {r.tags.includes("bomb") && (
                              <span className="tag-icon" role="img" aria-label="올해 배당 전년 대비 50%+ 증가" title="올해 배당 전년 대비 50%+ 증가">💣</span>
                            )}
                          </span>
                        )}
                      </a>
                      <span className="code">{r.code}<span className="show-sm"> {r.market}</span></span>
                    </td>
                    <td className="hide-sm">{r.market}</td>
                    <td className={`num hide-sm${r.isLive ? " prov" : ""}`}>{won(r.price)}</td>
                    <td className="num hide-sm">{r.has_div_data ? won(r.dps) : NA}</td>
                    {columns.map((key) => {
                      if (key === "yield") return <td key={key} className={`num${r.isLive ? " prov" : ""}`}>{pct(r.yield)}</td>;
                      if (key === "multiple") return <td key={key} className={`num key${r.isLive ? " prov" : ""}`}>{mult(r.multiple)}</td>;
                      if (key === "paid10") return <td key={key} className="num hide-sm">{r.paid10 == null ? NA : `${r.paid10}/10`}</td>;
                      return <td key={key}><BandGauge pct={r.pct} /></td>;
                    })}
                    <td className="hide-sm num">{r.price_date}{r.isLive && <span className="prov-tag"> 잠정</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <div className="home-notice">
        <p className="note">처음 조회하는 종목은 2016년부터 가격·배당을 모으느라 1~3분 걸립니다. 한 번 조회한 종목은 저장해 두고 다음부터는 새 데이터만 확인합니다.</p>
      </div>

      {index?.rules && (
        <details className="rules">
          <summary>계산 규칙과 데이터 사용 기준</summary>
          <ol>{index.rules.map((r, i) => <li key={i}>{r}</li>)}</ol>
        </details>
      )}

      <footer className="home-footer-figures">
        <dl className="headline-figures">
          <div><dt>미국 10년물 데이터</dt><dd>{us?.data_through ?? NA}</dd></div>
          <div><dt>조회한 종목</dt><dd>{recent.length}개</dd></div>
        </dl>
      </footer>
    </main>
  );
}

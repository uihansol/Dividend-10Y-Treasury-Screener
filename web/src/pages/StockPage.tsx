import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { getRun, getStock, requestRefresh } from "../lib/data";
import type { Detail, MasterStock, RunInfo } from "../lib/types";
import { pushRecent } from "../lib/recent";
import { SearchBox } from "../components/SearchBox";

const DetailPage = lazy(() => import("./Detail").then((m) => ({ default: m.DetailPage })));

type Phase =
  | { kind: "loading" }
  | { kind: "preparing"; since: number; run?: RunInfo }           // 캐시 없음 → 최초 수집 중
  | { kind: "ready"; data: Detail; refreshing: boolean; note?: string }
  | { kind: "error"; message: string };

const POLL_MS = 6000;
const INTRADAY_REFRESH_MS = 45_000; // 장중 화면 최신성 확인
const AUTO_REFRESH_REQUEST_MS = 5 * 60_000; // 실제 서버 수집 요청은 5분에 한 번으로 제한
const DAY_CHANGE_CHECK_MS = 30_000; // KST 날짜 변경 확인
const MARKET_OPEN_MIN = 9 * 60;
const MARKET_CLOSE_MIN = 15 * 60 + 30;
const STEPS = [
  "가격 데이터 확인 (2016년~, KRX)",
  "배당 데이터 확인 (2015년~, DART 정기보고서)",
  "미국 10년물 데이터 확인 (FRED)",
  "분석 계산 중",
];

/** Actions step 상태 → 각 단계 표시: done | current | todo */
function stepStates(run?: RunInfo): ("done" | "current" | "todo")[] {
  const st = run?.steps ?? [];
  return STEPS.map((_, i) => {
    const s = st.find((x) => x.name.startsWith("①②③④"[i]));
    return s?.status === "completed" ? "done" : s?.status === "in_progress" ? "current" : "todo";
  });
}
const TIMEOUT_MS = 10 * 60 * 1000;

export function StockPage({ code, master, onPick }: {
  code: string; master: MasterStock[] | null; onPick: (s: MasterStock) => void;
}) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [now, setNow] = useState(Date.now());
  const alive = useRef(true);
  const name = master?.find((s) => s.code === code)?.name ?? code;

  /** force=true: 캐시가 최신이어도(쿨다운 무시) 다시 수집·계산을 요청한다. 새로고침 버튼이 쓴다. */
  const lastAutoRefreshAt = useRef(0);

  const run = useCallback(async (force: boolean, autoRefresh = false) => {
    try {
      const first = await getStock(code);
      if (!alive.current) return;
      const shouldRequestAutoRefresh = autoRefresh
        && Date.now() - lastAutoRefreshAt.current >= AUTO_REFRESH_REQUEST_MS;
      if (!force && !shouldRequestAutoRefresh && first.status === "ready" && !first.stale) {
        setPhase({ kind: "ready", data: first.analysis, refreshing: false });
        return;
      }
      const before = first.status === "ready" ? first.analysis.metadata?.last_attempt : undefined;
      if (first.status === "ready") setPhase({ kind: "ready", data: first.analysis, refreshing: true });
      else setPhase({ kind: "preparing", since: Date.now() });

      if (autoRefresh) lastAutoRefreshAt.current = Date.now();
      const r = await requestRefresh(code, force);
      if (r.status === "error") throw new Error(r.error ?? "업데이트 요청 실패");
      const started = Date.now();
      while (alive.current && Date.now() - started < TIMEOUT_MS) {
        await new Promise((res) => setTimeout(res, POLL_MS));
        if (!alive.current) return;
        // fresh=true: 캐시(메모리·엣지)를 건너뛰고 항상 최신을 본다 — 그렇지 않으면 새로고침이
        // 끝나도 캐시된 옛 응답만 반복해서 받아 완료를 영영 감지하지 못한다.
        const [s, runInfo] = await Promise.all([getStock(code, true), getRun(code).catch(() => ({ run: null }))]);
        if (s.status === "ready" && s.analysis.metadata?.last_attempt !== before) {
          const errs = s.analysis.metadata?.last_error;
          setPhase({ kind: "ready", data: s.analysis, refreshing: false,
            note: errs ? `일부 데이터 업데이트 실패(${Object.keys(errs).join(", ")}) — 기존 캐시로 계산했습니다.` : undefined });
          return;
        }
        const failed = runInfo.run && runInfo.run.status === "completed" && runInfo.run.conclusion !== "success"
          && new Date(runInfo.run.created_at).getTime() >= started - 60_000;
        if (failed) {
          if (s.status === "ready") {
            setPhase({ kind: "ready", data: s.analysis, refreshing: false, note: "최신 데이터 확인에 실패해 저장된 데이터를 보여줍니다." });
          } else {
            setPhase({ kind: "error", message: `${name} 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해주세요.` });
          }
          return;
        }
        setPhase((p) => (p.kind === "preparing" ? { ...p, run: runInfo.run ?? undefined } : p));
      }
      if (alive.current) {
        setPhase((p) => (p.kind === "ready" ? { ...p, refreshing: false, note: "최신 데이터 확인이 오래 걸려 저장된 데이터를 보여줍니다." }
          : { kind: "error", message: `${name} 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해주세요.` }));
      }
    } catch (e) {
      if (alive.current) setPhase({ kind: "error", message: `서버에 연결하지 못했습니다 (${(e as Error).message}).` });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  // 페이지를 계속 열어 둔 상태에서 KST 날짜가 바뀌면 최신 증분 데이터를 확인한다.
  const dayKey = () => new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Seoul",
    year: "numeric", month: "2-digit", day: "2-digit",
  }).format(new Date());

  useEffect(() => {
    alive.current = true;
    setPhase({ kind: "loading" });
    let lastDay = dayKey();

    const isKstWeekday = (d: Date) => {
      const weekday = new Intl.DateTimeFormat("en-US", {
        timeZone: "Asia/Seoul", weekday: "short",
      }).format(d);
      return weekday !== "Sat" && weekday !== "Sun";
    };
    const kstMinutes = (d: Date) => {
      const parts = new Intl.DateTimeFormat("en-US", {
        timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", hour12: false,
      }).formatToParts(d);
      return Number(parts.find((p) => p.type === "hour")?.value ?? 0) * 60
        + Number(parts.find((p) => p.type === "minute")?.value ?? 0);
    };
    const isKstMarketHours = () => {
      const d = new Date();
      const m = kstMinutes(d);
      return isKstWeekday(d) && m >= MARKET_OPEN_MIN && m <= MARKET_CLOSE_MIN;
    };

    const clock = setInterval(() => setNow(Date.now()), 1000);

    // KST 날짜 변경: 하루가 바뀌면 배당·가격·미국10Y의 증분 갱신을 한 번 확인한다.
    const dayWatcher = setInterval(() => {
      const currentDay = dayKey();
      if (currentDay !== lastDay) {
        lastDay = currentDay;
        run(false);
      }
    }, DAY_CHANGE_CHECK_MS);

    // 장중에는 45초마다 최신 가격을 확인한다.
    // 서버의 증분 수집/쿨다운이 실제 외부 API 호출 빈도를 제어하므로
    // 브라우저에서 빈번하게 확인해도 과도한 전체 데이터 재수집은 하지 않는다.
    const intradayWatcher = setInterval(() => {
      if (isKstMarketHours() && alive.current) {
        run(false, true);
      }
    }, INTRADAY_REFRESH_MS);

    run(false);

    return () => {
      alive.current = false;
      clearInterval(clock);
      clearInterval(dayWatcher);
      clearInterval(intradayWatcher);
    };
  }, [code, run]);

  // 방문 기록(브라우저 로컬)에 남긴다. 화면에 데이터가 뜬 시점(=code가 유효했던 시점)에만 남긴다.
  useEffect(() => {
    if (phase.kind === "ready") pushRecent({ code: phase.data.code, name: phase.data.name, market: phase.data.market });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase.kind === "ready" ? phase.data.code : null]);

  const refreshing = phase.kind === "ready" && phase.refreshing;

  return (
    <>
      <div className="page top-search">
        <a href="#/" className="back" aria-label="조회한 종목으로 돌아가기" title="조회한 종목으로 돌아가기"><span aria-hidden="true">‹</span><span className="back-text">조회한 종목</span></a>
        <div className="detail-search"><SearchBox master={master} onPick={onPick} /></div>
        {phase.kind === "ready" && (
          <button type="button" className="btn refresh-btn" disabled={refreshing} onClick={() => run(true)}
            title="이 종목의 최신 가격·배당 데이터를 다시 불러옵니다"
            aria-label={refreshing ? "새로고침 중" : "최신 데이터 새로고침"}>
            <span className={`refresh-icon${refreshing ? " spin" : ""}`} aria-hidden>⟳</span>
          </button>
        )}
      </div>
      {phase.kind === "loading" && <main className="page"><StockSkeleton /></main>}
      {phase.kind === "error" && <main className="page"><p className="err big">{phase.message}</p></main>}
      {phase.kind === "preparing" && (
        <main className="page">
          <section className="panel preparing" aria-live="polite">
            <h2>{name} 데이터를 준비하고 있습니다.</h2>
            <ol className="steps">
              {stepStates(phase.run).map((st, i) => (
                <li key={i} className={`step-${st}`}>
                  <span className="step-mark" aria-hidden>{"①②③④"[i]}</span> {STEPS[i]}
                  {st === "done" && <span className="step-tag"> 완료</span>}
                  {st === "current" && <span className="step-tag"> 진행 중…</span>}
                </li>
              ))}
            </ol>
            <p className="note">
              {!phase.run ? "작업 요청 중" : phase.run.status === "queued" ? "작업 대기 중"
                : stepStates(phase.run).every((x) => x === "todo") ? "실행 환경 준비 중" : "작업 실행 중"} ·
              경과 {Math.floor((now - phase.since) / 1000)}초 · 보통 1~3분 걸립니다.
              {phase.run && <> <a href={phase.run.url} target="_blank" rel="noreferrer">실행 기록</a></>}
            </p>
          </section>
        </main>
      )}
      {phase.kind === "ready" && (
        <>
          {(phase.refreshing || phase.note) && (
            <div className="page"><p className={`banner${phase.note ? " warn" : ""}`} aria-live="polite">
              {phase.refreshing ? `${name} 최신 데이터 확인 중…` : phase.note}
            </p></div>
          )}
          <Suspense fallback={<main className="page"><StockSkeleton chartOnly /></main>}>
            <DetailPage d={phase.data} />
          </Suspense>
        </>
      )}
    </>
  );
}

/** 실제 종목 페이지 뼈대(헤더 수치·차트 자리)를 흉내 낸 자리표시자. "불러오는 중…" 문구 대신 쓴다. */
function StockSkeleton({ chartOnly = false }: { chartOnly?: boolean }) {
  return (
    <div className="skeleton" aria-hidden aria-busy="true">
      {!chartOnly && (
        <>
          <div className="sk-line sk-title" />
          <div className="sk-cards">
            {Array.from({ length: 5 }, (_, i) => <div key={i} className="sk-card" />)}
          </div>
        </>
      )}
      <div className="sk-block" />
    </div>
  );
}

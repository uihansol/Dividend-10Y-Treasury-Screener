import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { getRun, getStock, requestRefresh } from "../lib/data";
import type { Detail, MasterStock, RunInfo } from "../lib/types";
import { SearchBox } from "../components/SearchBox";

const DetailPage = lazy(() => import("./Detail").then((m) => ({ default: m.DetailPage })));

type Phase =
  | { kind: "loading" }
  | { kind: "preparing"; since: number; run?: RunInfo }           // 캐시 없음 → 최초 수집 중
  | { kind: "ready"; data: Detail; refreshing: boolean; note?: string }
  | { kind: "error"; message: string };

const POLL_MS = 8000;
const TIMEOUT_MS = 10 * 60 * 1000;

export function StockPage({ code, master, onPick }: {
  code: string; master: MasterStock[] | null; onPick: (s: MasterStock) => void;
}) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const [now, setNow] = useState(Date.now());
  const alive = useRef(true);
  const name = master?.find((s) => s.code === code)?.name ?? code;

  useEffect(() => {
    alive.current = true;
    setPhase({ kind: "loading" });
    const t = setInterval(() => setNow(Date.now()), 1000);
    run().catch((e) => alive.current && setPhase({ kind: "error", message: `서버에 연결하지 못했습니다 (${e.message}).` }));
    return () => { alive.current = false; clearInterval(t); };

    async function run() {
      const first = await getStock(code);
      if (!alive.current) return;
      if (first.status === "ready" && !first.stale) {
        setPhase({ kind: "ready", data: first.analysis, refreshing: false });
        return;
      }
      const before = first.status === "ready" ? first.analysis.metadata?.last_attempt : undefined;
      if (first.status === "ready") setPhase({ kind: "ready", data: first.analysis, refreshing: true });
      else setPhase({ kind: "preparing", since: Date.now() });

      const r = await requestRefresh(code);
      if (r.status === "error") throw new Error(r.error ?? "업데이트 요청 실패");
      const started = Date.now();
      while (alive.current && Date.now() - started < TIMEOUT_MS) {
        await new Promise((res) => setTimeout(res, POLL_MS));
        if (!alive.current) return;
        const [s, run] = await Promise.all([getStock(code), getRun(code).catch(() => ({ run: null }))]);
        if (s.status === "ready" && s.analysis.metadata?.last_attempt !== before) {
          const errs = s.analysis.metadata?.last_error;
          setPhase({ kind: "ready", data: s.analysis, refreshing: false,
            note: errs ? `일부 데이터 업데이트 실패(${Object.keys(errs).join(", ")}) — 기존 캐시로 계산했습니다.` : undefined });
          return;
        }
        const failed = run.run && run.run.status === "completed" && run.run.conclusion !== "success"
          && new Date(run.run.created_at).getTime() >= started - 60_000;
        if (failed) {
          if (s.status === "ready") {
            setPhase({ kind: "ready", data: s.analysis, refreshing: false, note: "최신 데이터 확인에 실패해 저장된 데이터를 보여줍니다." });
          } else {
            setPhase({ kind: "error", message: `${name} 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해주세요.` });
          }
          return;
        }
        setPhase((p) => (p.kind === "preparing" ? { ...p, run: run.run ?? undefined } : p));
      }
      if (alive.current) {
        setPhase((p) => (p.kind === "ready" ? { ...p, refreshing: false, note: "최신 데이터 확인이 오래 걸려 저장된 데이터를 보여줍니다." }
          : { kind: "error", message: `${name} 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해주세요.` }));
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [code]);

  return (
    <>
      <div className="page top-search">
        <a href="#/" className="back">← 조회한 종목</a>
        <SearchBox master={master} onPick={onPick} />
      </div>
      {phase.kind === "loading" && <main className="page"><p className="loading">{name} 불러오는 중…</p></main>}
      {phase.kind === "error" && <main className="page"><p className="err big">{phase.message}</p></main>}
      {phase.kind === "preparing" && (
        <main className="page">
          <section className="panel preparing" aria-live="polite">
            <h2>{name} 데이터를 준비하고 있습니다.</h2>
            <ol className="steps">
              <li>가격 데이터 확인 (2016년~, KRX)</li>
              <li>배당 데이터 확인 (2015년~, DART 정기보고서)</li>
              <li>미국 10년물 데이터 확인 (FRED)</li>
              <li>분석 계산 중</li>
            </ol>
            <p className="note">
              {phase.run ? (phase.run.status === "queued" ? "작업 대기 중" : "작업 실행 중") : "작업 요청 중"} ·
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
          <Suspense fallback={<main className="page"><p className="loading">불러오는 중…</p></main>}>
            <DetailPage d={phase.data} />
          </Suspense>
        </>
      )}
    </>
  );
}

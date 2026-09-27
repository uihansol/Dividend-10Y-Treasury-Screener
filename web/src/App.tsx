import { lazy, Suspense, useEffect, useState } from "react";
import { loadMeta, loadScreener } from "./lib/data";
import type { Meta, ScreenerRow } from "./lib/types";
import { Screener } from "./pages/Screener";
const DetailPage = lazy(() => import("./pages/Detail").then((m) => ({ default: m.DetailPage })));
import { DataStatus } from "./components/DataStatus";

function useHashRoute(): string {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => { setHash(window.location.hash); window.scrollTo(0, 0); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

export function App() {
  const [meta, setMeta] = useState<Meta | null>(null);
  const [rows, setRows] = useState<ScreenerRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const hash = useHashRoute();

  useEffect(() => {
    Promise.all([loadMeta(), loadScreener()])
      .then(([m, r]) => { setMeta(m); setRows(r); })
      .catch((e) => setError(String(e)));
  }, []);

  if (error) {
    return (
      <main className="page empty">
        <h1>계산된 데이터가 없습니다</h1>
        <p>data/meta.json을 읽지 못했습니다 ({error}).</p>
        <p>저장소의 README “데이터 최초 구축” 순서대로 파이프라인을 실행하면 이 화면이 스크리너로 바뀝니다.</p>
      </main>
    );
  }
  if (!meta || !rows) return <main className="page"><p className="loading">불러오는 중…</p></main>;

  const m = hash.match(/^#\/stock\/(\d{6})/);
  return (
    <>
      {m ? (
        <Suspense fallback={<main className="page"><p className="loading">불러오는 중…</p></main>}>
          <DetailPage code={m[1]} meta={meta} />
        </Suspense>
      ) : <Screener meta={meta} rows={rows} />}
      <div className="page"><DataStatus meta={meta} /></div>
    </>
  );
}

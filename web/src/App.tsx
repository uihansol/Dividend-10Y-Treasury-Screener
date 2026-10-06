import { useEffect, useState } from "react";
import { loadIndex, loadMaster } from "./lib/data";
import type { CacheIndex, MasterStock } from "./lib/types";
import { Home } from "./pages/Home";
import { StockPage } from "./pages/StockPage";
import { KrxAlert } from "./components/KrxAlert";
import { TokenAlert } from "./components/TokenAlert";

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
  const [master, setMaster] = useState<MasterStock[] | null>(null);
  const [index, setIndex] = useState<CacheIndex | null>(null);
  const [indexError, setIndexError] = useState<string | null>(null);
  const hash = useHashRoute();
  const m = hash.match(/^#\/stock\/(\d{6})/);

  useEffect(() => { loadMaster().then(setMaster).catch(() => setMaster([])); }, []);
  const reloadIndex = () =>
    loadIndex().then((i) => { setIndex(i); setIndexError(null); }).catch((e) => setIndexError(String(e.message ?? e)));
  useEffect(() => {
    if (m) return;
    reloadIndex();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [m?.[1]]);   // 목록 화면으로 돌아올 때마다 새로 읽음

  const onPick = (s: MasterStock) => { window.location.hash = `#/stock/${s.code}`; };
  return (
    <>
      <KrxAlert />
      <TokenAlert />
      {m ? <StockPage key={m[1]} code={m[1]} master={master} onPick={onPick} />
        : <Home master={master} index={index} indexError={indexError} onPick={onPick} reloadIndex={reloadIndex} />}
    </>
  );
}

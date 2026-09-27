import type { Detail, Meta, ScreenerRow } from "./types";

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json() as Promise<T>;
}

export const loadMeta = () => getJson<Meta>("data/meta.json");
export const loadScreener = () => getJson<ScreenerRow[]>("data/screener.json");
export const loadDetail = (code: string) => getJson<Detail>(`data/stocks/${code}.json`);

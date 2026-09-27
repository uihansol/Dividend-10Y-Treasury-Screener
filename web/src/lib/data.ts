import type { CacheIndex, Detail, MasterStock, RunInfo } from "./types";

async function getJson<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { cache: "no-cache", ...init });
  if (!res.ok) {
    // Worker가 {"error": "..."} 형태로 원인을 돌려주면 그 메시지를 보여준다 (없으면 상태 코드만)
    const body = await res.json().catch(() => null);
    throw new Error(body?.error ? `${path}: ${body.error}` : `${path}: HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

/** 검색용 종목 목록. 정적 파일이며 검색 중에는 서버를 호출하지 않는다. */
export async function loadMaster(): Promise<MasterStock[]> {
  const j = await getJson<{ stocks: Record<string, MasterStock> }>("data/master.json");
  return Object.values(j.stocks);
}

export const loadIndex = () => getJson<CacheIndex>("api/index");

export type StockResponse =
  | { status: "missing"; code: string }
  | { status: "ready"; code: string; stale: boolean; analysis: Detail };

export const getStock = (code: string) => getJson<StockResponse>(`api/stock/${code}`);
/** force=true: 최신 여부와 상관없이(캐시 쿨다운 무시) 다시 수집·계산한다. 사용자가 "새로고침"을 눌렀을 때 쓴다. */
export const requestRefresh = (code: string, force = false) =>
  getJson<{ status: "queued" | "running" | "error"; run?: RunInfo; error?: string }>(`api/stock/${code}/refresh`, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ force }),
  });
export const getRun = (code: string) => getJson<{ run: RunInfo | null }>(`api/stock/${code}/run`);

// ---------------------------------------------------------------- 로컬 검색
const norm = (s: string) => s.normalize("NFKC").toLowerCase().replace(/[\s()\-·.,&]/g, "");

/** pipeline/master.py search()와 같은 규칙: 정확히 일치 > 앞부분 일치 > 포함 */
export function searchMaster(master: MasterStock[], q: string, limit = 10): MasterStock[] {
  const nq = norm(q);
  if (!nq) return [];
  const scored: [number, MasterStock][] = [];
  for (const s of master) {
    const names = [norm(s.name), ...(s.aliases || []).map(norm)];
    let rank = -1;
    if (s.code === nq || names.some((n) => n === nq)) rank = 0;
    else if (s.code.startsWith(nq) || names.some((n) => n.startsWith(nq))) rank = 1;
    else if (names.some((n) => n.includes(nq))) rank = 2;
    if (rank >= 0) scored.push([rank, s]);
  }
  scored.sort((a, b) => a[0] - b[0] || a[1].name.length - b[1].name.length || a[1].name.localeCompare(b[1].name));
  return scored.slice(0, limit).map((x) => x[1]);
}

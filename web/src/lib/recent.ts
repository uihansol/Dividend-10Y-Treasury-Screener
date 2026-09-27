import type { MasterStock } from "./types";

const KEY = "recent-stocks";
const MAX = 8;

type RecentEntry = { code: string; name: string; market: string; at: number };

function safeParse(): RecentEntry[] {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as RecentEntry[]) : [];
  } catch {
    return [];   // 프라이빗 모드 등으로 localStorage를 못 쓰면 그냥 빈 목록
  }
}

/** 브라우저에만 남는 개인 방문 기록. 서버로 보내지 않는다. */
export function loadRecent(): RecentEntry[] {
  return safeParse();
}

export function pushRecent(s: Pick<MasterStock, "code" | "name" | "market">): void {
  try {
    const list = safeParse().filter((r) => r.code !== s.code);
    list.unshift({ code: s.code, name: s.name, market: s.market, at: Date.now() });
    localStorage.setItem(KEY, JSON.stringify(list.slice(0, MAX)));
  } catch { /* 무시 */ }
}

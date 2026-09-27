import type { MasterStock } from "./types";

const KEY = "recent-stocks";

export type RecentEntry = { code: string; name: string; market: string; at: number };

function safeParse(): RecentEntry[] {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as RecentEntry[]) : [];
  } catch {
    return [];
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
    localStorage.setItem(KEY, JSON.stringify(list));
  } catch { /* 무시 */ }
}

export function removeRecent(code: string): RecentEntry[] {
  try {
    const next = safeParse().filter((r) => r.code !== code);
    localStorage.setItem(KEY, JSON.stringify(next));
    return next;
  } catch {
    return safeParse();
  }
}

export function removeRecentMany(codes: Iterable<string>): RecentEntry[] {
  try {
    const targets = new Set(codes);
    const next = safeParse().filter((r) => !targets.has(r.code));
    localStorage.setItem(KEY, JSON.stringify(next));
    return next;
  } catch {
    return safeParse();
  }
}

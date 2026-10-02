import type { MasterStock } from "./types";

const KEY = "recent-stocks";
// 삭제 기록(tombstone). 기기 간 동기화(sync.ts)에서, 다른 기기가 아직 그 삭제를 모른 채 자기
// 목록에 그 종목을 들고 있을 때 "최근 방문 vs 삭제 중 뭐가 더 최근이냐"를 판단하는 데만 쓴다.
// 동기화를 쓰지 않으면 이 값은 아무 데도 전송되지 않는다.
const DELETED_KEY = "recent-stocks-deleted";
const DELETED_MAX = 200;

export type RecentEntry = { code: string; name: string; market: string; at: number };
export type Tombstone = { code: string; at: number };

function safeParse(): RecentEntry[] {
  try {
    const raw = localStorage.getItem(KEY);
    return raw ? (JSON.parse(raw) as RecentEntry[]) : [];
  } catch {
    return [];
  }
}

function safeParseDeleted(): Tombstone[] {
  try {
    const raw = localStorage.getItem(DELETED_KEY);
    return raw ? (JSON.parse(raw) as Tombstone[]) : [];
  } catch {
    return [];
  }
}

/** 브라우저에만 남는 개인 방문 기록. 동기화(sync.ts)를 켜지 않으면 서버로 보내지 않는다. */
export function loadRecent(): RecentEntry[] {
  return safeParse();
}

export function loadDeleted(): Tombstone[] {
  return safeParseDeleted();
}

function recordDeleted(codes: string[]): void {
  try {
    const now = Date.now();
    const kept = safeParseDeleted().filter((t) => !codes.includes(t.code));
    const next = [...codes.map((code) => ({ code, at: now })), ...kept].slice(0, DELETED_MAX);
    localStorage.setItem(DELETED_KEY, JSON.stringify(next));
  } catch { /* 무시 — 동기화 중이 아니면 영향 없고, 동기화 중이어도 로컬 삭제 자체는 이미 끝났다 */ }
}

export function pushRecent(s: Pick<MasterStock, "code" | "name" | "market">): RecentEntry[] {
  try {
    const list = safeParse().filter((r) => r.code !== s.code);
    list.unshift({ code: s.code, name: s.name, market: s.market, at: Date.now() });
    localStorage.setItem(KEY, JSON.stringify(list));
    return list;
  } catch {
    return safeParse();
  }
}

/** 동기화(기기 간 코드 합치기 등)로 통째로 교체할 때 쓴다. 일반적인 조회·삭제는 위 함수들을 쓴다. */
export function replaceRecent(list: RecentEntry[]): RecentEntry[] {
  try {
    localStorage.setItem(KEY, JSON.stringify(list));
    return list;
  } catch {
    return safeParse();
  }
}

export function replaceDeleted(list: Tombstone[]): Tombstone[] {
  try {
    localStorage.setItem(DELETED_KEY, JSON.stringify(list.slice(0, DELETED_MAX)));
    return list;
  } catch {
    return safeParseDeleted();
  }
}

export function removeRecent(code: string): RecentEntry[] {
  try {
    const next = safeParse().filter((r) => r.code !== code);
    localStorage.setItem(KEY, JSON.stringify(next));
    recordDeleted([code]);
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
    recordDeleted([...targets]);
    return next;
  } catch {
    return safeParse();
  }
}

import { getJson } from "./data";
import { loadDeleted, loadRecent, replaceDeleted, replaceRecent, type RecentEntry, type Tombstone } from "./recent";

/** 기기 간 '최근 조회' 동기화. 로그인 없이, 사용자가 기기 사이에 직접 옮기는 8자 코드가 키다.
 * 코드 자체는 이 기기에만 localStorage로 저장하고(recent-stocks와 같은 성격), 목록은 Worker의
 * KV(web/worker.js syncHandler)에 코드로 저장한다. 서버 저장은 실패해도 로컬 목록엔 영향 없다. */
const CODE_KEY = "recent-sync-code";
const ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"; // 숫자/영문 중 헷갈리는 0 O 1 I L 제외

export function loadSyncCode(): string | null {
  try {
    return localStorage.getItem(CODE_KEY);
  } catch {
    return null;
  }
}

function saveSyncCode(code: string | null): void {
  try {
    if (code) localStorage.setItem(CODE_KEY, code);
    else localStorage.removeItem(CODE_KEY);
  } catch { /* 무시 — 코드 저장 실패해도 이번 세션 동기화는 계속 쓸 수 있다 */ }
}

function generateSyncCode(): string {
  const bytes = new Uint8Array(8);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => ALPHABET[b % ALPHABET.length]).join("");
}

/** 종목·삭제 기록을 하나로 합친다. 삭제가 그 종목의 마지막 방문보다 늦으면(= 한쪽이 지운 뒤 다른 쪽이
 * 아직 그걸 모르는 채로 들고 있는 상태) 목록에서 뺀다 — 단순히 두 목록을 합치기만 하면(union) 한쪽이
 * 지운 종목을 다른 쪽이 그대로 들고 있어서 동기화할 때마다 삭제가 취소되고 되살아난다. */
function mergeState(local: { recent: RecentEntry[]; deleted: Tombstone[] },
                    remote: { recent: RecentEntry[]; deleted: Tombstone[] }): { recent: RecentEntry[]; deleted: Tombstone[] } {
  const byCode = new Map<string, Tombstone>();
  for (const t of [...local.deleted, ...remote.deleted]) {
    const prev = byCode.get(t.code);
    if (!prev || t.at > prev.at) byCode.set(t.code, t);
  }
  const deleted = [...byCode.values()];
  const deletedAt = new Map(deleted.map((t) => [t.code, t.at]));

  const byEntry = new Map<string, RecentEntry>();
  for (const r of [...local.recent, ...remote.recent]) {
    const prev = byEntry.get(r.code);
    if (!prev || r.at > prev.at) byEntry.set(r.code, r);
  }
  const recent = [...byEntry.values()]
    .filter((r) => !(deletedAt.has(r.code) && deletedAt.get(r.code)! >= r.at))
    .sort((a, b) => b.at - a.at);
  return { recent, deleted };
}

type SyncState = { recent: RecentEntry[]; deleted: Tombstone[] };
type SyncGetResponse = { status: "ok"; recent: RecentEntry[]; deleted: Tombstone[]; updated_at: string } | { status: "missing" };
type SyncPutResponse = { status: "ok"; updated_at: string };

async function fetchSynced(code: string): Promise<SyncState> {
  const res = await getJson<SyncGetResponse>(`api/sync/${code}`);
  return res.status === "ok" ? { recent: res.recent, deleted: res.deleted ?? [] } : { recent: [], deleted: [] };
}

const pushSynced = (code: string, state: SyncState) =>
  getJson<SyncPutResponse>(`api/sync/${code}`, {
    method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(state),
  });

function applyLocally(state: SyncState): RecentEntry[] {
  replaceDeleted(state.deleted);
  return replaceRecent(state.recent);
}

/** 이 기기에 동기화를 새로 켠다: 코드를 만들어 지금 가진 목록을 올리고 저장한다. */
export async function enableSync(): Promise<string> {
  const code = generateSyncCode();
  await pushSynced(code, { recent: loadRecent(), deleted: loadDeleted() });
  saveSyncCode(code);
  return code;
}

/** 다른 기기에서 받은 코드를 입력: 서버 목록과 이 기기 목록을 합쳐 양쪽에 반영하고, 이 기기도 그 코드로 계속 동기화한다. */
export async function joinSync(rawCode: string): Promise<RecentEntry[]> {
  const code = rawCode.trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (code.length !== 8) throw new Error("코드는 8자리입니다.");
  const remote = await fetchSynced(code);
  const merged = mergeState({ recent: loadRecent(), deleted: loadDeleted() }, remote);
  await pushSynced(code, merged);
  saveSyncCode(code);
  return applyLocally(merged);
}

/** 앱 진입 시 한 번: 동기화 중이면 다른 기기에서 생긴 변경(새 방문·삭제)을 끌어와 합친다.
 * 동기화 중이 아니면 아무것도 안 한다. */
export async function syncPull(): Promise<RecentEntry[] | null> {
  const code = loadSyncCode();
  if (!code) return null;
  const remote = await fetchSynced(code);
  const local = { recent: loadRecent(), deleted: loadDeleted() };
  const merged = mergeState(local, remote);
  if (JSON.stringify(merged) !== JSON.stringify(remote)) pushSynced(code, merged).catch(() => {});
  return applyLocally(merged);
}

/** 로컬 변경(조회·삭제) 직후 호출: 동기화 중인 서버 상태에 반영한다. 실패는 조용히 무시 — 로컬은
 * 항상 정상이고, 다음 변경이나 다음 syncPull 때 다시 맞춰진다. */
export function syncPush(recent: RecentEntry[]): void {
  const code = loadSyncCode();
  if (!code) return;
  pushSynced(code, { recent, deleted: loadDeleted() }).catch(() => {});
}

export function disableSync(): void {
  saveSyncCode(null);
}

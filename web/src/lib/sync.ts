import { getJson } from "./data";
import { loadDeleted, loadRecent, replaceDeleted, replaceRecent, type RecentEntry, type Tombstone } from "./recent";
import { CODE_PATTERN, generateCode, parseCodeInput } from "./syncWords";

/** 기기 간 '최근 조회' 동기화. 로그인 없이, 사용자가 기기 사이에 직접 옮기는 코드가 키다.
 *
 * 코드는 두 가지다 — 보기 코드(3단어, 예: apple-tiger-chair)는 아는 사람 누구나 그 목록을 볼 수
 * 있고, 편집 키(5단어)를 같이 아는 기기만 목록을 바꿀 수 있다. 서버(Worker)는 편집 키 자체가 아니라
 * 그 해시만 저장해 둔다. 두 코드 다 이 기기에만 localStorage로 저장하고(recent-stocks와 같은 성격),
 * 목록은 Worker의 KV(web/worker.js syncHandler)에 보기 코드로 저장한다. 서버 저장이 실패해도 로컬
 * 목록엔 영향 없다. */
const CODE_KEY = "recent-sync-code";
const EDIT_KEY_KEY = "recent-sync-edit-key";
const VIEW_WORDS = 3;
const EDIT_WORDS = 5;

export function loadSyncCode(): string | null {
  try {
    return localStorage.getItem(CODE_KEY);
  } catch {
    return null;
  }
}

/** 이 기기가 편집 키도 가지고 있는지(=읽기+쓰기 가능한지, 아니면 보기 전용인지). */
export function hasEditAccess(): boolean {
  try {
    return !!localStorage.getItem(EDIT_KEY_KEY);
  } catch {
    return false;
  }
}

/** 이미 만든 편집 키를 다시 보여줄 때(다른 기기를 추가로 등록하고 싶을 때 등) 쓴다. */
export function loadEditKey(): string | null {
  try {
    return localStorage.getItem(EDIT_KEY_KEY);
  } catch {
    return null;
  }
}

function saveSyncCredentials(code: string | null, editKey: string | null): void {
  try {
    if (code) localStorage.setItem(CODE_KEY, code); else localStorage.removeItem(CODE_KEY);
    if (editKey) localStorage.setItem(EDIT_KEY_KEY, editKey); else localStorage.removeItem(EDIT_KEY_KEY);
  } catch { /* 무시 — 저장 실패해도 이번 세션 동기화는 계속 쓸 수 있다 */ }
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

/** editKey 없이는 새로 만들 때만(최초 PUT) 통한다. 이미 있는 코드에 editKey 없이 쓰려 하면 서버가 403. */
const pushSynced = (code: string, state: SyncState, editKey: string) =>
  getJson<SyncPutResponse>(`api/sync/${code}`, {
    method: "PUT", headers: { "content-type": "application/json" },
    body: JSON.stringify({ ...state, editKey }),
  });

function applyLocally(state: SyncState): RecentEntry[] {
  replaceDeleted(state.deleted);
  return replaceRecent(state.recent);
}

/** 이 기기에 동기화를 새로 켠다: 보기 코드·편집 키를 만들어 지금 가진 목록을 올리고 둘 다 저장한다. */
export async function enableSync(): Promise<{ code: string; editKey: string }> {
  const code = generateCode(VIEW_WORDS);
  const editKey = generateCode(EDIT_WORDS);
  await pushSynced(code, { recent: loadRecent(), deleted: loadDeleted() }, editKey);
  saveSyncCredentials(code, editKey);
  return { code, editKey };
}

/** 코드를 입력해 합류한다. 편집 키까지 맞으면 이 기기도 쓸 수 있게 되고(합쳐서 반영), 편집 키가
 * 없거나 틀리면 보기 전용으로 그 목록만 보여준다(이 기기의 조회·삭제는 서버에 올라가지 않는다). */
export async function joinSync(rawCode: string, rawEditKey?: string): Promise<{ recent: RecentEntry[]; readOnly: boolean }> {
  const code = parseCodeInput(rawCode);
  if (!CODE_PATTERN.test(code)) throw new Error("코드 형식이 올바르지 않습니다.");
  const remote = await fetchSynced(code);
  const editKey = rawEditKey && rawEditKey.trim() ? parseCodeInput(rawEditKey) : "";
  if (!editKey) {
    saveSyncCredentials(code, null);
    return { recent: applyLocally(mergeState({ recent: loadRecent(), deleted: loadDeleted() }, remote)), readOnly: true };
  }
  const merged = mergeState({ recent: loadRecent(), deleted: loadDeleted() }, remote);
  await pushSynced(code, merged, editKey);   // 편집 키가 틀리면 Worker가 거부 메시지를 그대로 던진다
  saveSyncCredentials(code, editKey);
  return { recent: applyLocally(merged), readOnly: false };
}

/** 앱 진입 시 한 번: 동기화 중이면 다른 기기에서 생긴 변경(새 방문·삭제)을 끌어와 합친다.
 * 편집 키가 없는(보기 전용) 기기는 서버에 다시 올리지 않고 받기만 한다. */
export async function syncPull(): Promise<RecentEntry[] | null> {
  const code = loadSyncCode();
  if (!code) return null;
  const remote = await fetchSynced(code);
  const local = { recent: loadRecent(), deleted: loadDeleted() };
  const merged = mergeState(local, remote);
  const editKey = loadEditKey();
  if (editKey && JSON.stringify(merged) !== JSON.stringify(remote)) pushSynced(code, merged, editKey).catch(() => {});
  return applyLocally(merged);
}

/** 로컬 변경(조회·삭제) 직후 호출: 편집 키가 있는(쓰기 가능한) 기기만 서버 상태에 반영한다.
 * 실패는 조용히 무시 — 로컬은 항상 정상이고, 다음 변경이나 다음 syncPull 때 다시 맞춰진다. */
export function syncPush(recent: RecentEntry[]): void {
  const code = loadSyncCode();
  const editKey = loadEditKey();
  if (!code || !editKey) return;
  pushSynced(code, { recent, deleted: loadDeleted() }, editKey).catch(() => {});
}

export function disableSync(): void {
  saveSyncCredentials(null, null);
}

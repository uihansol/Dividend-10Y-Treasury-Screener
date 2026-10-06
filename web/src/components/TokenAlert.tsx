import { useEffect, useRef, useState } from "react";
import { getTokenStatus, updateToken, type TokenStatus } from "../lib/data";

const EXPIRY_WARN_DAYS = 14;
const RECHECK_MS = 5 * 60_000;
const SNOOZE_KEY = "token-alert-snoozed-day";
const NEW_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new";

const today = () => new Date().toISOString().slice(0, 10);
const readSnooze = () => { try { return localStorage.getItem(SNOOZE_KEY); } catch { return null; } };
const writeSnooze = () => { try { localStorage.setItem(SNOOZE_KEY, today()); } catch { /* 저장 실패는 무시 */ } };

/** GitHub 토큰이 만료·누락되면(또는 14일 안에 만료되면) 알림 창을 직접 띄우고 그 자리에서 새 토큰을 입력받는다.
 * 토큰은 저장하지 않고 한 번 Worker로 보낸 뒤 입력칸을 비운다. 만료 임박(아직 동작 중)은 하루에 한 번만 창을 띄운다. */
export function TokenAlert() {
  const [st, setSt] = useState<TokenStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // 처음 한 번 + 5분마다 + 탭으로 돌아올 때 다시 확인해, 열어 둔 채 만료돼도 놓치지 않는다.
  useEffect(() => {
    const check = () => getTokenStatus().then(setSt).catch(() => { /* 확인 실패는 그대로 둔다 */ });
    check();
    const id = window.setInterval(check, RECHECK_MS);
    const onVisible = () => { if (document.visibilityState === "visible") check(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(id); document.removeEventListener("visibilitychange", onVisible); };
  }, []);

  const kind: "dead" | "soon" | null = !st ? null
    : st.state === "expired" || st.state === "missing" ? "dead"
    : st.state === "ok" && st.days_left !== null && st.days_left <= EXPIRY_WARN_DAYS ? "soon" : null;

  // 상태가 정상 → 만료로 바뀌는 순간에만 창을 연다(닫은 뒤 주기 확인이 다시 열지 않는다).
  useEffect(() => {
    if (kind === "dead" || (kind === "soon" && readSnooze() !== today())) setOpen(true);
  }, [kind]);

  useEffect(() => { if (open) inputRef.current?.focus(); }, [open]);

  // 저장 실패로 버튼이 잠시 비활성화되면 포커스가 창 밖으로 빠지므로, Esc는 창이 아니라 문서에서 받는다.
  const closeRef = useRef<() => void>(() => undefined);
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") closeRef.current(); };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  if (!st || !kind) return null;
  const dead = kind === "dead";
  const title = st.state === "missing" ? "GitHub 접근 토큰이 설정되어 있지 않습니다"
    : dead ? "GitHub 접근 토큰이 만료되었습니다"
    : `GitHub 접근 토큰이 ${st.days_left}일 뒤 만료됩니다`;
  const detail = dead ? "지금은 종목 목록과 데이터를 불러오지 못합니다. 새 토큰을 입력하면 바로 복구됩니다."
    : "만료되면 종목 목록과 데이터를 불러오지 못합니다. 미리 새 토큰으로 바꿔 두세요.";

  const close = () => { setOpen(false); setValue(""); setErr(null); if (!dead) writeSnooze(); };
  closeRef.current = close;
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    updateToken(value.trim())
      .then(() => { setValue(""); window.location.reload(); })
      .catch((e2) => setErr(String((e2 as Error).message).replace(/^api\/token:\s*/, "")))
      .finally(() => { setBusy(false); setValue(""); window.setTimeout(() => inputRef.current?.focus(), 0); });
  };

  return (
    <>
      {!open && (
        <div className="page">
          <section className={"krx-alert" + (dead ? "" : " soft")} role="alert">
            <strong>{title}.</strong> <span>{detail}</span>
            <div><button type="button" className="btn" onClick={() => setOpen(true)}>토큰 입력</button></div>
          </section>
        </div>
      )}
      {open && (
        <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) close(); }}>
          <div className="modal" role="alertdialog" aria-modal="true" aria-labelledby="token-modal-title">
            <h2 id="token-modal-title" className={dead ? "modal-title warn" : "modal-title"}>{title}</h2>
            <p>{detail}</p>
            {st.can_update ? (
              <form className="token-form" onSubmit={submit}>
                <p className="token-note">
                  <a href={NEW_TOKEN_URL} target="_blank" rel="noreferrer">GitHub에서 새 토큰 만들기</a> — 이 저장소만 선택, 권한은
                  Contents: Read and write, Actions: Read. 이 저장소에 쓰기 권한이 있는 계정의 토큰만 받습니다.
                </p>
                <input ref={inputRef} type="password" autoComplete="off" spellCheck={false} placeholder="새 토큰을 붙여 넣으세요"
                  value={value} onChange={(e) => setValue(e.target.value)} aria-label="새 GitHub 토큰" />
                <div className="modal-actions">
                  <button type="submit" className="btn" disabled={busy || value.trim().length < 20}>{busy ? "확인 중…" : "저장하고 복구"}</button>
                  <button type="button" className="btn" onClick={close}>{dead ? "나중에" : "오늘은 그만 보기"}</button>
                </div>
                {err && <p className="sync-error" role="alert">{err}</p>}
              </form>
            ) : (
              <>
                <p className="token-note">웹에서 갱신하려면 동기화 저장소(KV)가 필요합니다. GitHub 저장소 시크릿 <code>WORKER_GITHUB_TOKEN</code>을
                  새 토큰으로 바꾸고 Deploy를 실행하세요.</p>
                <div className="modal-actions"><button type="button" className="btn" onClick={close}>닫기</button></div>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}

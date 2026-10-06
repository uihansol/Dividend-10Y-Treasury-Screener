import { useEffect, useState } from "react";
import { getTokenStatus, updateToken, type TokenStatus } from "../lib/data";

const EXPIRY_WARN_DAYS = 14;
const NEW_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new";

/** GitHub 토큰 만료·누락 알림과 즉시 갱신 폼. 토큰은 저장하지 않고 한 번 Worker로 보낸 뒤 입력칸을 비운다. */
export function TokenAlert() {
  const [st, setSt] = useState<TokenStatus | null>(null);
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { getTokenStatus().then(setSt).catch(() => setSt(null)); }, []);

  if (!st) return null;
  const dead = st.state === "expired" || st.state === "missing";
  const soon = st.state === "ok" && st.days_left !== null && st.days_left <= EXPIRY_WARN_DAYS;
  if (!dead && !soon) return null;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    updateToken(value.trim())
      .then(() => { setValue(""); window.location.reload(); })
      .catch((e2) => setErr(String((e2 as Error).message).replace(/^api\/token:\s*/, "")))
      .finally(() => { setBusy(false); setValue(""); });
  };

  return (
    <div className="page">
      <section className={"krx-alert" + (dead ? "" : " soft")} role="alert">
        <strong>{st.state === "missing" ? "GitHub 접근 토큰이 설정되어 있지 않습니다."
          : dead ? "GitHub 접근 토큰이 만료되었습니다."
          : `GitHub 접근 토큰이 ${st.days_left}일 뒤 만료됩니다.`}</strong>
        <span> {dead ? "종목 목록과 데이터를 불러오지 못합니다." : "만료되면 종목 목록과 데이터를 불러오지 못합니다."}</span>
        {!open && st.can_update && (
          <div><button type="button" className="btn" onClick={() => setOpen(true)}>지금 토큰 갱신</button></div>
        )}
        {!st.can_update && <p className="token-note">웹에서 갱신하려면 동기화 저장소(KV)가 필요합니다. GitHub 저장소 시크릿 <code>WORKER_GITHUB_TOKEN</code>을 바꾸고 Deploy를 실행하세요.</p>}
        {open && (
          <form className="token-form" onSubmit={submit}>
            <p className="token-note">
              <a href={NEW_TOKEN_URL} target="_blank" rel="noreferrer">GitHub에서 새 토큰</a>을 만들어(이 저장소만 선택, Contents: Read,
              Actions: Read and write) 붙여 넣으세요. 이 저장소에 쓰기 권한이 있는 계정의 토큰만 받습니다.
            </p>
            <input type="password" autoComplete="off" spellCheck={false} placeholder="github_pat_…"
              value={value} onChange={(e) => setValue(e.target.value)} aria-label="새 GitHub 토큰" />
            <button type="submit" className="btn" disabled={busy || value.trim().length < 20}>{busy ? "확인 중…" : "저장"}</button>
            <button type="button" className="btn" onClick={() => { setOpen(false); setValue(""); setErr(null); }}>취소</button>
            {err && <p className="sync-error" role="alert">{err}</p>}
          </form>
        )}
      </section>
    </div>
  );
}

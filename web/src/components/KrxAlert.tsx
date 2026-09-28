import { useEffect, useState } from "react";
import { getKrxStatus, type KrxStatus } from "../lib/data";

const KRX_LOGIN = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd";

/** KRX 비밀번호 만료·로그인 실패 알림. 앱은 비밀번호를 직접 저장하지 않고, 교체에 필요한 페이지를 순서대로 열어 준다. */
export function KrxAlert() {
  const [st, setSt] = useState<KrxStatus | null>(null);
  const [checking, setChecking] = useState(false);
  useEffect(() => { getKrxStatus().then(setSt).catch(() => setSt(null)); }, []);

  if (!st || (st.state !== "password_expired" && st.state !== "login_failed")) return null;
  const repo = st.repo ? `https://github.com/${st.repo}` : null;
  const recheck = () => {
    setChecking(true);
    getKrxStatus(true).then(setSt).catch(() => undefined).finally(() => setChecking(false));
  };

  return (
    <div className="page">
      <section className="krx-alert" role="alert">
        <strong>{st.state === "password_expired"
          ? "KRX 비밀번호가 만료되었습니다(90일)."
          : "KRX 로그인에 실패했습니다. 저장된 비밀번호(KRX_PW)가 KRX 계정과 다릅니다."}</strong>
        <span> 새 가격을 받지 못해 저장된 값만 표시됩니다.</span>
        <ol>
          <li><a href={KRX_LOGIN} target="_blank" rel="noreferrer">KRX 정보데이터시스템</a>에 로그인해 비밀번호를 변경합니다.</li>
          <li>{repo
            ? <a href={`${repo}/settings/secrets/actions/KRX_PW`} target="_blank" rel="noreferrer">GitHub 시크릿 KRX_PW</a>
            : "GitHub 시크릿 KRX_PW"}에 새 비밀번호를 입력해 저장합니다.</li>
          <li>{repo
            ? <a href={`${repo}/actions/workflows/deploy.yml`} target="_blank" rel="noreferrer">Deploy</a>
            : "Deploy"}를 한 번 실행합니다(Run workflow). 약 1분 뒤 웹에도 반영됩니다.</li>
        </ol>
        <button type="button" className="btn" onClick={recheck} disabled={checking}>{checking ? "확인 중…" : "다시 확인"}</button>
      </section>
    </div>
  );
}

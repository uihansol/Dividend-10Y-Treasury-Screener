import { useEffect, useState } from "react";
import { getKrxStatus, type KrxStatus } from "../lib/data";

const KRX_LOGIN = "https://data.krx.co.kr/contents/MDC/COMS/client/MDCCOMS001.cmd";

/** KRX 비밀번호 만료·로그인 실패 알림. 앱은 비밀번호를 직접 저장하지 않고, 교체에 필요한 페이지를 순서대로 열어 준다. */
export function KrxAlert() {
  const [st, setSt] = useState<KrxStatus | null>(null);
  const [checking, setChecking] = useState(false);
  useEffect(() => { getKrxStatus().then(setSt).catch(() => setSt(null)); }, []);

  const noResponse = st?.state === "unreachable" && st.reason === "login_no_response";
  if (!st || (st.state !== "password_expired" && st.state !== "login_failed" && !noResponse)) return null;
  const repo = st.repo ? `https://github.com/${st.repo}` : null;
  const recheck = () => {
    setChecking(true);
    getKrxStatus(true).then(setSt).catch(() => undefined).finally(() => setChecking(false));
  };

  // KRX가 로그인 결과 코드를 주지 않은 경우: 비밀번호가 틀렸다는 뜻이 아니다(차단·점검·응답 형식 변경 등).
  if (noResponse) {
    return (
      <div className="page">
        <section className="krx-alert soft" role="alert">
          <strong>KRX 로그인 응답을 받지 못했습니다.</strong>
          <span> 비밀번호 문제가 아닐 수 있습니다(KRX 점검, 이 서버의 접속 차단, 응답 형식 변경 등). 장중 잠정 시세만 영향을 받고,
            확정 가격 수집(GitHub Actions)은 별도로 동작합니다.</span>
          <div><button type="button" className="btn" onClick={recheck} disabled={checking}>{checking ? "확인 중…" : "다시 확인"}</button></div>
        </section>
      </div>
    );
  }

  return (
    <div className="page">
      <section className="krx-alert" role="alert">
        <strong>{st.state === "password_expired"
          ? "KRX 비밀번호가 만료되었습니다(90일)."
          : `KRX가 로그인을 거부했습니다${st.code ? ` (결과 코드 ${st.code})` : ""}. 저장된 KRX 아이디·비밀번호가 KRX 계정과 다릅니다.`}</strong>
        <span> 새 가격을 받지 못해 저장된 값만 표시됩니다.</span>
        <ol>
          <li><a href={KRX_LOGIN} target="_blank" rel="noreferrer">KRX 정보데이터시스템</a>에 로그인해 비밀번호를 변경합니다.</li>
          <li>{repo
            ? <a href={`${repo}/settings/secrets/actions`} target="_blank" rel="noreferrer">GitHub 저장소 시크릿</a>
            : "GitHub 저장소 시크릿"}에서 KRX 비밀번호 항목을 새 값으로 수정합니다.</li>
          <li>{repo
            ? <a href={`${repo}/actions/workflows/deploy.yml`} target="_blank" rel="noreferrer">Deploy</a>
            : "Deploy"}를 한 번 실행합니다(Run workflow). 약 1분 뒤 웹에도 반영됩니다.</li>
        </ol>
        {st.state === "login_failed" && (
          <p className="token-note">그래도 계속되면 확인하세요: ① 시크릿 이름이 저장소 README '설정' 표의 KRX 항목 이름과 글자 하나까지 같은지(Actions의
            Repository secrets이며 Environment/Dependabot 아님) ② KRX 사이트에 같은 아이디·비밀번호로 직접 로그인이 되는지 ③ 로그인 실패가 반복돼 계정이 잠기지 않았는지.
            값 앞뒤 공백·줄바꿈은 자동으로 제거합니다.</p>
        )}
        <button type="button" className="btn" onClick={recheck} disabled={checking}>{checking ? "확인 중…" : "다시 확인"}</button>
      </section>
    </div>
  );
}

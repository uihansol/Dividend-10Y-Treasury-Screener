/**
 * KRX 로그인 상태 확인 (Worker) — 비밀번호 만료 알림용.
 *
 *  GET /api/krx/status  → {state, repo, code?, reason?}
 *    state: ok | password_expired(KRX CD010, 90일 만료) | login_failed(KRX가 거부 — code에 KRX 결과 코드) | not_configured | unreachable
 *    unreachable의 reason=login_no_response: KRX가 로그인 결과 코드를 주지 않음(차단·점검 등) — 비밀번호 문제가 아닐 수 있다.
 *    이때 http(응답 상태), json(JSON 여부), content_type을 함께 돌려줘 원인을 구분한다(403=차단, 5xx=점검·장애, HTML=안내 페이지)
 *
 * 비밀번호 교체 자체는 앱이 하지 않는다. 화면이 KRX 비밀번호 변경 → GitHub 시크릿 KRX_PW 수정 → Deploy 재실행
 * 순서로 해당 페이지를 바로 열어 준다. (앱이 GitHub 시크릿을 쓰려면 별도 권한이 필요해 넣지 않았다.)
 */
import { withSession, KrxError } from "./krx.js";

const STATE_BY_CODE = {
  password_change: "password_expired",
  login_failed: "login_failed",
  no_credentials: "not_configured",
};

/** 캐시된 세션이 살아 있으면 로그인하지 않고 ok. 없으면 한 번 로그인해 본다(실패한 비밀번호의 반복 로그인은 krx.js가 막는다). */
export async function krxStatus(env, fetchImpl = fetch) {
  if (!env.KRX_ID || !env.KRX_PW) return { state: "not_configured" };
  try {
    await withSession(env, async () => true, { fetchImpl });
    return { state: "ok" };
  } catch (e) {
    const state = (e instanceof KrxError && STATE_BY_CODE[e.code]) || "unreachable";
    // 원인 구분용 비밀 아닌 정보만: KRX 결과 코드, 내부 분류. 자격 증명은 어떤 메시지에도 들어가지 않는다.
    return { state, ...(e instanceof KrxError ? { reason: e.code, ...(e.krxCode ? { code: e.krxCode } : {}),
      ...(e.http ? { http: e.http, json: e.json, content_type: e.contentType, trail: e.trail, snippet: e.snippet } : {}) } : {}) };
  }
}

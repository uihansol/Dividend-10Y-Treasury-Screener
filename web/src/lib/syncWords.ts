/** 동기화 코드용 단어 목록. 과일·동물·물건처럼 주위에서 쉽게 보는 것들로, 무작위 문자열 대신
 * 기억하기 쉬운 코드를 만든다. 실제로 서버(Worker)·URL에 쓰는 값은 `en`(영문 소문자, 공백 없음)
 * 이고, 화면에는 `ko`로 보여준다. 둘 다 이 목록 안에서 중복이 없어야 한다. */
export type SyncWord = { ko: string; en: string };

export const SYNC_WORDS: SyncWord[] = [
  // 과일
  { ko: "사과", en: "apple" }, { ko: "바나나", en: "banana" }, { ko: "포도", en: "grape" },
  { ko: "딸기", en: "strawberry" }, { ko: "수박", en: "watermelon" }, { ko: "참외", en: "melon" },
  { ko: "복숭아", en: "peach" }, { ko: "배", en: "pear" }, { ko: "오렌지", en: "orange" },
  { ko: "레몬", en: "lemon" }, { ko: "키위", en: "kiwi" }, { ko: "망고", en: "mango" },
  { ko: "파인애플", en: "pineapple" }, { ko: "체리", en: "cherry" }, { ko: "자두", en: "plum" },
  { ko: "감", en: "persimmon" }, { ko: "귤", en: "tangerine" }, { ko: "블루베리", en: "blueberry" },
  { ko: "석류", en: "pomegranate" }, { ko: "무화과", en: "fig" }, { ko: "밤", en: "chestnut" },
  { ko: "호두", en: "walnut" }, { ko: "토마토", en: "tomato" }, { ko: "메론", en: "cantaloupe" },
  // 동물
  { ko: "호랑이", en: "tiger" }, { ko: "사자", en: "lion" }, { ko: "코끼리", en: "elephant" },
  { ko: "기린", en: "giraffe" }, { ko: "토끼", en: "rabbit" }, { ko: "다람쥐", en: "squirrel" },
  { ko: "고양이", en: "cat" }, { ko: "강아지", en: "puppy" }, { ko: "곰", en: "bear" },
  { ko: "여우", en: "fox" }, { ko: "늑대", en: "wolf" }, { ko: "사슴", en: "deer" },
  { ko: "판다", en: "panda" }, { ko: "캥거루", en: "kangaroo" }, { ko: "코알라", en: "koala" },
  { ko: "펭귄", en: "penguin" }, { ko: "돌고래", en: "dolphin" }, { ko: "고래", en: "whale" },
  { ko: "거북이", en: "turtle" }, { ko: "올빼미", en: "owl" }, { ko: "독수리", en: "eagle" },
  { ko: "참새", en: "sparrow" }, { ko: "오리", en: "duck" }, { ko: "닭", en: "chicken" },
  { ko: "돼지", en: "pig" }, { ko: "소", en: "cow" }, { ko: "말", en: "horse" },
  { ko: "양", en: "sheep" }, { ko: "염소", en: "goat" }, { ko: "개구리", en: "frog" },
  { ko: "물고기", en: "fish" }, { ko: "문어", en: "octopus" }, { ko: "거미", en: "spider" },
  { ko: "나비", en: "butterfly" }, { ko: "벌", en: "bee" }, { ko: "달팽이", en: "snail" },
  // 물건
  { ko: "의자", en: "chair" }, { ko: "책상", en: "desk" }, { ko: "우산", en: "umbrella" },
  { ko: "시계", en: "clock" }, { ko: "모자", en: "hat" }, { ko: "신발", en: "shoes" },
  { ko: "가방", en: "bag" }, { ko: "안경", en: "glasses" }, { ko: "열쇠", en: "key" },
  { ko: "지갑", en: "wallet" }, { ko: "우체통", en: "mailbox" }, { ko: "자전거", en: "bicycle" },
  { ko: "자동차", en: "car" }, { ko: "비행기", en: "airplane" }, { ko: "기차", en: "train" },
  { ko: "전화기", en: "phone" }, { ko: "컴퓨터", en: "computer" }, { ko: "연필", en: "pencil" },
  { ko: "지우개", en: "eraser" }, { ko: "책", en: "book" }, { ko: "공책", en: "notebook" },
  { ko: "접시", en: "plate" }, { ko: "컵", en: "cup" }, { ko: "숟가락", en: "spoon" },
  { ko: "포크", en: "fork" }, { ko: "냄비", en: "pot" }, { ko: "담요", en: "blanket" },
  { ko: "베개", en: "pillow" }, { ko: "거울", en: "mirror" }, { ko: "빗", en: "comb" },
  { ko: "칫솔", en: "toothbrush" }, { ko: "비누", en: "soap" }, { ko: "우유", en: "milk" },
  { ko: "풍선", en: "balloon" }, { ko: "연", en: "kite" }, { ko: "공", en: "ball" },
];

const BY_EN = new Map(SYNC_WORDS.map((w) => [w.en, w.ko]));
const BY_KO = new Map(SYNC_WORDS.map((w) => [w.ko, w.en]));

function randomWords(n: number): SyncWord[] {
  const bytes = new Uint32Array(n);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => SYNC_WORDS[b % SYNC_WORDS.length]);
}

/** 영문 소문자-하이픈 코드 생성(실제 전송·저장용). */
export function generateCode(wordCount: number): string {
  return randomWords(wordCount).map((w) => w.en).join("-");
}

/** 영문 코드를 화면에 보여줄 한글 단어로 바꾼다. 알 수 없는 조각은 그대로 둔다(안전한 표시용). */
export function codeToKorean(code: string): string {
  return code.split("-").map((en) => BY_EN.get(en) ?? en).join(" · ");
}

/** 사용자가 직접 입력한 코드를 정규화(소문자, 허용 문자만, 구두점을 하이픈으로). */
export function normalizeCode(raw: string): string {
  return raw.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
}

/** 다른 기기 화면에 뜬 한글 단어("사과 · 호랑이 · 의자" 또는 "사과 호랑이 의자")를 그대로 입력해도
 * 받아들인다 — 실제 코드(영문)를 입력해도 된다. 한글 단어는 목록에서 찾아 영문으로 바꾸고, 모르는
 * 조각은 이미 영문 코드라고 보고 그대로 둔다(최종 형식 검증은 CODE_PATTERN이 한다). */
export function parseCodeInput(raw: string): string {
  const tokens = raw.trim().split(/[\s,·ㆍ-]+/).filter(Boolean);
  return tokens.map((t) => BY_KO.get(t) ?? normalizeCode(t)).join("-");
}

export const CODE_PATTERN = /^[a-z]{2,20}(-[a-z]{2,20}){1,7}$/;

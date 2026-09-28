/// <reference types="vite/client" />

// 배포 워크플로(deploy.yml)가 빌드 시 주입한다. 로컬 개발에서는 비어 있어 화면이 "dev · local"로 표시된다.
interface ImportMetaEnv {
  readonly VITE_APP_BUILD?: string;
  readonly VITE_APP_SHA?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

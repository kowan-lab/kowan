/// <reference types="vite/client" />

/** The Kowan version this SPA was built as (web/frontend/package.json). */
declare const __APP_VERSION__: string;

/** docs/CHANGELOG.md, provided by the kowan-changelog plugin (vite.config.ts). */
declare module "virtual:changelog" {
  const text: string;
  export default text;
}

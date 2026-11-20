/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** The Convex deployment URL. Public by design: it is baked into the bundle. */
  readonly VITE_CONVEX_URL?: string;
  /** The Convex HTTP-actions origin, used by anything that is not a Convex client. */
  readonly VITE_CONVEX_SITE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

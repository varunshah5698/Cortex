/**
 * The Convex client: one connection, one session token, one place that knows the URL.
 *
 * The deployment URL is public (it is baked into the bundle), so it comes from a `VITE_`
 * variable with a working default. The deploy key is a server secret and is deliberately
 * absent here — a browser bundle is readable by anyone who loads the page.
 *
 * The session token is the only credential the browser holds. It lives in localStorage
 * because a page reload must not sign the person out, and it is sent to Convex as an
 * argument rather than a cookie because Convex functions are not same-origin requests.
 */
import { ConvexReactClient } from "convex/react";

const url = (import.meta.env.VITE_CONVEX_URL as string | undefined) ?? "https://dynamic-jay-407.convex.cloud";

export const convex = new ConvexReactClient(url);

const TOKEN_KEY = "cortex.convex.token.v1";

export function loadToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function saveToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage may be unavailable; the session then lasts one page */
  }
}

export function convexUrl(): string {
  return url;
}

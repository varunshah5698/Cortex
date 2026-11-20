import { useEffect, useState } from "react";
import { API } from "./providers";

export type User = {
  id: string;
  name: string;
  email: string;
  created?: string;
};

async function call(path: string, init: RequestInit = {}): Promise<User> {
  const response = await fetch(`${API}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const data = (await response.json().catch(() => ({}))) as { user?: User; detail?: string };
  if (!response.ok || !data.user) {
    throw new Error(data.detail || "That request did not go through. Try again.");
  }
  window.dispatchEvent(new Event("cortex:auth"));
  return data.user;
}

/**
 * True when this page and the harness are both on the operator's own machine.
 *
 * The API middleware already lets a loopback browser through without a session
 * (`local_browser`), so a 401 from `/api/auth/me` here does not mean "signed out" —
 * it means "no account needed". Reading it as signed-out bounced the Build Room to
 * the login form while every API call behind it kept working.
 */
function onLoopback(): boolean {
  const local = (host: string) => ["127.0.0.1", "localhost", "[::1]", "::1"].includes(host);
  try {
    return local(window.location.hostname) && (!API || local(new URL(API).hostname));
  } catch {
    return false;
  }
}

const LOCAL_OPERATOR: User = {
  id: "local-operator",
  name: "Local operator",
  email: "local@127.0.0.1",
};

export async function me(): Promise<User | null> {
  const response = await fetch(`${API}/api/auth/me`, { credentials: "include" });
  if (response.status === 401) return onLoopback() ? LOCAL_OPERATOR : null;
  if (!response.ok) throw new Error("The harness is not answering.");
  const data = (await response.json()) as { user?: User };
  return data.user ?? null;
}

export function signup(name: string, email: string, password: string): Promise<User> {
  return call("/api/auth/signup", {
    method: "POST",
    body: JSON.stringify({ name, email, password }),
  });
}

export function login(email: string, password: string): Promise<User> {
  return call("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${API}/api/auth/logout`, { method: "POST", credentials: "include" });
  } finally {
    window.dispatchEvent(new Event("cortex:auth"));
  }
}

/**
 * Exchange a Convex session token for the harness's own session cookie.
 *
 * There are two auth stores and they are not the same one: the browser holds a Convex
 * token, but `/api` only trusts the `cortex_session` cookie the Python side issues. A
 * Google sign-in completes at Convex, so without this the person is signed in to the data
 * layer and 401 on every build, roster and run call — signed in and unable to do anything.
 *
 * The bridge is server-verified, not a trust transfer: the harness asks Convex who the
 * token belongs to and mints its own session for that email. A forged token gets a 401
 * from Convex and no cookie.
 */
export async function adoptCloudSession(token: string): Promise<User> {
  const response = await fetch(`${API}/api/auth/cloud`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  const data = (await response.json().catch(() => ({}))) as { user?: User; detail?: string };
  if (!response.ok || !data.user) {
    throw new Error(data.detail || "Could not finish signing you in.");
  }
  window.dispatchEvent(new Event("cortex:auth"));
  return data.user;
}

/**
 * Read a Google sign-in token out of the URL and immediately remove it.
 *
 * The callback puts the token in the fragment because a fragment is never sent to a
 * server, so it stays out of access logs and Referer headers. That only holds if the page
 * clears it: leaving it in the address bar puts it in history and in anything the person
 * copies. Returns null when there is nothing to adopt.
 */
export function takeCallbackToken(): string | null {
  const hash = window.location.hash;
  const at = hash.indexOf("?");
  if (at === -1) return null;
  const params = new URLSearchParams(hash.slice(at + 1));
  const token = params.get("token");
  if (!token) return null;
  params.delete("token");
  params.delete("email");
  const rest = params.toString();
  const clean = hash.slice(0, at) + (rest ? `?${rest}` : "");
  window.history.replaceState(null, "", `${window.location.pathname}${clean}`);
  return token;
}

/**
 * The signed-in user for the whole app. Refreshes on sign-in/out and whenever an API
 * call answers 401 (installed by `lib/api.ts`), so a stale tab lands on the form.
 */
export function useSession() {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const refresh = () => {
      me()
        .then((next) => {
          if (alive) setUser(next);
        })
        .catch(() => {
          if (alive) setUser(null);
        })
        .finally(() => {
          if (alive) setLoading(false);
        });
    };
    refresh();
    window.addEventListener("cortex:auth", refresh);
    window.addEventListener("cortex:unauthorized", refresh);
    return () => {
      alive = false;
      window.removeEventListener("cortex:auth", refresh);
      window.removeEventListener("cortex:unauthorized", refresh);
    };
  }, []);

  return { user, loading, setUser };
}

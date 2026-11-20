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

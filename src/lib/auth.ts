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

export async function me(): Promise<User | null> {
  const response = await fetch(`${API}/api/auth/me`, { credentials: "include" });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("Cortex is not answering.");
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
    let retry: number | undefined;
    let seq = 0;

    // A stop or a harness restart can answer 401 for a moment before the same cookie
    // works again. Confirm with a second call before throwing the room back to login;
    // a network failure keeps the current user instead of clearing it.
    const refresh = async (confirm: boolean) => {
      const at = ++seq;
      if (retry) {
        window.clearTimeout(retry);
        retry = undefined;
      }
      const first = await me().catch(() => undefined);
      if (!alive || at !== seq) return;
      if (first !== null) {
        if (first !== undefined) setUser(first);
        setLoading(false);
        return;
      }
      // A login just succeeded: a 401 here is more likely cookie propagation than an
      // expired session, so keep the account the form just returned.
      if (!confirm) {
        setLoading(false);
        return;
      }
      retry = window.setTimeout(async () => {
        const second = await me().catch(() => undefined);
        if (!alive || at !== seq) return;
        if (second !== undefined) setUser(second);
        setLoading(false);
      }, 600);
    };

    const onAuth = () => void refresh(false);
    const onUnauthorized = () => void refresh(true);
    void refresh(true);
    window.addEventListener("cortex:auth", onAuth);
    window.addEventListener("cortex:unauthorized", onUnauthorized);
    return () => {
      alive = false;
      if (retry) window.clearTimeout(retry);
      window.removeEventListener("cortex:auth", onAuth);
      window.removeEventListener("cortex:unauthorized", onUnauthorized);
    };
  }, []);

  return { user, loading, setUser };
}

/**
 * The app's Convex-backed data layer.
 *
 * One module so the rest of the UI does not know whether state came from localStorage, a
 * SQLite file or Convex. Every call takes the session token from `convex.ts`; a missing
 * token is a signed-out user, not an error.
 *
 * Writes are best-effort by design: the harness is the source of truth for a run, and a
 * Convex hiccup must not stop a build from finishing. Reads are not — a failed read
 * returns empty and the caller renders the empty state rather than a spinner forever.
 */
import { ConvexHttpClient } from "convex/browser";
import { api } from "../../convex/_generated/api";
import type { Id } from "../../convex/_generated/dataModel";
import { convexUrl, loadToken } from "./convex";

const client = new ConvexHttpClient(convexUrl());

export type CloudThread = { _id: Id<"threads">; title: string; updatedAt: number; createdAt: number };
export type CloudRun = {
  _id: Id<"runs">;
  runId: string;
  prompt: string;
  depth: string;
  status: "running" | "done" | "blocked" | "failed" | "cancelled";
  errors?: number;
  warnings?: number;
  costUsd?: number;
  tokens?: number;
  message?: string;
  createdAt: number;
  finishedAt?: number;
};

export type CloudAccount = { email: string; name: string | null; createdAt: number };

function token(): string | null {
  return loadToken();
}

async function guarded<T>(work: () => Promise<T>, fallback: T): Promise<T> {
  try {
    return await work();
  } catch {
    return fallback;
  }
}

// ---------- accounts ----------

export async function signUp(email: string, password: string, name?: string) {
  return await client.mutation(api.auth.signUp, { email, password, name });
}

export async function signIn(email: string, password: string) {
  return await client.mutation(api.auth.signIn, { email, password });
}

export async function signOut(): Promise<void> {
  const current = token();
  if (!current) return;
  await guarded(() => client.mutation(api.auth.signOut, { token: current }), { ok: true });
}

export async function whoAmI(): Promise<CloudAccount | null> {
  const current = token();
  if (!current) return null;
  return await guarded(() => client.query(api.auth.me, { token: current }), null);
}

/**
 * Start Google sign-in.
 *
 * The redirect URI is registered against the Convex site, so the whole exchange happens
 * there and the browser only carries a state nonce out and a session token back. This
 * returns the URL to navigate to; it does not navigate, so a caller can show an error
 * instead of leaving the page when the deployment is not configured.
 */
export async function startGoogleSignIn(redirectTo = "/"): Promise<string> {
  const { url } = await client.mutation(api.auth.startGoogle, { redirectTo });
  return url;
}

// ---------- threads ----------

export async function listThreads(): Promise<CloudThread[]> {
  const current = token();
  if (!current) return [];
  return await guarded(() => client.query(api.threads.list, { token: current }), []);
}

export async function createThread(title: string): Promise<Id<"threads"> | null> {
  const current = token();
  if (!current) return null;
  return await guarded(() => client.mutation(api.threads.create, { token: current, title }), null);
}

export async function appendMessage(
  threadId: Id<"threads">,
  role: "user" | "assistant" | "system",
  text: string,
  extra: { kind?: string; agent?: string } = {},
): Promise<void> {
  const current = token();
  if (!current) return;
  await guarded(
    () => client.mutation(api.threads.append, { token: current, threadId, role, text, ...extra }),
    null,
  );
}

export async function threadMessages(threadId: Id<"threads">) {
  const current = token();
  if (!current) return [];
  return await guarded(() => client.query(api.threads.messages, { token: current, threadId }), []);
}

export async function deleteThread(threadId: Id<"threads">): Promise<void> {
  const current = token();
  if (!current) return;
  await guarded(() => client.mutation(api.threads.remove, { token: current, threadId }), {
    ok: true,
    deleted: 0,
  });
}

// ---------- runs ----------

export async function recordRunStart(runId: string, prompt: string, depth: string): Promise<void> {
  const current = token();
  if (!current) return;
  await guarded(() => client.mutation(api.runs.start, { token: current, runId, prompt, depth }), null);
}

export async function recordRunEvent(runId: string, type: string, payload: unknown): Promise<void> {
  const current = token();
  if (!current) return;
  await guarded(() => client.mutation(api.runs.appendEvent, { token: current, runId, type, payload }), null);
}

export async function recordRunFinish(
  runId: string,
  status: CloudRun["status"],
  extra: { errors?: number; warnings?: number; costUsd?: number; tokens?: number; message?: string } = {},
): Promise<void> {
  const current = token();
  if (!current) return;
  await guarded(() => client.mutation(api.runs.finish, { token: current, runId, status, ...extra }), { ok: true });
}

export async function runHistory(limit = 50): Promise<CloudRun[]> {
  const current = token();
  if (!current) return [];
  return await guarded(() => client.query(api.runs.history, { token: current, limit }), []);
}

export async function runEvents(runId: string, after = -1) {
  const current = token();
  if (!current) return [];
  return await guarded(() => client.query(api.runs.events, { token: current, runId, after }), []);
}

// ---------- roster ----------

export type CloudSpecialist = {
  _id: Id<"specialists">;
  specId: string;
  name: string;
  role: string;
  kind: string;
  baseUrl?: string;
  model: string;
  ready: boolean;
};

export async function listSpecialists(): Promise<CloudSpecialist[]> {
  const current = token();
  if (!current) return [];
  return await guarded(() => client.query(api.roster.list, { token: current }), []);
}

export async function saveSpecialist(spec: Omit<CloudSpecialist, "_id">): Promise<void> {
  const current = token();
  if (!current) return;
  await guarded(() => client.mutation(api.roster.save, { token: current, ...spec }), null);
}

// ---------- lessons ----------

export async function recentLessons(limit = 20) {
  return await guarded(() => client.query(api.lessons.recent, { limit }), []);
}

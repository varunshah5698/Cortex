export type Role = "planner" | "frontend" | "backend" | "reviewer" | "research" | "synthesizer" | "general";

export type ProviderKind = "ollama" | "openai" | "anthropic" | "groq" | "openrouter" | "custom";

export type Specialist = {
  id: string;
  name: string;
  role: Role;
  kind: ProviderKind;
  baseUrl: string;
  apiKey: string;
  /** The harness holds a key for this specialist; the key itself never comes back. */
  has_key?: boolean;
  /** The harness considers it runnable (model + URL + key, or a local provider). */
  configured?: boolean;
  /** One-shot: ask the harness to delete this specialist's stored key. Never persisted. */
  clearKey?: boolean;
  model: string;
};

export const roles: { id: Role; label: string; hint: string }[] = [
  { id: "planner", label: "Planner", hint: "Decomposes the prompt, assigns work" },
  { id: "frontend", label: "Frontend", hint: "UI, components, styling" },
  { id: "backend", label: "Backend", hint: "APIs, data, orchestration" },
  { id: "reviewer", label: "Reviewer", hint: "Checks claims and acceptance criteria" },
  { id: "research", label: "Research", hint: "Sources, evidence, verification" },
  { id: "synthesizer", label: "Synthesizer", hint: "Composes the final answer" },
  { id: "general", label: "Generalist", hint: "Any work unit you assign" },
];

export const providers: Record<
  ProviderKind,
  { label: string; baseUrl: string; hint: string; model: string }
> = {
  ollama: {
    label: "Ollama (local)",
    baseUrl: "http://127.0.0.1:11434/v1",
    hint: "http://127.0.0.1:11434/v1",
    model: "gemma4:e4b",
  },
  openai: {
    label: "OpenAI",
    baseUrl: "https://api.openai.com/v1",
    hint: "https://api.openai.com/v1",
    model: "gpt-4.1-mini",
  },
  anthropic: {
    label: "Anthropic",
    baseUrl: "https://api.anthropic.com/v1",
    hint: "https://api.anthropic.com/v1",
    model: "claude-sonnet-4-5",
  },
  groq: {
    label: "Groq",
    baseUrl: "https://api.groq.com/openai/v1",
    hint: "https://api.groq.com/openai/v1",
    model: "llama-3.3-70b-versatile",
  },
  openrouter: {
    label: "OpenRouter",
    baseUrl: "https://openrouter.ai/api/v1",
    hint: "https://openrouter.ai/api/v1",
    model: "google/gemini-2.5-pro",
  },
  custom: {
    label: "Custom / vLLM",
    baseUrl: "",
    hint: "https://your-endpoint/v1",
    model: "",
  },
};

export const roleLabel = (role: Role) => roles.find((r) => r.id === role)?.label ?? "Generalist";

export const roleColor = (role: Role) =>
  ({
    planner: "#7c4fe0",
    frontend: "#3a8bdf",
    backend: "#2fa37a",
    reviewer: "#d98b3a",
    research: "#2f9aa8",
    synthesizer: "#c4579a",
    general: "#7b7288",
  })[role] ?? "#7b7288";

const FREE_HOSTED = "https://text.pollinations.ai/openai/v1";
export const KEYLESS_HOST = "text.pollinations.ai";

/**
 * Four specialists so the workshop opens with a team that actually runs.
 *
 * Local use gets Ollama. A production build is served from a cloud host, where the
 * visitor's `127.0.0.1:11434` is not the server's Ollama: the same default would fail
 * before the first token. The hosted default is keyless and public, so a deployed room
 * can start without asking for three API keys; any card can be pointed elsewhere.
 */
export function defaultRoster(): Specialist[] {
  const base = import.meta.env.PROD
    ? { kind: "openai" as const, baseUrl: FREE_HOSTED, apiKey: "", model: "openai" }
    : { kind: "ollama" as const, baseUrl: providers.ollama.baseUrl, apiKey: "", model: providers.ollama.model };
  return [
    { id: "atlas", name: "Atlas", role: "planner", ...base },
    { id: "vega", name: "Vega", role: "frontend", ...base },
    { id: "orion", name: "Orion", role: "backend", ...base },
    { id: "lyra", name: "Lyra", role: "reviewer", ...base },
  ];
}

function usesLoopback(baseUrl: string): boolean {
  return /localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]/.test(baseUrl);
}

const ROSTER_KEY = "cortex.roster.v1";
const SELECTED_KEY = "cortex.selected.v1";

/** Python harness. Defined here because this module owns the roster wire protocol. */
// The harness defaults to 8000; a second checkout can point its UI elsewhere with
// VITE_HARNESS_URL so two people running the tree do not fight over one port.
// A production build is served by the harness itself, so its API is same-origin.
export const API = import.meta.env.VITE_HARNESS_URL ?? (import.meta.env.PROD ? "" : "http://127.0.0.1:8000");

/**
 * Generated pages get their own origin (`ai_harness/preview.py`), so a script a model
 * wrote is never same-origin with `/api`.
 *
 * The preview origin is the harness API's port + 1 by convention (8000→8001,
 * 8096→8097). Hard-coding 8001 made a second checkout show `{"detail":"unknown run"}`:
 * the room was talking to one harness while the iframe talked to another one's preview.
 * A deployment that places the preview elsewhere sets VITE_PREVIEW_URL.
 */
function previewOrigin(api: string): string {
  const explicit = import.meta.env.VITE_PREVIEW_URL;
  if (explicit) return explicit.replace(/\/$/, "");
  // A production build is served by the harness itself. There is no second loopback
  // port in the browser's world; a deployment with a preview proxy sets the variable.
  if (import.meta.env.PROD) return window.location.origin;
  try {
    const url = new URL(api || window.location.origin);
    const port = Number(url.port || (url.protocol === "https:" ? 443 : 80));
    url.port = String(port + 1);
    return url.origin;
  } catch {
    return "http://127.0.0.1:8001";
  }
}

export const PREVIEW = previewOrigin(API);

/**
 * What the harness will tell us about a specialist: everything except the key.
 * A stored key is reported as a boolean, so a reload can still show "saved".
 */
export type ServerSpecialist = Omit<Specialist, "apiKey"> & { has_key?: boolean; configured?: boolean };

export function loadRoster(): Specialist[] {
  try {
    const raw = localStorage.getItem(ROSTER_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as (Specialist & { apiKey?: string })[];
    if (!Array.isArray(parsed) || !parsed.length) return [];
    // Drop keys written by older builds: they belong on the harness now.
    // `apiKey` stays an empty string, not missing: callers read it without a guard.
    return parsed.map(({ id, name, role, kind, baseUrl, model }) => ({ id, name, role, kind, baseUrl, model, apiKey: "" }));
  } catch {
    return [];
  }
}

/** Cache the non-secret fields only; keys belong to the harness, not to localStorage. */
export function saveRoster(list: Specialist[]) {
  try {
    // Keys are never persisted. `has_key`/`configured` *are*, so a page that has not
    // hydrated yet can still tell a specialist is runnable; every hydrate refreshes them.
    const keyless = list.map(({ apiKey: _apiKey, clearKey: _clearKey, ...rest }) => rest);
    localStorage.setItem(ROSTER_KEY, JSON.stringify(keyless));
  } catch {
    /* storage unavailable */
  }
}

function mergeKeys(local: Specialist[], server: ServerSpecialist[]): Specialist[] {
  const byId = new Map(local.map((s) => [s.id, s]));
  const merged: Specialist[] = server.map((remote) => ({
    ...remote,
    apiKey: byId.get(remote.id)?.apiKey ?? "",
    has_key: Boolean(remote.has_key),
    configured: Boolean(remote.configured),
  }));
  // A specialist added in the workshop but not saved into the harness yet stays in the
  // list: hydrating should refresh what the server knows, never delete a fresh entry.
  const known = new Set(merged.map((s) => s.id));
  for (const draft of local) {
    if (known.has(draft.id)) continue;
    known.add(draft.id);
    merged.push(draft);
  }
  return merged;
}

async function readServerRoster(): Promise<ServerSpecialist[] | null> {
  try {
    const res = await fetch(`${API}/api/roster`);
    if (!res.ok) return null;
    const data = (await res.json()) as { specialists?: ServerSpecialist[] };
    return Array.isArray(data.specialists) ? data.specialists : null;
  } catch {
    return null;
  }
}

/**
 * Pull the roster (keys redacted) so a fresh browser still knows what is configured.
 * Entries this browser has but the harness does not are kept, not dropped: an edit made
 * in the workshop survives a reload and is published by the next run.
 */
function usableRoster(list: Specialist[]): Specialist[] {
  // A cloud-served UI cannot reach the visitor's loopback Ollama. Keep the card's name
  // and role, but point it at the keyless hosted default instead of failing preflight.
  if (!import.meta.env.PROD) return list;
  return list.map((s) =>
    usesLoopback(s.baseUrl)
      ? { ...s, kind: "openai", baseUrl: FREE_HOSTED, apiKey: "", model: "openai" }
      : s,
  );
}

export async function hydrateRoster(): Promise<Specialist[]> {
  const server = await readServerRoster();
  if (!server) return usableRoster(loadRoster());  // unreadable: keep what we have
  const local = loadRoster();
  // `mergeKeys` already appends local drafts the server does not know; appending them
  // again here duplicated every draft and made a 3-person team look like 21 cards.
  const merged = usableRoster(mergeKeys(local, server));
  saveRoster(merged);
  return merged;
}

/**
 * Store the roster server-side. Keys travel here and nowhere else: a run request
 * carries ids only. Returns the redacted roster with the typed keys dropped.
 */
export async function pushRoster(list: Specialist[], replace = true): Promise<Specialist[]> {
  let res: Response;
  try {
    res = await fetch(`${API}/api/roster`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ specialists: list, replace }),
    });
  } catch {
    throw new Error("The harness is not answering on 127.0.0.1:8000, so the roster was not saved.");
  }
  if (!res.ok) {
    const raw = await res.text().catch(() => "");
    let detail = raw.slice(0, 250);
    try {
      detail = (JSON.parse(raw) as { detail?: string }).detail ?? detail;
    } catch {
      /* keep the raw body */
    }
    throw new Error(detail || `Saving the roster failed (${res.status})`);
  }
  const data = (await res.json()) as { specialists?: ServerSpecialist[] };
  const merged = mergeKeys(list, data.specialists ?? []).map((s) => ({ ...s, apiKey: "" }));
  saveRoster(merged);
  return merged;
}

/**
 * Make sure the harness has this roster before a run refers to it by id.
 *
 * Only specialists the harness does not have yet are published. "This browser differs"
 * is not a safe test: a stale local copy holds defaults the user never chose, and pushing
 * it overwrote the harness's real record (a local Ollama specialist came back as OpenAI).
 * Editing an existing specialist is the workshop's explicit save, not a side effect of
 * starting a run, and `replace: false` means nothing unseen is dropped.
 */
export async function ensureRoster(list: Specialist[]): Promise<void> {
  const stored = (await readServerRoster()) ?? [];
  const known = new Set(stored.map((s) => s.id));
  const missing = list.filter((s) => !known.has(s.id));
  if (missing.length) await pushRoster(missing, false);
}

export function loadSelected(): string | null {
  try {
    return localStorage.getItem(SELECTED_KEY);
  } catch {
    return null;
  }
}

export function saveSelected(id: string) {
  try {
    localStorage.setItem(SELECTED_KEY, id);
  } catch {
    /* storage unavailable */
  }
}

export function isConfigured(s: Specialist) {
  if (!s.name.trim() || !s.baseUrl.trim() || !s.model.trim()) return false;
  const local = s.kind === "ollama" || usesLoopback(s.baseUrl);
  const keyless = s.baseUrl.includes(KEYLESS_HOST);
  return local || keyless || Boolean(s.apiKey.trim()) || Boolean(s.has_key) || Boolean(s.configured);
}

export function newSpecialist(index: number): Specialist {
  return {
    id: crypto.randomUUID(),
    name: `Specialist ${index}`,
    role: "general",
    kind: "openai",
    baseUrl: providers.openai.baseUrl,
    apiKey: "",
    model: providers.openai.model,
  };
}

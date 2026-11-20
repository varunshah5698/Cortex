import { API, PREVIEW, ensureRoster, type Specialist } from "./providers";

/** Python harness — src/ai_harness/main.py. Lives in `providers` with the roster wire. */
export { API, PREVIEW };

export type Depth = "quick" | "deep" | "ultra";

export type PlanUnit = {
  id: string;
  title: string;
  role: string;
  objective: string;
  file: string;
  agent: string;
};

export type VerifyIssue = { file: string; level: string; code: string; detail: string };

export type Verification = {
  ok: boolean;
  stage: string;
  errors: number;
  warnings: number;
  checked?: number;
  issues: VerifyIssue[];
};

export type RunBudget = {
  max_tokens: number;
  max_usd: number;
  tokens: number;
  usd: number;
  calls: number;
  remaining_tokens: number | null;
  remaining_usd: number | null;
};

/**
 * What the preview did with a run's files: `null` on every run the pipeline finished,
 * because the pipeline verifies and never compiles. Only `main._record_build` sets it.
 */
export type RunBuild = {
  state: ServiceState;
  kind: string;
  detail: string;
  at: string;
};

export type UsageTotals = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  ms: number;
  calls: number;
  thinking_chars: number;
  cost_usd?: number;
  cached?: number;
};

export type RunMetrics = {
  run_id: string;
  prompt: string;
  depth: string;
  kind?: string;
  answer?: string;
  created: string;
  done: boolean;
  files: number;
  totals: UsageTotals;
  agents: (UsageTotals & { id: string; name: string; role: string; model: string })[];
  budget?: RunBudget;
  cache?: { run_hits: number };
  verification?: Verification | null;
  /** Absent/null until something installed and built the files. */
  build?: RunBuild | null;
  /** False on a run that was verified but never compiled, which is every run's `done`. */
  previewed?: boolean;
  prompts?: Record<string, string>;
};

export type ChatTurn = { role: "user" | "assistant" | "system"; content: string };

/** The verdict of a chat's self-check on its own direction. */
export type Verdict = "on-track" | "adjust";

export type HarnessEvent =
  | { type: "start"; prompt: string; kind?: string; created?: string; resumed?: boolean }
  | { type: "queue"; position: number; concurrency: number; message?: string }
  | { type: "phase"; phase: string }
  | { type: "status"; agent: string; status: string }
  | { type: "plan"; units: PlanUnit[]; objective?: string }
  | { type: "scaffold"; files: string[]; objective?: string }
  | { type: "research"; query: string; title: string; url: string; snippet: string; agent?: string }
  | {
      type: "usage";
      agent: string;
      phase: string;
      prompt_tokens: number;
      completion_tokens: number;
      total_tokens: number;
      ms: number;
      calls: number;
      thinking_chars: number;
      model?: string;
      name?: string;
      role?: string;
      cost_usd?: number;
      cached?: number;
    }
  | {
      type: "verify";
      stage: string;
      ok: boolean;
      issues: VerifyIssue[];
      errors: number;
      warnings: number;
      checked?: number;
      /** Tier 2: whether the artifact's own compiler ran, and what it said. */
      toolchain?: { checked: boolean; note: string } | null;
    }
  | { type: "thrash"; tool?: string; path?: string; count: number; message: string }
  | { type: "fallback"; agent?: string; from: string; to: string; message: string }
  | { type: "retry"; agent?: string; attempt: number; delay_ms: number; message: string }
  | ({ type: "budget"; message: string } & RunBudget)
  | { type: "message"; from: string; to?: string; text: string; kind: string }
  | { type: "think"; agent: string; delta: string }
  | { type: "check"; verdict: Verdict; text: string }
  | { type: "token"; delta: string; agent?: string }
  | {
      type: "tool";
      tool: string;
      args: Record<string, unknown>;
      result: string;
      ms: number;
      ok: boolean;
      agent?: string;
      phase?: string;
      error?: string;
    }
  | { type: "file_start"; agent: string; path: string; mode?: string }
  | { type: "file"; path: string; delta: string }
  | { type: "file_end"; path: string; agent: string; lines: number; revised?: boolean }
  | { type: "guard"; stage: string; heading?: string | null; removed_chars: number }
  | {
      type: "done";
      metrics?: RunMetrics;
      files?: FileRow[];
      answer?: string;
      finished?: string;
      /** The other half of the verdict: `null`/false means verified, never compiled. */
      build?: RunBuild | null;
      previewed?: boolean;
    }
  | { type: "stream_lost" }
  | { type: "error"; message: string; agent?: string };

export async function resumeRun(runId: string): Promise<string> {
  const res = await fetch(`${API}/api/run/${runId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail.slice(0, 250) || `Harness responded ${res.status}`);
  }
  return ((await res.json()) as { run_id: string }).run_id;
}

export async function startRun(prompt: string, specialists: Specialist[], depth: Depth): Promise<string> {
  await ensureRoster(specialists);
  const res = await fetch(`${API}/api/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, specialist_ids: specialists.map((s) => s.id), depth }),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(detail.slice(0, 250) || `Harness responded ${res.status}`);
  }
  return ((await res.json()) as { run_id: string }).run_id;
}

const offline =
  "The harness is not answering on 127.0.0.1:8000 — the chat and the build page share it, so start it with: PYTHONPATH=src ./.venv/bin/python -m uvicorn ai_harness.main:app --port 8000";

export type ProviderTest = {
  ok: boolean;
  detail?: string;
  error?: string;
  models?: string[];
  count?: number;
};

/** Test a provider through the harness, which can reuse a key it already holds. */
export async function testProvider(specialist: Specialist): Promise<ProviderTest> {
  try {
    const res = await fetch(`${API}/api/providers/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        id: specialist.id,
        baseUrl: specialist.baseUrl,
        apiKey: specialist.apiKey,
        kind: specialist.kind,
      }),
    });
    if (!res.ok) return { ok: false, error: `Harness responded ${res.status}` };
    return (await res.json()) as ProviderTest;
  } catch {
    return { ok: false, error: offline };
  }
}

/** Start a chat as a harness run; stream it with the same `subscribe` a build uses. */
export async function startChat(
  specialist: Specialist,
  messages: ChatTurn[],
  depth: string,
): Promise<string> {
  await ensureRoster([specialist]);

  let res: Response;
  try {
    res = await fetch(`${API}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ specialist_id: specialist.id, messages, depth }),
    });
  } catch {
    throw new Error(offline);
  }
  if (!res.ok) {
    const raw = await res.text().catch(() => "");
    let detail = raw.slice(0, 250);
    try {
      detail = (JSON.parse(raw) as { detail?: string }).detail ?? detail;
    } catch {
      /* keep the raw body */
    }
    throw new Error(detail || `Harness responded ${res.status}`);
  }
  return ((await res.json()) as { run_id: string }).run_id;
}

export async function stopRun(id: string): Promise<void> {
  await fetch(`${API}/api/run/${id}/stop`, { method: "POST" }).catch(() => {});
}

export function subscribe(id: string, onEvent: (event: HarnessEvent) => void): EventSource {
  const source = new EventSource(`${API}/api/run/${id}/events`);
  let reported = false;
  source.onmessage = (message) => {
    reported = false;
    try {
      onEvent(JSON.parse(message.data) as HarnessEvent);
    } catch {
      /* ignore malformed frames */
    }
  };
  source.onerror = () => {
    if (reported) return;
    reported = true;
    onEvent({ type: "stream_lost" });
  };
  return source;
}

/** Generated files, on the artifact origin (never the API's). */
export const previewUrl = (runId: string, path = ""): string => `${PREVIEW}/preview/${runId}/${path}`;

/** A run's served app, through the token-gated proxy on the artifact origin. */
export const liveUrl = (runId: string, token: string, path = ""): string =>
  `${PREVIEW}/live/${runId}/${token}/${path}`;

export type ServiceState =
  | "idle"
  | "detecting"
  | "installing"
  | "building"
  | "starting"
  | "ready"
  | "failed"
  | "stopped"
  | "needs_approval";

/** What a real browser saw: the tier the static checks and HTTP probe cannot see. */
export type BrowserReport = {
  ok: boolean;
  skipped?: boolean;
  stage: "browser";
  errors: number;
  warnings: number;
  checked: number;
  issues: { file: string; level: string; code: string; detail: string }[];
  url?: string;
  title?: string;
  interactions?: { label: string; clicked: boolean; changed: boolean; newErrors: number }[];
  note?: string;
};

/** The live service a run's files were installed, built and started as. */
export type RunService = {
  run_id: string;
  state: ServiceState;
  kind: string;
  detail: string;
  url: string | null;
  port: number | null;
  /** Per-run secret: the served app is only reachable through the preview proxy with it. */
  token?: string | null;
  exit_code: number | null;
  /** Commands waiting on a human approval, when state is `needs_approval`. */
  pending?: string[];
  /** What those commands will actually do: dependencies, scripts, the entry file. */
  detail_lines?: string[];
  /** Whether the processes are contained, in the service's own words. */
  sandbox?: string;
  /** The last browser verdict, when the served page has been opened in Chrome. */
  browser?: BrowserReport | null;
  /** True when the run executes on the separately deployed cloud runner. */
  remote?: boolean;
  /** Seconds until a cloud runner removes the run. */
  expires_in?: number;
  logs: string[];
};

const service = async (res: Response): Promise<RunService> => {
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail || `the harness answered ${res.status}`);
  }
  return (await res.json()) as RunService;
};

/**
 * Start the service. `restart` stops the running one and installs/builds/serves again.
 * `approve` is the human checkpoint: without it an install or build comes back as
 * `needs_approval` naming the command, and nothing runs.
 */
export async function startService(runId: string, restart = false, approve = false): Promise<RunService> {
  const params = new URLSearchParams();
  if (restart) params.set("restart", "1");
  if (approve) params.set("approve", "1");
  const query = params.toString() ? `?${params.toString()}` : "";
  return service(await fetch(`${API}/api/run/${runId}/serve${query}`, { method: "POST" }));
}

export async function stopService(runId: string): Promise<RunService> {
  return service(await fetch(`${API}/api/run/${runId}/serve/stop`, { method: "POST" }));
}

export async function serviceStatus(runId: string): Promise<RunService> {
  return service(await fetch(`${API}/api/run/${runId}/serve`));
}

/** A one-run public share: the tunnel's URL, or the approval it is waiting on. */
export type ShareState = "idle" | "needs_approval" | "starting" | "ready" | "stopped" | "failed";

export type RunShare = {
  run_id: string;
  state: ShareState;
  url: string | null;
  detail?: string;
  command?: string;
  expires_at?: string | null;
  logs?: string[];
};

export async function startShare(runId: string, approve = false): Promise<RunShare> {
  const query = approve ? "?approve=1" : "";
  const res = await fetch(`${API}/api/run/${runId}/share${query}`, { method: "POST" });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail || `share failed (${res.status})`);
  }
  return (await res.json()) as RunShare;
}

const share = async (res: Response): Promise<RunShare> => {
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail || `share failed (${res.status})`);
  }
  return (await res.json()) as RunShare;
};

export async function stopShare(runId: string): Promise<RunShare> {
  return share(await fetch(`${API}/api/run/${runId}/share/stop`, { method: "POST" }));
}

export async function shareStatus(runId: string): Promise<RunShare> {
  return share(await fetch(`${API}/api/run/${runId}/share`));
}

/** Open the served page in Chrome: console errors, failed requests, one click. */
export async function checkBrowser(runId: string): Promise<BrowserReport> {
  const res = await fetch(`${API}/api/run/${runId}/browser-check`, { method: "POST" });
  if (!res.ok) {
    const body = (await res.json().catch(() => ({}))) as { detail?: string };
    throw new Error(body.detail || `browser check failed (${res.status})`);
  }
  return (await res.json()) as BrowserReport;
}

export const SERVICE_BUSY: ServiceState[] = ["detecting", "installing", "building", "starting"];

export type FileRow = { path: string; agent: string | null; lines: number; done?: boolean };

export async function loadFiles(runId: string): Promise<{ files: FileRow[] }> {
  const res = await fetch(`${API}/api/run/${runId}/files`);
  if (!res.ok) return { files: [] };
  return (await res.json()) as { files: FileRow[] };
}

export async function loadFile(runId: string, path: string): Promise<string> {
  const res = await fetch(`${API}/api/run/${runId}/file?path=${encodeURIComponent(path)}`);
  if (!res.ok) return "";
  return ((await res.json()) as { content: string }).content;
}

export async function loadRuns(): Promise<RunMetrics[]> {
  try {
    const res = await fetch(`${API}/api/runs`);
    if (!res.ok) return [];
    return ((await res.json()) as { runs: RunMetrics[] }).runs;
  } catch {
    return [];
  }
}

export const fmtNumber = (n: number) =>
  n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : `${n}`;

export const fmtMs = (ms: number) =>
  ms >= 60000 ? `${(ms / 60000).toFixed(1)}m` : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;

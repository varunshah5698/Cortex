import { startChat, stopRun, subscribe, type HarnessEvent, type Verdict } from "./harness";
import type { Specialist } from "./providers";

export type { Verdict };

/** A build run started from a chat answer, pinned to the message it came from. */
export type BuildCard = {
  runId: string;
  prompt: string;
  at: number;
  status: "running" | "done" | "error";
  files: number;
  tokens: number;
  verdict?: { ok: boolean; errors: number; warnings: number } | null;
  error?: string;
};

export type ChatMessage = {
  role: "system" | "user" | "assistant";
  content: string;
  /** The visible reasoning a harness run produced before answering. */
  reasoning?: string;
  /** The direction check the agent ran against its own reasoning. */
  check?: string;
  verdict?: Verdict;
  /** Present once this answer has been handed to the build room. */
  build?: BuildCard;
};

/** The phases the chat run reports; a finished answer becomes a stored message. */
export type ChatPhase = "idle" | "thinking" | "check" | "answering";

/** How much reasoning one message may spend before it answers. */
export type ReasoningDepth = "off" | "brief" | "deep" | "ultra";

/**
 * Each step is a model call, and the wait is that many round trips: `off` answers
 * directly, `brief` reasons and then answers, `deep` also checks its own direction.
 * The seconds are a nominal 20s a call; real latency depends on the provider.
 */
export const REASONING: Record<ReasoningDepth, { label: string; calls: number; seconds: number; hint: string }> = {
  off: { label: "Off", calls: 1, seconds: 20, hint: "Answer only — one model call, no reasoning pass" },
  brief: { label: "Brief", calls: 2, seconds: 40, hint: "Short reasoning, then the answer" },
  deep: {
    label: "Deep",
    calls: 3,
    seconds: 60,
    hint: "Reasoning, a self-check of its direction, then the answer",
  },
  ultra: {
    label: "Ultra",
    calls: 4,
    seconds: 90,
    hint: "Long reasoning, a self-check, an adversarial second read, then the answer",
  },
};

export const REASONING_ORDER: ReasoningDepth[] = ["off", "brief", "deep", "ultra"];

/**
 * The prompt a build is seeded with: the request, the agreed answer, and the reasoning
 * that produced it — so the build team starts from the direction the chat settled on
 * instead of re-deriving it, and is told not to restate it.
 */
export function buildSeed(messages: ChatMessage[], index: number): string {
  const answer = messages[index];
  const question = [...messages.slice(0, index)].reverse().find((m) => m.role === "user")?.content ?? "";
  const reasoning = (answer.reasoning ?? "").trim();
  const parts = [
    "Turn this into a working artifact.",
    `Request:\n${question.trim() || answer.content.trim()}`,
    `Agreed direction (the assistant's answer):\n${answer.content.trim()}`,
  ];
  if (reasoning) parts.push(`Reasoning already settled (do not restate it):\n${reasoning.slice(-1200)}`);
  return parts.join("\n\n");
}

type Args = {
  specialist: Specialist;
  messages: ChatMessage[];
  signal: AbortSignal;
  depth: ReasoningDepth;
  onPhase?: (phase: ChatPhase) => void;
  onThink?: (delta: string) => void;
  onCheck?: (text: string, verdict: Verdict) => void;
  onToken?: (delta: string) => void;
};

/**
 * Stream one answer from the shared Python harness.
 *
 * The chat is a run like any build: POST /api/chat registers it, then this reads the
 * same `/api/run/{id}/events` stream through the same `subscribe` the build page uses.
 */
export async function streamChatHarness({
  specialist,
  messages,
  signal,
  depth,
  onPhase,
  onThink,
  onCheck,
  onToken,
}: Args): Promise<string> {
  if (!specialist.baseUrl.trim() || !specialist.model.trim()) {
    throw new Error("Finish this specialist's setup first (base URL and model ID).");
  }
  if (signal.aborted) throw new DOMException("Aborted", "AbortError");

  const runId = await startChat(
    specialist,
    messages.map(({ role, content }) => ({ role, content })),
    depth,
  );

  return await new Promise<string>((resolve, reject) => {
    let full = "";
    let settled = false;
    let source: EventSource | null = null;

    const finish = (failure?: Error) => {
      if (settled) return;
      settled = true;
      source?.close();
      signal.removeEventListener("abort", onAbort);
      if (failure) reject(failure);
      else if (!full.trim()) reject(new Error(`${specialist.name} returned an empty answer.`));
      else resolve(full);
    };

    function onAbort() {
      void stopRun(runId);
      finish(new DOMException("Aborted", "AbortError"));
    }

    signal.addEventListener("abort", onAbort, { once: true });

    const handle = (event: HarnessEvent) => {
      switch (event.type) {
        case "phase":
          if (event.phase) onPhase?.(event.phase as ChatPhase);
          break;
        case "think":
          if (event.delta) onThink?.(event.delta);
          break;
        case "check":
          onCheck?.(event.text ?? "", event.verdict ?? "adjust");
          break;
        case "token":
          if (event.delta) {
            full += event.delta;
            onToken?.(event.delta);
          }
          break;
        case "error":
          finish(new Error(event.message));
          break;
        case "done":
          // The harness strips a leaked reflection section before finishing; its
          // answer is authoritative, so the saved message matches what it returned.
          if (event.answer) full = event.answer;
          finish();
          break;
        case "stream_lost":
          finish(new Error("The harness stream dropped before the answer finished."));
          break;
        default:
          break;
      }
    };

    source = subscribe(runId, handle);
  });
}

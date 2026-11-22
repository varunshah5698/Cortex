import { useCallback, useEffect, useState } from "react";
import type { ChatMessage } from "./chat";

export type Thread = {
  id: string;
  title: string;
  messages: ChatMessage[];
  updatedAt: number;
};

export const seedPrompts: { group: string; items: string[] }[] = [
  {
    group: "Today",
    items: [
      "Create a detailed 7-day sprint plan for the onboarding redesign",
      "Draft a concise email to stakeholders about the Q1 timeline shift",
      "Analyze the 'Eisenhower Matrix' and how it applies to my week",
    ],
  },
  {
    group: "Yesterday",
    items: [
      "Summarize the main differences between RAG and long-context prompting",
      "I need to negotiate an extension for the deliverable without losing trust",
    ],
  },
  {
    group: "7 days",
    items: [
      "Generate 5 effective morning habits for a deep-work schedule",
      "As a non-technical PM, list 5 crucial questions to ask an ML engineer",
      "Help me allocate 8 hours tomorrow: research, writing, and review",
      "We need a creative name for our new analytics product",
      "Write a 100-word positive feedback note for a teammate",
    ],
  },
];

const STORAGE_KEY = "cortex.threads.v1";
const ACTIVE_KEY = "cortex.active.v1";

/** The thread that was open when the page was last closed. */
export function loadActive(): string | null {
  try {
    return localStorage.getItem(ACTIVE_KEY);
  } catch {
    return null;
  }
}

export function saveActive(id: string | null) {
  try {
    if (id) localStorage.setItem(ACTIVE_KEY, id);
    else localStorage.removeItem(ACTIVE_KEY);
  } catch {
    /* storage may be unavailable */
  }
}

function load(): Thread[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as Thread[];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function useThreads() {
  const [threads, setThreads] = useState<Thread[]>(load);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(threads));
    } catch {
      /* storage may be unavailable */
    }
  }, [threads]);

  const patch = useCallback((id: string, update: (t: Thread) => Thread) => {
    setThreads((prev) => prev.map((t) => (t.id === id ? update(t) : t)));
  }, []);

  return { threads, setThreads, patch };
}

export function titleFor(text: string) {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > 64 ? `${clean.slice(0, 64)}…` : clean;
}

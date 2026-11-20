import { useCallback, useEffect, useRef, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { Composer } from "./components/Composer";
import { Greeting, SuggestionCards, FooterNote } from "./components/EmptyState";
import { ChatView } from "./components/ChatView";
import { Icon } from "./components/Icon";
import {
  buildSeed,
  streamChatHarness,
  type BuildCard,
  type ChatMessage,
  type ChatPhase,
  type ReasoningDepth,
  type Verdict,
} from "./lib/chat";
import { startRun } from "./lib/harness";
import { hydrateRoster, loadRoster, loadSelected, saveSelected } from "./lib/providers";
import { titleFor, useThreads } from "./lib/threads";
import { navigate } from "./lib/router";

export default function App() {
  const { threads, setThreads, patch } = useThreads();
  // Hydrated from the harness on mount; readiness is server truth, not localStorage.
  const [roster, setRoster] = useState(() => loadRoster());
  useEffect(() => {
    let alive = true;
    hydrateRoster()
      .then((list) => {
        if (alive) setRoster(list);
      })
      .catch(() => {
        /* harness offline: keep the local copy */
      });
    return () => {
      alive = false;
    };
  }, []);
  const [selectedId, setSelectedId] = useState<string>(() => {
    const saved = loadSelected();
    return saved && roster.some((r) => r.id === saved) ? saved : roster[0]?.id ?? "";
  });
  const [activeId, setActiveId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [depth, setDepth] = useState<ReasoningDepth>("brief");
  const [streaming, setStreaming] = useState<string | null>(null);
  const [thought, setThought] = useState("");
  const [check, setCheck] = useState("");
  const [verdict, setVerdict] = useState<Verdict | "">("");
  const [phase, setPhase] = useState<ChatPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const abortRef = useRef<AbortController | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const active = threads.find((t) => t.id === activeId) ?? null;
  const messages = active?.messages ?? [];
  const specialist = roster.find((s) => s.id === selectedId) ?? roster[0];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!activeId) {
      setTimeout(() => document.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus(), 30);
    }
  }, [activeId]);

  const pickSpecialist = (id: string) => {
    setSelectedId(id);
    saveSelected(id);
    setError(null);
  };

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
    setStreaming(null);
    setPhase("idle");
  }, []);

  const send = useCallback(
    async (raw?: string) => {
      const content = (raw ?? draft).trim();
      if (!content || busy || !specialist) return;

      setError(null);
      setDraft("");
      setBusy(true);
      setStreaming("");
      setThought("");
      setCheck("");
      setVerdict("");
      setPhase("thinking");

      let threadId = activeId;
      let history: ChatMessage[] = [];

      if (!threadId) {
        threadId = crypto.randomUUID();
        const thread = {
          id: threadId,
          title: titleFor(content),
          messages: [{ role: "user", content } as ChatMessage],
          updatedAt: Date.now(),
        };
        setThreads((prev) => [thread, ...prev]);
        setActiveId(threadId);
        history = thread.messages;
      } else {
        patch(threadId, (t) => ({
          ...t,
          title: t.messages.some((m) => m.role === "user") ? t.title : titleFor(content),
          messages: [...t.messages, { role: "user", content }],
          updatedAt: Date.now(),
        }));
        history = [...(threads.find((t) => t.id === threadId)?.messages ?? []), { role: "user", content }];
      }

      const controller = new AbortController();
      abortRef.current = controller;
      let acc = "";
      let thinking = "";
      let selfCheck = "";
      let selfVerdict: Verdict | "" = "";

      try {
        const answer = await streamChatHarness({
          specialist,
          signal: controller.signal,
          depth,
          messages: history.map((m) => ({ role: m.role, content: m.content })),
          onPhase: setPhase,
          onThink: (delta) => {
            thinking += delta;
            setThought(thinking);
          },
          onCheck: (text, value) => {
            selfCheck = text;
            selfVerdict = value;
            setCheck(text);
            setVerdict(value);
          },
          onToken: (token) => {
            acc += token;
            setStreaming(acc);
          },
        });
        patch(threadId, (t) => ({
          ...t,
          messages: [
            ...t.messages,
            {
              role: "assistant",
              content: answer || acc,
              reasoning: thinking.trim(),
              check: selfCheck,
              verdict: selfVerdict || undefined,
            },
          ],
          updatedAt: Date.now(),
        }));
      } catch (err) {
        const aborted = err instanceof DOMException && err.name === "AbortError";
        if (!aborted) {
          setError(err instanceof Error ? err.message : "Cortex did not answer.");
        }
        if (acc) {
          patch(threadId, (t) => ({
            ...t,
            messages: [
              ...t.messages,
              {
                role: "assistant",
                content: aborted ? `${acc}\n\n_(stopped)_` : acc,
                reasoning: thinking.trim() || undefined,
                check: selfCheck || undefined,
                verdict: selfVerdict || undefined,
              },
            ],
            updatedAt: Date.now(),
          }));
        }
      } finally {
        abortRef.current = null;
        setBusy(false);
        setStreaming(null);
        setPhase("idle");
      }
    },
    [activeId, busy, depth, draft, patch, setThreads, specialist, threads],
  );

  // Hand this answer to the build room: the run is seeded with the question, the answer
  // and the reasoning behind it, and pinned to the message as a card.
  const buildFrom = useCallback(
    async (index: number) => {
      const thread = threads.find((t) => t.id === activeId);
      const message = thread?.messages[index];
      if (!thread || !message || message.role !== "assistant") return;

      const seed = buildSeed(thread.messages, index);
      const patchCard = (next: (card: BuildCard) => BuildCard) =>
        patch(thread.id, (t) => ({
          ...t,
          updatedAt: Date.now(),
          messages: t.messages.map((m, i) =>
            i === index && m.build ? { ...m, build: next(m.build) } : m,
          ),
        }));

      patch(thread.id, (t) => ({
        ...t,
        updatedAt: Date.now(),
        messages: t.messages.map((m, i) =>
          i === index
            ? {
                ...m,
                build: {
                  runId: "",
                  prompt: seed,
                  at: Date.now(),
                  status: "running",
                  files: 0,
                  tokens: 0,
                } satisfies BuildCard,
              }
            : m,
        ),
      }));

      try {
        const runId = await startRun(seed, roster, "deep");
        patchCard((card) => ({ ...card, runId }));
      } catch (err) {
        const text = err instanceof Error ? err.message : "Cortex did not start the run.";
        patchCard((card) => ({ ...card, status: "error", error: text }));
      }
    },
    [activeId, patch, roster, threads],
  );

  const patchBuildCard = useCallback(
    (index: number, update: Partial<BuildCard>) => {
      if (!activeId) return;
      patch(activeId, (t) => ({
        ...t,
        updatedAt: Date.now(),
        messages: t.messages.map((m, i) =>
          i === index && m.build ? { ...m, build: { ...m.build, ...update } } : m,
        ),
      }));
    },
    [activeId, patch],
  );

  const newChat = () => {
    stop();
    setActiveId(null);
    setDraft("");
    setError(null);
  };

  const usePrompt = (text: string) => {
    setActiveId(null);
    setDraft(text);
    setTimeout(() => document.querySelector<HTMLTextAreaElement>(".composer textarea")?.focus(), 30);
  };

  const exportChat = () => {
    if (!messages.length) return;
    const body = messages
      .map((m) => `${m.role === "user" ? "## You" : `## ${specialist?.name ?? "Cortex"}`}\n\n${m.content}`)
      .join("\n\n");
    const blob = new Blob([`# ${active?.title ?? "Cortex chat"}\n\n${body}\n`], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${(active?.title ?? "cortex-chat").slice(0, 40).replace(/[^\w -]/g, "")}.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const isEmpty = messages.length === 0 && streaming === null;

  return (
    <div className="page">
      <div className="frame">
        <Sidebar
          collapsed={collapsed}
          onToggle={() => setCollapsed((v) => !v)}
          threads={threads}
          activeId={activeId}
          query={query}
          onQuery={setQuery}
          searchRef={searchRef}
          onNewChat={newChat}
          onOpenThread={setActiveId}
          onUsePrompt={usePrompt}
        />

        <main className="main">
          <Topbar
            specialists={roster}
            selectedId={selectedId}
            onSelect={pickSpecialist}
            onExport={exportChat}
            onSetup={() => navigate("workshop")}
            onBuild={() => navigate("build")}
          />

          <div className={`content${isEmpty ? " is-empty" : ""}`}>
            {isEmpty ? (
              <div className="hero">
                <Greeting />
                <div className="hero-stack">
                  <Composer
                    value={draft}
                    onChange={setDraft}
                    onSend={() => send()}
                    onStop={stop}
                    busy={busy}
                    depth={depth}
                    onDepth={setDepth}
                  />
                  <SuggestionCards onPick={(text) => send(text)} />
                </div>
              </div>
            ) : (
              <>
                <ChatView
                  messages={messages}
                  streaming={streaming}
                  error={error}
                  thought={thought}
                  check={check}
                  verdict={verdict}
                  phase={phase}
                  onBuild={(index) => void buildFrom(index)}
                  onBuildChange={patchBuildCard}
                />
                <div className="dock">
                  <Composer
                    value={draft}
                    onChange={setDraft}
                    onSend={() => send()}
                    onStop={stop}
                    busy={busy}
                    depth={depth}
                    onDepth={setDepth}
                  />
                </div>
              </>
            )}
          </div>

          <div className="floating">
            <button className="round-btn ghost" title="Language">
              <Icon name="translate" size={17} />
            </button>
            <button className="round-btn ghost" title="Help">
              <Icon name="question" size={17} />
            </button>
          </div>

          <FooterNote />
        </main>
      </div>
    </div>
  );
}

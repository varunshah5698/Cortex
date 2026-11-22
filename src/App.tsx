import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Sidebar } from "./components/Sidebar";
import { Topbar } from "./components/Topbar";
import { Composer, type Mode } from "./components/Composer";
import { Greeting, SuggestionCards } from "./components/EmptyState";
import { ChatView } from "./components/ChatView";
import { WorkPanel, type PanelTab } from "./components/WorkPanel";
import { Icon } from "./components/Icon";
import {
  buildSeed,
  streamChatHarness,
  streamFlock,
  type BuildCard,
  type ChatMessage,
  type ChatPhase,
  type FlockTask,
  type ReasoningDepth,
  type Verdict,
} from "./lib/chat";
import { startRun } from "./lib/harness";
import { hydrateRoster, isConfigured, loadRoster, loadSelected, saveSelected } from "./lib/providers";
import { loadActive, saveActive, titleFor, useThreads } from "./lib/threads";
import { appendMessage, createThread, recordRunStart } from "./lib/cloud";
import { navigate } from "./lib/router";
import { useSession } from "./lib/auth";

/** The context window the composer's meter is measured against. */
const CONTEXT_WINDOW = 200_000;

export default function App() {
  const { threads, setThreads, patch } = useThreads();
  const { user } = useSession();
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
  // Restore the open conversation. A reload used to drop the person back to the empty
  // state with their thread still in the rail, which reads as lost work.
  const [activeId, setActiveId] = useState<string | null>(() => {
    const saved = loadActive();
    return saved && threads.some((t) => t.id === saved) ? saved : null;
  });
  useEffect(() => saveActive(activeId), [activeId]);
  const [collapsed, setCollapsed] = useState(false);
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [depth, setDepth] = useState<ReasoningDepth>("brief");
  const [mode, setMode] = useState<Mode>("solo");
  const [team, setTeam] = useState<string[]>([]);
  const [streaming, setStreaming] = useState<string | null>(null);
  const [thought, setThought] = useState("");
  const [check, setCheck] = useState("");
  const [verdict, setVerdict] = useState<Verdict | "">("");
  const [phase, setPhase] = useState<ChatPhase>("idle");
  const [tasks, setTasks] = useState<FlockTask[]>([]);
  const [synthesizing, setSynthesizing] = useState(false);
  const [spent, setSpent] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [panelOpen, setPanelOpen] = useState(true);
  const [panelTab, setPanelTab] = useState<PanelTab>("work");
  const [view, setView] = useState<"chat" | "account">("chat");
  // A run can be reopened from the URL (`#/app?run=<id>`) or from the last build, so a
  // preview survives a reload instead of disappearing with the page.
  const runFromUrl = () => new URLSearchParams(window.location.hash.split("?")[1] ?? "").get("run");
  const [runId, setRunId] = useState<string | null>(
    () => runFromUrl() || localStorage.getItem("cortex:lastRun") || null,
  );
  // The hash is the address of a run, so it has to be read on every change: initialising
  // from it once meant `?run=<id>` was ignored for the whole life of the page and the
  // panel kept showing whichever run happened to be in storage.
  useEffect(() => {
    const onHash = () => {
      const next = runFromUrl();
      if (next) setRunId(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (runId) localStorage.setItem("cortex:lastRun", runId);
  }, [runId]);

  const abortRef = useRef<AbortController | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const active = threads.find((t) => t.id === activeId) ?? null;
  const messages = active?.messages ?? [];
  const specialist = roster.find((s) => s.id === selectedId) ?? roster[0];
  const ready = useMemo(() => roster.filter(isConfigured), [roster]);

  // Default the flock to the specialists that can actually run, so the mode is usable
  // the moment it is switched on instead of asking for a team first.
  useEffect(() => {
    if (team.length) return;
    const ids = ready.slice(0, 4).map((s) => s.id);
    if (ids.length >= 2) setTeam(ids);
  }, [ready, team.length]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        searchRef.current?.focus();
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "\\") {
        e.preventDefault();
        setPanelOpen((v) => !v);
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
    setSynthesizing(false);
  }, []);

  const resetTurn = () => {
    setStreaming("");
    setThought("");
    setCheck("");
    setVerdict("");
    setPhase("thinking");
    setTasks([]);
    setSynthesizing(false);
    setSpent(0);
    setRunId(null);
  };

  const send = useCallback(
    async (raw?: string) => {
      const content = (raw ?? draft).trim();
      if (!content || busy) return;

      const flockTeam = team
        .map((id) => roster.find((s) => s.id === id))
        .filter((s): s is NonNullable<typeof s> => Boolean(s) && isConfigured(s as never));
      const useFlock = mode === "flock" && flockTeam.length >= 2;
      // Build mode is the only path that scaffolds a tree and writes files. Solo and
      // flock both answer in prose, so a request typed here used to come back as an essay
      // with no files and no scaffold — the build pipeline existed but nothing in the
      // composer reached it.
      if (mode === "build") {
        const teamForBuild = flockTeam.length ? flockTeam : roster.filter((s) => isConfigured(s as never));
        if (!teamForBuild.length) {
          setError("No specialist has a model and a key yet — configure the roster first.");
          return;
        }
        setError(null);
        setDraft("");
        setBusy(true);
        resetTurn();

        // A build is a turn in the conversation, not a detour: the request and the run
        // card belong in the thread, or the person is left holding a panel with no record
        // of what they asked for.
        let buildThread = activeId;
        if (!buildThread) {
          buildThread = crypto.randomUUID();
          setThreads((prev) => [
            { id: buildThread!, title: titleFor(content), messages: [{ role: "user", content }], updatedAt: Date.now() },
            ...prev,
          ]);
          setActiveId(buildThread);
        } else {
          patch(buildThread, (t) => ({
            ...t,
            messages: [...t.messages, { role: "user", content }],
            updatedAt: Date.now(),
          }));
        }

        try {
          // `depth` here is the chat's reasoning depth (off/brief/deep/ultra); a build
          // takes its own (quick/deep/ultra). Map rather than cast: `off` and `brief` are
          // both "no extra passes" for a build, which is `quick`.
          const buildDepth = depth === "ultra" ? "ultra" : depth === "deep" ? "deep" : "quick";
          const id = await startRun(content, teamForBuild, buildDepth);
          setRunId(id);
          setPanelOpen(true);
          setPanelTab("files");
          patch(buildThread, (t) => ({
            ...t,
            updatedAt: Date.now(),
            messages: [
              ...t.messages,
              {
                role: "assistant",
                content: `Building this now: **${content}**. The files land in the panel as the team writes them.`,
                build: { runId: id, prompt: content, at: Date.now(), status: "running", files: 0, tokens: 0 },
              },
            ],
          }));
          void recordRunStart(id, content, buildDepth);
        } catch (err) {
          setError(err instanceof Error ? err.message : "Cortex did not start the build.");
        } finally {
          setBusy(false);
        }
        return;
      }
      if (!useFlock && !specialist) return;

      setError(null);
      setDraft("");
      setBusy(true);
      resetTurn();

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
        void (async () => {
          const cloudId = await createThread(thread.title);
          if (cloudId) await appendMessage(cloudId, "user", content);
        })();
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
      let parts: FlockTask[] = [];
      let tokens = 0;

      try {
        const answer = useFlock
          ? await streamFlock({
              team: flockTeam,
              signal: controller.signal,
              depth,
              messages: history.map((m) => ({ role: m.role, content: m.content })),
              onPhase: setPhase,
              onTasks: (list) => {
                parts = list;
                setTasks(list);
                setPanelTab("work");
              },
              onTaskStatus: (id, status) => {
                parts = parts.map((p) => (p.id === id ? { ...p, status } : p));
                setTasks(parts);
              },
              onSynth: () => setSynthesizing(true),
              onToken: (token) => {
                acc += token;
                tokens += Math.ceil(token.length / 4);
                setSpent(tokens);
                setStreaming(acc);
              },
            })
          : await streamChatHarness({
              specialist: specialist!,
              signal: controller.signal,
              depth,
              messages: history.map((m) => ({ role: m.role, content: m.content })),
              onPhase: setPhase,
              onThink: (delta) => {
                thinking += delta;
                tokens += Math.ceil(delta.length / 4);
                setSpent(tokens);
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
                tokens += Math.ceil(token.length / 4);
                setSpent(tokens);
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
              reasoning: thinking.trim() || undefined,
              check: selfCheck || undefined,
              verdict: selfVerdict || undefined,
              flock: parts.length ? parts : undefined,
              tokens,
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
                flock: parts.length ? parts : undefined,
                tokens,
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
        setSynthesizing(false);
      }
    },
    [activeId, busy, depth, draft, mode, patch, roster, setThreads, specialist, team, threads],
  );

  // Hand this answer to a build run: the run is seeded with the question, the answer
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
        const id = await startRun(seed, roster, "deep");
        patchCard((card) => ({ ...card, runId: id }));
        setRunId(id);
        setPanelOpen(true);
        setPanelTab("files");
        void recordRunStart(id, seed, "deep");
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
    setTasks([]);
    setRunId(null);
    setView("chat");
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
  // While a turn runs, the panel shows that turn. Between turns it shows the last
  // answer's flock, so reopening a conversation does not read as an idle room.
  const lastAnswer = [...messages].reverse().find((m) => m.role === "assistant");
  const panelTasks = busy ? tasks : (lastAnswer?.flock ?? []);
  const panelSpent = busy ? spent : (lastAnswer?.tokens ?? 0);
  // A build's files live under its run, not under whatever was built last: reopening a
  // thread must show that thread's run, or the panel is showing someone else's work.
  const panelRun = runId ?? lastAnswer?.build?.runId ?? null;
  // A build is "live" only while its card says running: a failed or finished build is
  // still worth showing, but calling it live is a lie the panel would repeat.
  const building = lastAnswer?.build?.status === "running";
  // The card only describes the run it names. A run opened from history or the URL has no
  // card, so its status must come from the run itself, not from this thread's last build.
  const cardForRun = lastAnswer?.build?.runId === panelRun ? lastAnswer.build : undefined;
  const flockTeam = team
    .map((id) => roster.find((s) => s.id === id))
    .filter((s): s is NonNullable<typeof s> => Boolean(s));
  const panelTeam = mode === "flock" ? flockTeam : specialist ? [specialist] : [];

  return (
    <div className={`shell${collapsed ? " is-rail-closed" : ""}${panelOpen ? "" : " is-panel-closed"}`}>
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
        agents={ready.length}
      />

      <main className="main">
        <Topbar
          specialists={roster}
          selectedId={selectedId}
          view={view}
          onView={setView}
          panelOpen={panelOpen}
          onPanel={() => setPanelOpen((v) => !v)}
          onExport={exportChat}
          onSetup={() => navigate("workshop")}
        />

        {view === "account" ? (
          <div className="account">
            <section className="account-card">
              <h2>Specialists</h2>
              <p>
                {ready.length} of {roster.length} configured. A flock needs at least two.
              </p>
              <ul className="account-list">
                {roster.map((s) => (
                  <li key={s.id}>
                    <b>{s.name}</b>
                    <em>{s.model || "no model"}</em>
                    <span className={isConfigured(s) ? "is-ok" : "is-off"}>
                      {isConfigured(s) ? "ready" : "not configured"}
                    </span>
                  </li>
                ))}
              </ul>
              <button className="solid-btn" onClick={() => navigate("workshop")}>
                Open the workshop
              </button>
            </section>
            <section className="account-card">
              <h2>Workspace</h2>
              <p>Runs, files and metrics live beside the chat, not in a separate room.</p>
              <div className="account-links">
                <button onClick={() => navigate("files")}>
                  <Icon name="folder" size={14} /> Files
                </button>
                <button onClick={() => navigate("metrics")}>
                  <Icon name="route" size={14} /> Metrics
                </button>
              </div>
            </section>
          </div>
        ) : (
          <div className={`content${isEmpty ? " is-empty" : ""}`}>
            {isEmpty ? (
              <div className="welcome">
                <Greeting name={user?.name} />
                <div className="welcome-stack">
                  <Composer
                    value={draft}
                    onChange={setDraft}
                    onSend={() => send()}
                    onStop={stop}
                    busy={busy}
                    depth={depth}
                    onDepth={setDepth}
                    mode={mode}
                    onMode={setMode}
                    specialists={roster}
                    selectedId={selectedId}
                    onSelect={pickSpecialist}
                    team={team}
                    onTeam={setTeam}
                    spent={spent}
                    contextWindow={CONTEXT_WINDOW}
                    placeholder="Describe a task"
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
                  tasks={tasks}
                  team={panelTeam}
                  synthesizing={synthesizing}
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
                    mode={mode}
                    onMode={setMode}
                    specialists={roster}
                    selectedId={selectedId}
                    onSelect={pickSpecialist}
                    team={team}
                    onTeam={setTeam}
                    spent={spent}
                    contextWindow={CONTEXT_WINDOW}
                    placeholder="Describe a task"
                  />
                </div>
              </>
            )}
          </div>
        )}
      </main>

      <WorkPanel
        open={panelOpen}
        onClose={() => setPanelOpen(false)}
        tab={panelTab}
        onTab={setPanelTab}
        tasks={panelTasks}
        roster={roster}
        team={panelTeam}
        mode={mode}
        runId={panelRun}
        building={Boolean(cardForRun)}
        buildStatus={cardForRun?.status}
        live={busy || building}
        spent={panelSpent}
        answer={streaming ?? messages.filter((m) => m.role === "assistant").at(-1)?.content ?? ""}
      />
    </div>
  );
}

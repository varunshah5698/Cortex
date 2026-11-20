import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "../components/Icon";
import { Office, roleColor, type AgentStatus, type AgentView, type Zone } from "../components/office/Office";
import { navigate } from "../lib/router";
import { hydrateRoster, isConfigured, loadRoster, type Specialist } from "../lib/providers";
import {
  API,
  SERVICE_BUSY,
  checkBrowser,
  fmtMs,
  fmtNumber,
  loadFile,
  liveUrl,
  previewUrl,
  serviceStatus,
  shareStatus,
  startRun,
  startService,
  startShare,
  stopRun,
  stopService,
  stopShare,
  subscribe,
  type Depth,
  type FileRow,
  type HarnessEvent,
  type PlanUnit,
  type RunMetrics,
  type RunService,
  type RunShare,
} from "../lib/harness";

type Tab = "preview" | "thoughts" | "team" | "runtime";

const PHASE: Record<string, string> = {
  idle: "Lobby",
  planning: "Planning",
  scaffolding: "Scaffolding",
  research: "Web research",
  contracts: "Contract exchange",
  building: "Building",
  reviewing: "Review",
  revising: "Revising",
  synthesizing: "Hand-off",
  done: "Shipped",
  failed: "Failed",
};

const zoneFor = (role: string): Zone => {
  if (role === "planner") return "planning";
  if (role === "research") return "research";
  if (role === "reviewer" || role === "tester") return "review";
  return "coding";
};

type AgentState = {
  id: string;
  name: string;
  role: string;
  model: string;
  status: AgentStatus;
  bubble?: string;
  thought: string;
  file?: string;
  tokens: number;
  latency: number;
  calls: number;
  /** Hydrated from the run's stored ledger — its counters are already final. */
  fromMetrics?: boolean;
};

const seed = (roster: Specialist[]): Record<string, AgentState> =>
  Object.fromEntries(
    roster.map((s) => [
      s.id,
      {
        id: s.id,
        name: s.name,
        role: s.role,
        model: s.model,
        status: "idle" as AgentStatus,
        thought: "",
        tokens: 0,
        latency: 0,
        calls: 0,
      },
    ]),
  );

const base = (path: string) => path.split("/").pop() ?? path;

export default function Build() {
  // Server truth on mount: `has_key`/`configured` live there, so a browser that never
  // opened the workshop still starts a run with the right team — and only that team.
  const [roster, setRoster] = useState<Specialist[]>(() => loadRoster());
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
  const ready = useMemo(() => roster.filter(isConfigured), [roster]);

  const [prompt, setPrompt] = useState("");
  const [depth, setDepth] = useState<Depth>("deep");
  const [runId, setRunId] = useState<string | null>(() => localStorage.getItem("cortex:lastRun"));
  const [running, setRunning] = useState(false);
  const [phase, setPhase] = useState("idle");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("preview");
  const [agents, setAgents] = useState<Record<string, AgentState>>(() => seed(ready));
  const [units, setUnits] = useState<PlanUnit[]>([]);
  const [objective, setObjective] = useState("");
  const [scaffold, setScaffold] = useState<string[]>([]);
  const [sources, setSources] = useState<{ title: string; url: string; query: string }[]>([]);
  const [feed, setFeed] = useState<{ id: number; from: string; to?: string; text: string; kind: string }[]>([]);
  const [live, setLive] = useState<Record<string, string>>({});
  const [files, setFiles] = useState<FileRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [frame, setFrame] = useState(0);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [finishedAt, setFinishedAt] = useState<number | null>(null);
  const [now, setNow] = useState(Date.now());
  const [service, setService] = useState<RunService | null>(null);
  const [browserBusy, setBrowserBusy] = useState(false);
  const [share, setShare] = useState<RunShare | null>(null);
  const [shareBusy, setShareBusy] = useState(false);
  const [tools, setTools] = useState<
    { id: number; tool: string; result: string; ms: number; ok: boolean; phase?: string }[]
  >([]);
  const [logsOpen, setLogsOpen] = useState(false);
  const feedId = useRef(0);
  const toolId = useRef(0);
  const runIdRef = useRef<string | null>(runId);

  const totals = useMemo(() => {
    const list = Object.values(agents);
    return {
      tokens: list.reduce((sum, a) => sum + a.tokens, 0),
      latency: list.reduce((sum, a) => sum + a.latency, 0),
      calls: list.reduce((sum, a) => sum + a.calls, 0),
      thinking: list.reduce((sum, a) => sum + a.thought.length, 0),
    };
  }, [agents]);

  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(timer);
  }, []);

  const patchAgent = useCallback((id: string, patch: Partial<AgentState>) => {
    setAgents((prev) => {
      const current =
        prev[id] ?? { id, name: id, role: "general", model: "", status: "idle" as AgentStatus, thought: "", tokens: 0, latency: 0, calls: 0 };
      return { ...prev, [id]: { ...current, ...patch } };
    });
  }, []);

  useEffect(() => {
    runIdRef.current = runId;
  }, [runId]);

  // The room follows the roster. Hydration lands after the first paint, so a specialist
  // saved in the workshop appears here without a reload — but never over a live run's
  // own status events.
  useEffect(() => {
    if (running) return;
    setAgents((prev) => {
      const next = { ...prev };
      for (const spec of ready) {
        const existing = next[spec.id];
        next[spec.id] = existing
          ? { ...existing, name: spec.name || existing.name, role: spec.role, model: spec.model || existing.model }
          : {
              id: spec.id,
              name: spec.name,
              role: spec.role,
              model: spec.model,
              status: "idle" as AgentStatus,
              thought: "",
              tokens: 0,
              latency: 0,
              calls: 0,
            };
      }
      return next;
    });
  }, [ready, running]);

  // Install, build and serve the workspace — the preview stops being a snapshot.
  // `restart` stops the running server first, so an edited file is rebuilt.
  const boot = useCallback(async (id: string, restart = false, approve = false) => {
    setLogsOpen(true);
    try {
      setService(await startService(id, restart, approve));
    } catch (err) {
      setService(null);
      setNotice(err instanceof Error ? err.message : "The harness did not answer — is it running on :8000?");
    }
  }, []);

  // Tier 4: open the served page in headless Chrome. The report is also emitted on the
  // run's event stream, so this button is for a fresh look, not the only way to see it.
  const checkInBrowser = useCallback(async (id: string) => {
    setBrowserBusy(true);
    try {
      const report = await checkBrowser(id);
      setService((prev) => (prev ? { ...prev, browser: report } : prev));
      setNotice(
        report.skipped
          ? `Browser check skipped · ${report.note ?? "no Chrome"}`
          : report.ok
            ? "Browser check clean — the page rendered and its controls answered"
            : `Browser check found ${report.errors} issue(s) · ${report.issues[0]?.detail ?? "see runtime"}`,
      );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Browser check failed");
    } finally {
      setBrowserBusy(false);
    }
  }, []);

  // A public share is a capability URL, not a second API. The first click is the
  // approval prompt; the second carries the explicit approval.
  const shareRun = useCallback(async (id: string, approve = false) => {
    setShareBusy(true);
    try {
      const next = await startShare(id, approve);
      setShare(next);
      if (next.state === "needs_approval") {
        setNotice(`Publishing this run will run: ${next.command ?? "the tunnel"}. Approve only if you mean it.`);
      } else if (next.url) {
        setNotice(`Public share ready · ${next.url}`);
        void navigator.clipboard?.writeText(next.url).catch(() => {});
      }
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not share this run");
    } finally {
      setShareBusy(false);
    }
  }, []);

  const unshareRun = useCallback(async (id: string) => {
    setShareBusy(true);
    try {
      setShare(await stopShare(id));
      setNotice("Public share stopped");
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not stop the share");
    } finally {
      setShareBusy(false);
    }
  }, []);

  const handle = useCallback(
    (event: HarnessEvent) => {
      switch (event.type) {
        case "start":
          setRunning(true);
          setPhase("planning");
          // Replays carry the run's own timestamps, so an old run shows its real duration.
          // A run that predates timestamps shows no duration rather than a made-up one.
          setStartedAt(event.created ? Date.parse(event.created) || null : null);
          setFinishedAt(null);
          setFeed([]);
          setLive({});
          setScaffold([]);
          setUnits([]);
          setSources([]);
          break;
        case "phase":
          setPhase(event.phase);
          break;
        case "status":
          patchAgent(event.agent, { status: event.status as AgentStatus });
          break;
        case "plan":
          setUnits(event.units);
          if (event.objective) setObjective(event.objective);
          break;
        case "scaffold":
          setScaffold(event.files);
          setNotice(`${event.files.length} empty files planted before any model runs`);
          break;
        case "research":
          setSources((prev) => [...prev, { title: event.title || event.url, url: event.url, query: event.query }].slice(-8));
          if (event.agent) patchAgent(event.agent, { bubble: `📖 ${(event.title || event.url).slice(0, 46)}` });
          break;
        case "usage":
          setAgents((prev) => {
            const agent =
              prev[event.agent] ??
              ({
                id: event.agent,
                name: event.name ?? event.agent,
                role: event.role ?? "general",
                model: event.model ?? "",
                status: "thinking" as AgentStatus,
                thought: "",
                tokens: 0,
                latency: 0,
                calls: 0,
              } as AgentState);
            return {
              ...prev,
              [event.agent]: {
                ...agent,
                name: event.name ?? agent.name,
                role: event.role ?? agent.role,
                model: event.model ?? agent.model,
                // A replayed run may already be hydrated from its ledger; adding the
                // replayed usage events on top would count the same tokens twice.
                tokens: agent.fromMetrics ? agent.tokens : agent.tokens + event.total_tokens,
                latency: agent.fromMetrics ? agent.latency : agent.latency + event.ms,
                calls: agent.fromMetrics ? agent.calls : agent.calls + (event.calls ?? 1),
              },
            };
          });
          break;
        case "message":
          feedId.current += 1;
          setFeed((prev) =>
            [
              ...prev,
              { id: feedId.current, from: event.from, to: event.to, text: event.text, kind: event.kind },
            ].slice(-40),
          );
          if (event.text) patchAgent(event.from, { bubble: event.text.replace(/\s+/g, " ").slice(0, 84) });
          break;
        case "think":
          setAgents((prev) => {
            const agent = prev[event.agent];
            if (!agent) return prev;
            const thought = (agent.thought + event.delta).slice(-1600);
            return { ...prev, [event.agent]: { ...agent, thought } };
          });
          break;
        case "file_start":
          patchAgent(event.agent, { status: "coding", file: event.path, bubble: `✎ ${base(event.path)}` });
          if (event.mode === "rewrite") setLive((prev) => ({ ...prev, [event.path]: "" }));
          break;
        case "file":
          setLive((prev) => ({ ...prev, [event.path]: (prev[event.path] ?? "") + event.delta }));
          break;
        case "file_end":
          patchAgent(event.agent, { bubble: `✓ ${base(event.path)} · ${event.lines}L`, file: undefined });
          setFrame((v) => v + 1);
          setFiles((prev) => {
            const next = prev.filter((f) => f.path !== event.path);
            next.push({ path: event.path, agent: event.agent, lines: event.lines, done: true });
            return next;
          });
          break;
        case "verify":
          setNotice(
            event.ok
              ? `Checks clean · ${event.checked ?? 0} files · ${event.warnings} warning(s)`
              : `Checks found ${event.errors} error(s) · ${event.issues[0]?.detail ?? "see review"}`,
          );
          break;
        case "tool":
          // The transcript's tool history, in the panel that is about what ran and what it
          // cost — a search that returned nothing is a visible fact, not an absence.
          toolId.current += 1;
          setTools((prev) =>
            [
              ...prev,
              {
                id: toolId.current,
                tool: event.tool,
                result: typeof event.result === "string" ? event.result : JSON.stringify(event.result),
                ms: event.ms,
                ok: event.ok,
                phase: event.phase,
              },
            ].slice(-80),
          );
          break;
        case "thrash":
          setNotice(`Loop detected · ${event.message}`);
          break;
        case "fallback":
          setNotice(event.message);
          break;
        case "retry":
          setNotice(`Retrying ${event.agent ?? "provider"} in ${event.delay_ms}ms · attempt ${event.attempt}`);
          break;
        case "budget":
          setError(event.message);
          break;
        case "done":
          setRunning(false);
          setPhase("done");
          setFinishedAt(event.finished ? Date.parse(event.finished) || Date.now() : Date.now());
          setFrame((v) => v + 1);
          // `done` is a verification verdict, not a build report. The payload says which
          // half is which so this line cannot read as "checked and compiled".
          setNotice(
            event.previewed
              ? "Run complete — checks clean and already previewed"
              : "Run complete — verified only: nothing has compiled it yet, the preview does that next",
          );
          if (runIdRef.current) void boot(runIdRef.current);
          break;
        case "error":
          setError(event.message);
          // A failed run is over: without this the room stays "live" forever and the
          // team, the roster sync and the ledger merge all stay frozen.
          setRunning(false);
          setPhase("failed");
          setFinishedAt((value) => value ?? Date.now());
          if (event.agent) patchAgent(event.agent, { status: "error" });
          break;
        case "stream_lost":
          setNotice("Live stream dropped — the run keeps going on the server");
          break;
      }
    },
    [boot, patchAgent],
  );

  useEffect(() => {
    if (!runId) return;
    let source: EventSource | null = null;
    source = subscribe(runId, (event) => {
      handle(event);
      // A finished run's stream ends, and EventSource would otherwise reconnect and
      // replay the whole log forever: close once the terminal event arrives.
      if (event.type === "done" || event.type === "error") source?.close();
    });
    return () => source?.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  // A finished run is loaded from the server ledger, so the room still has its team
  // and the last numbers after a reload or a harness restart.
  useEffect(() => {
    if (!runId || running) return;
    let alive = true;
    fetch(`${API}/api/run/${runId}/metrics`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: RunMetrics | null) => {
        if (!alive || !data?.agents?.length) return;
        setAgents((prev) => {
          const next = { ...prev };
          for (const agent of data.agents) {
            if (next[agent.id]) continue;
            next[agent.id] = {
              id: agent.id,
              name: agent.name,
              role: agent.role,
              model: agent.model,
              status: "done",
              thought: "",
              tokens: agent.total_tokens,
              latency: agent.ms,
              calls: agent.calls,
              fromMetrics: true,
            };
          }
          return next;
        });
        // The event log already decided the phase when it replayed; only a run with
        // no phase events (restored from the ledger alone) needs this hint.
        setPhase((current) => (current === "idle" ? (data.done ? "done" : "building") : current));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [runId, running]);

  // Reattach to a service that is already up (page reload, second tab).
  useEffect(() => {
    if (!runId) return;
    let alive = true;
    serviceStatus(runId)
      .then((next) => {
        if (alive && next.state !== "idle") setService(next);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [runId]);

  // Reattach to a public share on reload; a tunnel outliving the tab is intentional.
  useEffect(() => {
    if (!runId) {
      setShare(null);
      return;
    }
    let alive = true;
    shareStatus(runId)
      .then((next) => {
        if (alive && next.state !== "idle") setShare(next);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [runId]);

  // A local run is reached through the preview proxy with its per-run token; a cloud run
  // already has a public capability URL of its own and is embedded directly.
  const servedUrl = (id: string): string => {
    if (service?.state === "ready" && service.url && (service.remote || service.token)) {
      return service.remote ? service.url : liveUrl(id, service.token ?? "");
    }
    return previewUrl(id);
  };

  // Poll while the service is doing something, and for a short window after ready
  // while the automatic browser check is still in flight. After that, `ready` is final
  // and the Browser button is the way to ask again.
  const serviceState = service?.state;
  const browserPending = serviceState === "ready" && !service?.browser;
  useEffect(() => {
    if (!runId || !serviceState) return;
    if (!SERVICE_BUSY.includes(serviceState) && !browserPending) return;
    let alive = true;
    let attempts = 0;
    const timer = setInterval(() => {
      attempts += 1;
      serviceStatus(runId)
        .then((next) => {
          if (alive) setService(next);
        })
        .catch(() => {});
      if (browserPending && attempts >= 20) clearInterval(timer);
    }, 1200);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [runId, serviceState, browserPending]);

  // A service that just became ready means a fresh document to load.
  useEffect(() => {
    if (serviceState === "ready") setFrame((v) => v + 1);
  }, [serviceState]);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(timer);
  }, [notice]);

  const send = async () => {
    if (!prompt.trim()) return;
    // This page may have loaded before the harness answered; ask once more instead of
    // refusing to build for a specialist that is in fact configured.
    let team = ready;
    if (!team.length) {
      const fresh = await hydrateRoster().catch(() => roster);
      setRoster(fresh);
      team = fresh.filter(isConfigured);
    }
    if (!team.length) {
      setError("No specialist is ready — open the workshop, save a model and key, then come back.");
      return;
    }
    setError(null);
    setRunning(true);
    setAgents(seed(team));
    setStartedAt(Date.now());
    try {
      const id = await startRun(prompt.trim(), team, depth);
      localStorage.setItem("cortex:lastRun", id);
      setService(null);
      setLogsOpen(false);
      setRunId(id);
    } catch (err) {
      setRunning(false);
      setError(err instanceof Error ? err.message : "The harness did not answer. Is it running on :8000?");
    }
  };

  const stop = async () => {
    if (runId) await stopRun(runId);
    setRunning(false);
    setPhase("done");
  };

  const views: AgentView[] = useMemo(
    () =>
      Object.values(agents).map((agent) => ({
        id: agent.id,
        name: agent.name,
        role: agent.role,
        zone: zoneFor(agent.role),
        status: agent.status,
        bubble: agent.bubble,
        thought: agent.thought,
        file: agent.file,
        tokens: agent.tokens,
        latency: agent.latency,
      })),
    [agents],
  );

  const elapsed = startedAt ? (finishedAt ?? now) - startedAt : 0;
  const selectedFile = selected ?? scaffold[0] ?? "";
  const [peek, setPeek] = useState("");

  useEffect(() => {
    if (!runId || !selectedFile) return;
    if (live[selectedFile]) {
      setPeek(live[selectedFile]);
      return;
    }
    let alive = true;
    loadFile(runId, selectedFile).then((text) => alive && setPeek(text));
    return () => {
      alive = false;
    };
  }, [runId, selectedFile, live]);

  return (
    <div className="bld">
      <header className="bld-head">
        <button className="bld-brand" onClick={() => navigate("landing")}>
          <span className="site-mark">
            <Icon name="logo" size={14} />
          </span>
          <span>Cortex</span>
        </button>

        <div className="bld-run">
          <span className={`bld-phase${running ? " is-live" : ""}`}>
            <i />
            {PHASE[phase] ?? phase}
          </span>
          <span className="bld-timer">
            {running || phase === "done" || phase === "failed" ? (startedAt ? fmtMs(elapsed) : "—") : "00:00"}
          </span>
          {runId && <code className="bld-id">{runId}</code>}
        </div>

        <nav className="bld-nav">
          <button onClick={() => navigate("files")}>
            <Icon name="folder" size={14} /> Files
            <i>{scaffold.length || files.length || ""}</i>
          </button>
          <button onClick={() => navigate("metrics")}>
            <Icon name="route" size={14} /> Runtime
          </button>
          <button onClick={() => navigate("workshop")}>
            <Icon name="sliders" size={14} /> Team
          </button>
        </nav>
      </header>

      <div className="bld-body">
        <section className="bld-room">
          <div className="bld-room-bar">
            <span className="bld-objective">
              <Icon name="bulb" size={13} />
              {objective || "Tell the team what to build — they plan, look things up, then write together."}
            </span>
            <span className="bld-room-stats">
              <i>
                <b>{Object.keys(agents).length}</b> specialists
              </i>
              <i>
                <b>{Object.values(agents).filter((a) => ["coding", "writing"].includes(a.status)).length}</b> writing
              </i>
              <i>
                <b>{fmtNumber(totals.tokens)}</b> tokens
              </i>
            </span>
          </div>

          <Office
            agents={views}
            active={selected ?? undefined}
            onPick={(id) => {
              setSelected(id);
              setTab("thoughts");
            }}
          />

          {units.length > 0 && (
            <div className="bld-units">
              {units.map((unit) => {
                const owner = agents[unit.agent];
                const done = files.some((f) => f.path === unit.file && f.done);
                return (
                  <span className={`bld-unit${done ? " is-done" : ""}`} key={unit.id}>
                    <i style={{ ["--tone" as string]: roleColor(unit.role) }} />
                    <b>{unit.file}</b>
                    <em>{owner?.name ?? unit.agent}</em>
                  </span>
                );
              })}
            </div>
          )}
        </section>

        <aside className="bld-panel">
          <div className="bld-tabs">
            {(["preview", "thoughts", "team", "runtime"] as Tab[]).map((key) => (
              <button key={key} className={tab === key ? "is-active" : ""} onClick={() => setTab(key)}>
                {key === "preview" ? "Preview" : key === "thoughts" ? "Reasoning" : key === "team" ? "Team" : "Runtime"}
              </button>
            ))}
          </div>

          <div className="bld-pane">
            {tab === "preview" && (
              <div className="pv">
                {runId ? (
                  <>
                    <div className="pv-bar">
                      <span className={`pv-state is-${service?.state ?? "idle"}`}>
                        {service ? service.state : running ? "writing files" : "not running"}
                      </span>
                      <div>
                        {serviceState === "needs_approval" ? (
                          <button
                            className="pv-btn"
                            onClick={() => void boot(runId, false, true)}
                            title={`Install and build code the team wrote: ${service?.pending?.join(", ") ?? ""}`}
                          >
                            <Icon name="check" size={11} /> Approve &amp; run
                          </button>
                        ) : serviceState && SERVICE_BUSY.includes(serviceState) ? (
                          <button
                            className="pv-btn"
                            onClick={() => void stopService(runId).then(setService).catch(() => {})}
                            title="Stop the server"
                          >
                            <Icon name="stop" size={11} /> Stop
                          </button>
                        ) : (
                          <button
                            className="pv-btn"
                            disabled={running}
                            onClick={() => void boot(runId, serviceState === "ready")}
                            title={
                              running
                                ? "Wait until the team has written every file"
                                : serviceState === "ready"
                                  ? "Stop, then install, build and serve again"
                                  : "Install, build and serve"
                            }
                          >
                            <Icon name="send" size={11} />{" "}
                            {running ? "Writing files…" : serviceState === "ready" ? "Restart" : "Run"}
                          </button>
                        )}
                        <button
                          className="pv-btn"
                          disabled={browserBusy || serviceState !== "ready"}
                          onClick={() => void checkInBrowser(runId)}
                          title="Open the served page in Chrome: console errors, failed requests, one click"
                        >
                          <Icon name="eye" size={11} /> {browserBusy ? "Checking…" : "Browser"}
                        </button>
                        {!service?.remote && (
                          <>
                            {share?.state === "ready" && share.url ? (
                              <button
                                className="pv-btn"
                                disabled={shareBusy}
                                onClick={() => void unshareRun(runId)}
                                title={`Public until ${share.expires_at ?? "stopped"} · ${share.url}`}
                              >
                                <Icon name="link" size={11} /> Stop share
                              </button>
                            ) : share?.state === "needs_approval" ? (
                              <button
                                className="pv-btn"
                                disabled={shareBusy}
                                onClick={() => void shareRun(runId, true)}
                                title="Publishes this one run on a public URL until stopped or the TTL expires"
                              >
                                <Icon name="globe" size={11} /> Approve &amp; share
                              </button>
                            ) : (
                              <button
                                className="pv-btn"
                                disabled={shareBusy || serviceState !== "ready"}
                                onClick={() => void shareRun(runId)}
                                title="Publish this one run through a temporary public tunnel"
                              >
                                <Icon name="globe" size={11} /> {shareBusy ? "Sharing…" : "Share"}
                              </button>
                            )}
                          </>
                        )}
                        <button onClick={() => setFrame((v) => v + 1)} title="Reload">
                          <Icon name="history" size={13} />
                        </button>
                        <a
                          href={servedUrl(runId)}
                          target="_blank"
                          rel="noreferrer"
                          title="Open in a tab"
                        >
                          <Icon name="link" size={13} />
                        </a>
                      </div>
                    </div>
                    {service && (
                      <div className="pv-detail">
                        {service.detail}
                        {service.port ? ` · :${service.port}` : ""}
                        {service.exit_code ? ` · exit ${service.exit_code}` : ""}
                      </div>
                    )}
                    {service?.browser && (
                      <div className="pv-detail">
                        Browser check:{" "}
                        {service.browser.skipped
                          ? `skipped${service.browser.note ? ` · ${service.browser.note}` : ""}`
                          : service.browser.ok
                            ? `clean${service.browser.title ? ` · “${service.browser.title}”` : ""}`
                            : `${service.browser.errors} issue(s) · ${service.browser.issues[0]?.detail ?? "see runtime"}`}
                      </div>
                    )}
                    {!service?.remote && share?.state === "ready" && share.url && (
                      <div className="pv-detail">
                        Public: <a href={share.url} target="_blank" rel="noreferrer">{share.url}</a>
                        {share.expires_at ? ` · expires ${new Date(share.expires_at).toLocaleTimeString()}` : ""}
                      </div>
                    )}
                    {service?.detail_lines && service.detail_lines.length > 0 && (
                      <details className="pv-plan">
                        <summary>
                          {serviceState === "needs_approval" ? "What this approval will run" : "What ran"}
                        </summary>
                        <ul>
                          {service.detail_lines.map((line) => (
                            <li key={line}>{line}</li>
                          ))}
                        </ul>
                        {service.sandbox && <p>{service.sandbox}</p>}
                      </details>
                    )}
                    {/*
                      Sandboxed and cross-origin on purpose: this is model-written code. No
                      `allow-same-origin`, so it has an opaque origin and no storage, and it
                      cannot reach `/api` (that would be cross-origin, and the API refuses a
                      foreign origin). Apps that rely on relative `fetch()` or localStorage
                      will not work here — that is the cost of running generated code.
                    */}
                    <iframe
                      key={frame}
                      title="preview"
                      sandbox="allow-scripts allow-forms allow-popups allow-modals"
                      src={servedUrl(runId)}
                    />
                    {service && (
                      <div className="pv-log">
                        <header>
                          <b>server log</b>
                          <button onClick={() => setLogsOpen((v) => !v)}>
                            {logsOpen ? "hide" : `show · ${service.logs.length}`}
                          </button>
                        </header>
                        {logsOpen && (
                          <pre>{service.logs.slice(-140).join("\n") || "No output yet — Run installs and starts it."}</pre>
                        )}
                      </div>
                    )}
                  </>
                ) : (
                  <div className="pv-empty">
                    <Icon name="eye" size={22} />
                    <p>The page the team writes appears here, live.</p>
                  </div>
                )}
                <div className="pv-files">
                  <b>{scaffold.length || files.length ? `${scaffold.length || files.length} files` : "files"}</b>
                  <button onClick={() => navigate("files")}>
                    Open explorer <Icon name="arrowRight" size={12} />
                  </button>
                </div>
                {sources.length > 0 && (
                  <ul className="pv-sources">
                    {sources.slice(-4).map((source) => (
                      <li key={source.url}>
                        <Icon name="globe" size={11} />
                        <a href={source.url} target="_blank" rel="noreferrer">
                          {source.title.slice(0, 54)}
                        </a>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}

            {tab === "thoughts" && (
              <div className="th">
                <div className="th-feed">
                  {feed.length === 0 && <p className="pane-empty">Conversation between specialists shows up here.</p>}
                  {feed.map((line) => (
                    <div className={`th-msg kind-${line.kind}`} key={line.id}>
                      <span className="th-from" style={{ ["--tone" as string]: roleColor(agents[line.from]?.role ?? "") }}>
                        {agents[line.from]?.name ?? line.from}
                      </span>
                      {line.to && <span className="th-to">→ {line.to === "all" ? "everyone" : agents[line.to]?.name ?? line.to}</span>}
                      <p>{line.text}</p>
                    </div>
                  ))}
                </div>
                <div className="th-think">
                  {Object.values(agents)
                    .filter((agent) => agent.thought)
                    .sort((a, b) => b.thought.length - a.thought.length)
                    .slice(0, 2)
                    .map((agent) => (
                      <article key={agent.id} className="th-card">
                        <header>
                          <i style={{ ["--tone" as string]: roleColor(agent.role) }} />
                          <b>{agent.name}</b>
                          <em>{agent.status}</em>
                        </header>
                        <p>{agent.thought.slice(-620)}</p>
                      </article>
                    ))}
                  {!Object.values(agents).some((a) => a.thought) && (
                    <p className="pane-empty">Every agent writes its reasoning before a single line of code.</p>
                  )}
                </div>
              </div>
            )}

            {tab === "team" && (
              <div className="tm">
                {Object.values(agents).map((agent) => (
                  <button
                    key={agent.id}
                    className={`tm-card${selected === agent.id ? " is-active" : ""}`}
                    onClick={() => setSelected(agent.id)}
                  >
                    <span className="tm-avatar" style={{ ["--tone" as string]: roleColor(agent.role) }}>
                      {agent.name.slice(0, 1).toUpperCase()}
                    </span>
                    <span className="tm-body">
                      <b>{agent.name}</b>
                      <em>{agent.role}</em>
                      <code>{agent.model || "no model"}</code>
                    </span>
                    <span className="tm-stats">
                      <i className={`st-${agent.status}`}>{agent.status}</i>
                      <small>
                        {fmtNumber(agent.tokens)} tok · {fmtMs(agent.latency)}
                      </small>
                    </span>
                  </button>
                ))}
                {Object.keys(agents).length === 0 && <p className="pane-empty">Configure specialists in the workshop.</p>}
              </div>
            )}

            {tab === "runtime" && (
              <div className="rt">
                <div className="rt-kpis">
                  <div>
                    <span>tokens</span>
                    <b>{fmtNumber(totals.tokens)}</b>
                  </div>
                  <div>
                    <span>model time</span>
                    <b>{fmtMs(totals.latency)}</b>
                  </div>
                  <div>
                    <span>calls</span>
                    <b>{totals.calls}</b>
                  </div>
                </div>
                {Object.values(agents).map((agent) => {
                  const share = totals.tokens ? (agent.tokens / totals.tokens) * 100 : 0;
                  return (
                    <div className="rt-row" key={agent.id}>
                      <span className="rt-name">{agent.name}</span>
                      <div className="rt-bar">
                        <i style={{ width: `${share}%`, ["--tone" as string]: roleColor(agent.role) }} />
                      </div>
                      <span className="rt-val">{fmtNumber(agent.tokens)}</span>
                      <span className="rt-ms">{fmtMs(agent.latency)}</span>
                    </div>
                  );
                })}
                {tools.length > 0 && (
                  <div className="rt-tools">
                    <header>
                      <b>tool calls</b>
                      <span>{tools.length}</span>
                    </header>
                    {tools.slice(-16).map((call) => (
                      <div className={`rt-tool${call.ok ? "" : " is-bad"}`} key={call.id}>
                        <i />
                        <b>{call.tool}</b>
                        <em>{call.result || "—"}</em>
                        <small>
                          {call.phase ? `${call.phase} · ` : ""}
                          {call.ms}ms
                        </small>
                      </div>
                    ))}
                  </div>
                )}
                <button className="rt-link" onClick={() => navigate("metrics")}>
                  Open the latency &amp; usage page <Icon name="arrowRight" size={13} />
                </button>
                <div className="rt-peek">
                  <header>
                    <b>{selectedFile || "no file selected"}</b>
                    <span>{peek ? `${peek.split("\n").length} lines` : "—"}</span>
                  </header>
                  <pre>{peek.slice(-1200) || "The file streams in here while it is written."}</pre>
                </div>
              </div>
            )}
          </div>
        </aside>
      </div>

      <footer className="bld-compose">
        <div className="bld-depth">
          {(["quick", "deep"] as Depth[]).map((key) => (
            <button key={key} className={depth === key ? "is-active" : ""} onClick={() => setDepth(key)}>
              {key === "quick" ? "Quick" : "Full crew"}
            </button>
          ))}
        </div>
        <textarea
          value={prompt}
          placeholder="Tell the team what to build…"
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
        />
        {running ? (
          <button className="bld-go is-stop" onClick={stop}>
            <Icon name="stop" size={15} /> Stop the room
          </button>
        ) : (
          <button className="bld-go" onClick={send} disabled={!prompt.trim()}>
            <Icon name="send" size={15} /> Send to work
          </button>
        )}
      </footer>

      {(error || notice) && (
        <div className={`bld-toast${error ? " is-error" : ""}`}>
          <Icon name={error ? "alert" : "check"} size={14} />
          <span>{error ?? notice}</span>
          {error && (
            <button onClick={() => setError(null)}>
              <Icon name="plus" size={12} />
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export const buildReady = (roster: Specialist[]) => roster.filter(isConfigured).length;

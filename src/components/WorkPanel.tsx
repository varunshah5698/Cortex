import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Icon } from "./Icon";
import { roleColor, roleLabel, type Role, type Specialist } from "../lib/providers";
import { Office, type AgentStatus, type AgentView, type Zone } from "./office/Office";
import type { FlockTask } from "../lib/chat";
import {
  SERVICE_BUSY,
  checkBrowser,
  fmtNumber,
  liveUrl,
  loadFile,
  loadFiles,
  loadRuns,
  previewUrl,
  resumeRun,
  serviceStatus,
  shareStatus,
  startService,
  startShare,
  stopService,
  stopShare,
  subscribe,
  type FileRow,
  type HarnessEvent,
  type PlanUnit,
  type RunMetrics,
  type RunService,
  type RunShare,
} from "../lib/harness";

export type PanelTab = "work" | "files" | "preview";

type Props = {
  open: boolean;
  onClose: () => void;
  tab: PanelTab;
  onTab: (tab: PanelTab) => void;
  /** The flock's parts, once the planner has split the request. */
  tasks: FlockTask[];
  /** Every configured specialist: a finished flock's owners must still resolve. */
  roster: Specialist[];
  /** The specialists on this turn, for the idle state. */
  team: Specialist[];
  mode: "solo" | "flock" | "build";
  /** The run this turn belongs to, if it has one. */
  runId: string | null;
  /** True when that run is a build, so the panel names the run rather than a model. */
  building?: boolean;
  /** The build card's status: the harness only serves a run that reached `done`. */
  buildStatus?: "running" | "done" | "error";
  /** True while the turn is still streaming. */
  live: boolean;
  /** Tokens spent this turn. */
  spent: number;
  /** The answer, for the preview tab's fallback when there is no run. */
  answer: string;
};

const zoneFor = (role: string): Zone => {
  if (role === "planner") return "planning";
  if (role === "research") return "research";
  if (role === "reviewer") return "review";
  return "coding";
};

/** A flock part's state, in the vocabulary the office scene draws. */
const officeStatus = (status: FlockTask["status"]): AgentStatus =>
  status === "running" ? "coding" : status === "done" ? "done" : status === "error" ? "error" : "idle";

const statusLabel: Record<FlockTask["status"], string> = {
  pending: "queued",
  running: "working",
  done: "done",
  error: "failed",
};

/** A preview the operator approved once is restored after a harness restart. */
const approvedKey = (runId: string) => `cortex:approved:${runId}`;

/** How long the panel waits for a service to settle before it stops polling. */
const POLL_MS = 2000;

/**
 * The right-hand panel. It replaces the build room: the same things a person actually
 * wants while a turn runs — who is working on what, the files that landed, and the page
 * they add up to — without leaving the conversation.
 *
 * Everything the old room could do lives here too, because dropping the page must not
 * drop the machinery: install/build/serve, the approval gate, the browser check, the
 * public share, and the repair run. Those are the harness's guarantees, and a chat that
 * could not reach them would be a prettier, weaker product.
 */
export function WorkPanel({
  open,
  onClose,
  tab,
  onTab,
  tasks,
  roster,
  team,
  mode,
  runId,
  building = false,
  buildStatus,
  live,
  spent,
  answer,
}: Props) {
  const [files, setFiles] = useState<FileRow[]>([]);
  const [selected, setSelected] = useState("");
  const [content, setContent] = useState("");
  const [frame, setFrame] = useState(0);

  const [service, setService] = useState<RunService | null>(null);
  const [share, setShare] = useState<RunShare | null>(null);
  const [serviceBusy, setServiceBusy] = useState(false);
  const [shareBusy, setShareBusy] = useState(false);
  const [browserBusy, setBrowserBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [fault, setFault] = useState<string | null>(null);
  const [logsOpen, setLogsOpen] = useState(false);
  const [runs, setRuns] = useState<RunMetrics[]>([]);
  // The run's own verdict, for a run opened by URL or from history: the chat's build card
  // only exists for a build started in this thread.
  const [runDone, setRunDone] = useState<boolean | null>(null);
  // The plan and the conversation between specialists: the old room's rail and activity
  // feed, which a build still emits and nothing was reading any more.
  const [plan, setPlan] = useState<PlanUnit[]>([]);
  const [feed, setFeed] = useState<{ id: number; from: string; text: string; kind: string }[]>([]);
  const [phase, setPhase] = useState<string>("");
  const [verdict, setVerdict] = useState<{ ok: boolean; errors: number; warnings: number } | null>(null);
  const [autoApprove, setAutoApprove] = useState(() => localStorage.getItem("cortex:autoApprove") === "1");

  // A late answer from a previous run must not land on the current one.
  const serviceSeq = useRef(0);
  const autoBooted = useRef<string | null>(null);

  // A run's files are the only reason to poll: a chat turn writes none.
  useEffect(() => {
    if (!open || !runId) {
      setFiles([]);
      return;
    }
    let alive = true;
    const pull = async () => {
      const result = await loadFiles(runId);
      if (!alive || !result) return;
      setFiles(result.files);
      setSelected((current) => current || result.files[0]?.path || "");
    };
    void pull();
    const timer = setInterval(pull, live ? 2500 : 8000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [open, runId, live]);

  useEffect(() => {
    if (!runId || !selected) {
      setContent("");
      return;
    }
    let alive = true;
    void loadFile(runId, selected).then((text) => alive && setContent(text));
    return () => {
      alive = false;
    };
  }, [runId, selected, frame]);

  // Switching runs clears everything that belonged to the last one: a toast about run A
  // appearing over run B reads as "this one is broken too".
  useEffect(() => {
    serviceSeq.current += 1;
    autoBooted.current = null;
    setService(null);
    setShare(null);
    setNotice(null);
    setFault(null);
    setLogsOpen(false);
    setSelected("");
    setContent("");
  }, [runId]);

  // Restore what the harness already knows about this run: a preview that is still
  // serving, and a share that is still public, survive a page reload.
  useEffect(() => {
    if (!open || !runId) return;
    let alive = true;
    void serviceStatus(runId)
      .then((next) => alive && setService(next))
      .catch(() => {
        /* no service yet: the Run button is the way to make one */
      });
    void shareStatus(runId)
      .then((next) => alive && setShare(next))
      .catch(() => {
        /* no share yet */
      });
    return () => {
      alive = false;
    };
  }, [open, runId]);

  // Poll only while the service is doing something, or while a fresh `ready` is still
  // waiting on its browser verdict. After that `ready` is final and Run asks again.
  const serviceState = service?.state;
  useEffect(() => {
    if (!runId || !serviceState) return;
    const pending = SERVICE_BUSY.includes(serviceState) || (serviceState === "ready" && !service?.browser);
    if (!pending) return;
    let alive = true;
    const timer = setInterval(() => {
      void serviceStatus(runId)
        .then((next) => alive && setService(next))
        .catch(() => {
          /* a failed poll is not a failed service */
        });
    }, POLL_MS);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [runId, serviceState, service?.browser]);

  const boot = useCallback(
    async (id: string, restart = false, approve = false) => {
      const seq = ++serviceSeq.current;
      setServiceBusy(true);
      setFault(null);
      try {
        const next = await startService(id, restart, approve || autoApprove);
        if (seq !== serviceSeq.current) return;
        setService(next);
        setLogsOpen(true);
        if (next.state === "needs_approval") {
          setNotice("The preview is waiting for your approval — approve it when you are ready.");
        }
      } catch (err) {
        if (seq !== serviceSeq.current) return;
        // Keep the last preview on screen: a failed restart is not a reason to erase the
        // page the person was already looking at.
        setFault(err instanceof Error ? err.message : "Cortex did not answer — is it running on :8000?");
      } finally {
        if (seq === serviceSeq.current) setServiceBusy(false);
      }
    },
    [autoApprove],
  );

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
            : `Browser check found ${report.errors} issue(s) · ${report.issues[0]?.detail ?? "see the log"}`,
      );
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Browser check failed");
    } finally {
      setBrowserBusy(false);
    }
  }, []);

  const shareRun = useCallback(
    async (id: string, approve = false) => {
      setShareBusy(true);
      try {
        const next = await startShare(id, approve || autoApprove);
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
    },
    [autoApprove],
  );

  const unshareRun = useCallback(async (id: string) => {
    setShareBusy(true);
    try {
      setShare(await stopShare(id));
      setNotice("Public share stopped");
    } catch (err) {
      setFault(`Could not stop the share — ${err instanceof Error ? err.message : "Cortex did not answer"}.`);
    } finally {
      setShareBusy(false);
    }
  }, []);

  const toggleAutoApprove = useCallback(() => {
    setAutoApprove((current) => {
      const next = !current;
      localStorage.setItem("cortex:autoApprove", next ? "1" : "0");
      setNotice(
        next
          ? "Auto-approve on — installs, builds and shares will run without asking again."
          : "Auto-approve off — the next install or build will ask first.",
      );
      return next;
    });
  }, []);

  // One button for everything the run is waiting on: the service and the share are
  // separate checkpoints, so approving clears both.
  const approveAll = useCallback(async () => {
    if (!runId) return;
    localStorage.setItem(approvedKey(runId), "1");
    autoBooted.current = runId;
    await boot(runId, false, true);
    if (share?.state === "needs_approval") await shareRun(runId, true);
  }, [boot, runId, share?.state, shareRun]);

  // A finished build serves itself, once. The old room did this on `done`; without it
  // the person had to find and press a button to see the page they just paid for.
  useEffect(() => {
    if (!runId || live || !building) return;
    if (autoBooted.current === runId) return;
    if (localStorage.getItem(approvedKey(runId)) === "1") return;
    autoBooted.current = runId;
    void boot(runId, false, autoApprove);
  }, [runId, live, building, autoApprove, boot]);

  // A build's own stream, read for the three things the panel cannot infer from files:
  // what the plan said it would write, what the team said to each other, and whether the
  // checks passed. `replay=0` asks a finished run for its bookends only, so reopening one
  // does not re-deliver an eight-thousand-event transcript.
  useEffect(() => {
    if (!open || !runId || !building) {
      setPlan([]);
      setFeed([]);
      setPhase("");
      setVerdict(null);
      return;
    }
    let seq = 0;
    const source = subscribe(
      runId,
      (event: HarnessEvent) => {
        switch (event.type) {
          case "plan":
            setPlan(event.units ?? []);
            break;
          case "phase":
            setPhase(event.phase ?? "");
            break;
          case "message":
            setFeed((prev) => [...prev.slice(-40), { id: ++seq, from: event.from, text: event.text, kind: event.kind }]);
            break;
          case "verify":
            setVerdict({ ok: event.ok, errors: event.errors, warnings: event.warnings });
            break;
          case "done":
            setPhase("done");
            break;
          case "error":
            setPhase("failed");
            break;
          default:
            break;
        }
      },
      { replay: false },
    );
    return () => source.close();
  }, [open, runId, building]);

  // Recent runs, so the panel can reopen one without a separate page.
  useEffect(() => {
    if (!open) return;
    void loadRuns().then((list) => {
      setRuns(list);
      if (!runId) return;
      const match = list.find((item) => item.run_id === runId);
      setRunDone(match ? Boolean(match.done) : null);
    });
  }, [open, runId]);

  const done = tasks.filter((t) => t.status === "done").length;
  const failed = tasks.filter((t) => t.status === "error").length;
  const byAgent = useMemo(() => {
    const map = new Map<string, Specialist>();
    roster.forEach((s) => map.set(s.id, s));
    return map;
  }, [roster]);

  // The room, driven by the same state as the board: a part that is running is a
  // specialist typing at their desk, so the picture cannot drift from the list.
  const officeAgents: AgentView[] = useMemo(() => {
    if (tasks.length === 0) {
      return team.map((s) => ({
        id: s.id,
        name: s.name,
        role: s.role,
        zone: zoneFor(s.role),
        status: "idle" as AgentStatus,
        tokens: 0,
        latency: 0,
      }));
    }
    return tasks.map((task) => {
      const owner = byAgent.get(task.agent);
      return {
        id: task.id,
        name: task.agentName || owner?.name || "Specialist",
        role: owner?.role ?? task.role,
        zone: zoneFor(owner?.role ?? task.role),
        status: officeStatus(task.status),
        bubble: task.title,
        tokens: task.chars,
        latency: 0,
      };
    });
  }, [tasks, team, byAgent]);

  const pendingCount = (service?.state === "needs_approval" ? 1 : 0) + (share?.state === "needs_approval" ? 1 : 0);
  const servedUrl = (id: string): string => {
    if (service?.state === "ready" && service.url && (service.remote || service.token)) {
      return service.remote ? service.url : liveUrl(id, service.token ?? "");
    }
    return previewUrl(id);
  };
  const serviceReady = service?.state === "ready";
  // A run that failed or is still writing cannot be served: the harness answers 409 and
  // says so. Disabling the button is the honest version of that rule.
  const finished = buildStatus ? buildStatus === "done" : runDone === true;
  const servable = Boolean(runId) && finished;

  if (!open) return null;

  return (
    <aside className="workpanel">
      <header className="wp-head">
        <div className="wp-tabs">
          {(["work", "files", "preview"] as PanelTab[]).map((key) => (
            <button key={key} className={tab === key ? "is-active" : ""} onClick={() => onTab(key)}>
              {key === "work" ? "Work" : key === "files" ? "Files" : "Preview"}
              {key === "files" && files.length > 0 && <i>{files.length}</i>}
              {key === "preview" && pendingCount > 0 && <i className="is-waiting">{pendingCount}</i>}
            </button>
          ))}
        </div>
        <button className="wp-close" onClick={onClose} title="Hide the panel">
          <Icon name="x" size={14} />
        </button>
      </header>

      <div className="wp-body">
        {tab === "work" && (
          <>
            <section className="wp-card">
              <header>
                <span>
                  {building ? "The build" : tasks.length > 0 || mode === "flock" ? "The flock" : "The specialist"}
                </span>
                <em className={live ? "is-live" : ""}>
                  {live ? "live" : tasks.length ? `${done}/${tasks.length}` : "idle"}
                </em>
              </header>

              {building && (phase || verdict) && (
                <div className="wp-buildline">
                  {phase && <span className={`wp-phase is-${phase}`}>{phase}</span>}
                  {verdict && (
                    <span className={`wp-verdict${verdict.ok ? " is-ok" : " is-bad"}`}>
                      {verdict.ok
                        ? "checks clean"
                        : `${verdict.errors} error${verdict.errors === 1 ? "" : "s"}`}
                      {verdict.warnings > 0 && ` · ${verdict.warnings} warning${verdict.warnings === 1 ? "" : "s"}`}
                    </span>
                  )}
                </div>
              )}

              {officeAgents.length > 0 && (
                <div className="wp-office">
                  <Office agents={officeAgents} />
                </div>
              )}

              {tasks.length > 0 ? (
                <ol className="wp-tasks">
                  {tasks.map((task) => {
                    const owner = byAgent.get(task.agent);
                    return (
                      <li key={task.id} className={`wp-task is-${task.status}`}>
                        <span
                          className="wp-task-dot"
                          style={{ background: roleColor((owner?.role ?? task.role) as Role) }}
                        />
                        <div className="wp-task-main">
                          <b>{task.title}</b>
                          <em>
                            {task.agentName || owner?.name || "unassigned"} ·{" "}
                            {roleLabel((owner?.role ?? (task.role as Role)) as Role)}
                          </em>
                        </div>
                        <span className={`wp-task-state is-${task.status}`}>{statusLabel[task.status]}</span>
                      </li>
                    );
                  })}
                </ol>
              ) : (
                <div className="wp-roster">
                  {team.map((s) => (
                    <div className="wp-member" key={s.id}>
                      <span className="wp-task-dot" style={{ background: roleColor(s.role) }} />
                      <div>
                        <b>{s.name}</b>
                        <em>{roleLabel(s.role)}</em>
                      </div>
                      <small>{s.model || "no model"}</small>
                    </div>
                  ))}
                  {team.length === 0 && <p className="wp-empty">No specialist is configured yet.</p>}
                </div>
              )}

              {building && plan.length > 0 && (
                <div className="wp-plan">
                  <p className="wp-plan-head">
                    Plan <em>{plan.length} file{plan.length === 1 ? "" : "s"}</em>
                  </p>
                  <ul>
                    {plan.map((unit) => {
                      const written = files.some((f) => f.path === unit.file && f.done);
                      return (
                        <li key={unit.id} className={written ? "is-done" : ""}>
                          <span className="wp-plan-dot" style={{ background: roleColor(unit.role as Role) }} />
                          <span className="wp-plan-file">{unit.file}</span>
                          <small>{unit.agent}</small>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}

              {building && feed.length > 0 && (
                <div className="wp-feed">
                  <p className="wp-plan-head">Activity</p>
                  {feed.slice(-6).map((line) => (
                    <p key={line.id} className={`wp-feed-line kind-${line.kind}`}>
                      <b>{line.from}</b>
                      {line.text}
                    </p>
                  ))}
                </div>
              )}

              {tasks.length === 0 && (
                <p className="wp-note">
                  {building
                    ? "The team is planning. Files appear under Files as they land."
                    : mode === "flock"
                      ? "Send a task and the planner splits it across the flock."
                      : "Send a task and this shows the work as it happens."}
                </p>
              )}
            </section>

            <section className="wp-card wp-stats">
              <div>
                <b>{fmtNumber(spent)}</b>
                <em>tokens</em>
              </div>
              <div>
                <b>{tasks.length || team.length}</b>
                <em>{tasks.length ? "parts" : "models"}</em>
              </div>
              <div>
                <b className={failed ? "is-bad" : ""}>{failed}</b>
                <em>failed</em>
              </div>
            </section>

            {runId && (
              <section className="wp-card">
                <header>
                  <span>Run</span>
                  <em>{service?.state ?? "not served"}</em>
                </header>
                <button
                  className="wp-runid"
                  onClick={() => void navigator.clipboard?.writeText(runId)}
                  title="Copy the run id"
                >
                  <Icon name="link" size={11} /> {runId}
                </button>
                {runs.length > 0 && (
                  <select
                    className="wp-runpick"
                    value={runId}
                    onChange={(event) => {
                      const id = event.target.value;
                      if (!id) return;
                      localStorage.setItem("cortex:lastRun", id);
                      window.location.hash = `#/app?run=${id}`;
                      window.location.reload();
                    }}
                    title="Reopen a previous run"
                  >
                    {runs.slice(0, 12).map((run) => (
                      <option key={run.run_id} value={run.run_id}>
                        {run.run_id} · {run.prompt.slice(0, 26)}
                      </option>
                    ))}
                  </select>
                )}
              </section>
            )}
          </>
        )}

        {tab === "files" && (
          <div className="wp-files">
            {!runId && <p className="wp-empty">This turn did not write files. A build turn does.</p>}
            {runId && files.length === 0 && <p className="wp-empty">No files yet.</p>}
            {files.length > 0 && (
              <>
                <ul className="wp-filelist">
                  {files.map((file) => (
                    <li key={file.path}>
                      <button
                        className={selected === file.path ? "is-active" : ""}
                        onClick={() => setSelected(file.path)}
                        title={file.path}
                      >
                        <Icon name="code" size={12} />
                        <span>{file.path}</span>
                        <small>{file.lines}</small>
                      </button>
                    </li>
                  ))}
                </ul>
                <div className="wp-peek">
                  <header>
                    <b>{selected || "no file"}</b>
                    <button onClick={() => setFrame((v) => v + 1)} title="Reload">
                      <Icon name="history" size={12} />
                    </button>
                  </header>
                  <pre>{content || "Select a file."}</pre>
                </div>
              </>
            )}
          </div>
        )}

        {tab === "preview" && (
          <div className="wp-preview">
            {runId ? (
              <>
                <div className="wp-preview-bar">
                  <span className={`wp-state is-${service?.state ?? "idle"}`}>
                    {service ? service.state : live ? "writing files" : "not served"}
                  </span>
                  <div>
                    <button onClick={() => setFrame((v) => v + 1)} title="Reload the frame">
                      <Icon name="history" size={12} />
                    </button>
                    <a href={servedUrl(runId)} target="_blank" rel="noreferrer" title="Open in a tab">
                      <Icon name="link" size={12} />
                    </a>
                  </div>
                </div>

                {notice && <p className="wp-notice">{notice}</p>}
                {fault && <p className="wp-fault">{fault}</p>}

                {service?.state === "needs_approval" && (
                  <div className="wp-approval">
                    <Icon name="alert" size={14} />
                    <div>
                      <b>The preview is waiting for you</b>
                      <span>
                        {service.pending?.join(" · ") ||
                          service.detail_lines?.slice(0, 2).join(" · ") ||
                          "install, build and serve the files the team wrote"}
                      </span>
                    </div>
                    <button onClick={() => void approveAll()} disabled={serviceBusy || shareBusy}>
                      {serviceBusy ? "Approving…" : "Approve"}
                    </button>
                  </div>
                )}

                <div className="wp-actions">
                  <button
                    className="wp-btn is-primary"
                    disabled={!servable || serviceBusy}
                    onClick={() => runId && void boot(runId, serviceReady)}
                    title={
                      !servable
                        ? live || buildStatus === "running" || runDone === false
                          ? "The team is still writing — this unlocks when the run finishes"
                          : "This run did not finish, so there is nothing to serve"
                        : serviceReady
                          ? "Stop, then install, build and serve again"
                          : "Install, build and serve the finished tree"
                    }
                  >
                    <Icon name="send" size={11} />
                    {!servable
                      ? live || buildStatus === "running" || runDone === false
                        ? "Writing…"
                        : "Not finished"
                      : serviceBusy
                        ? "Starting…"
                        : serviceReady
                          ? "Restart"
                          : "Run preview"}
                  </button>

                  {service && SERVICE_BUSY.includes(service.state) && (
                    <button
                      className="wp-btn"
                      onClick={() => {
                        localStorage.removeItem(approvedKey(runId));
                        void stopService(runId)
                          .then(setService)
                          .catch(() => setFault("Could not stop the preview."));
                      }}
                    >
                      Stop
                    </button>
                  )}

                  <button
                    className="wp-btn"
                    disabled={!serviceReady || browserBusy}
                    onClick={() => void checkInBrowser(runId)}
                    title="Open the served page in a real browser and report what it found"
                  >
                    <Icon name="shield" size={11} /> {browserBusy ? "Checking…" : "Browser check"}
                  </button>

                  {service?.browser && !service.browser.ok && !live && (
                    <button
                      className="wp-btn"
                      onClick={() =>
                        void resumeRun(runId)
                          .then(() => setNotice("Repair run started — the authors are fixing the live issues."))
                          .catch(() => setNotice("Could not start the repair run."))
                      }
                      title="Send the browser findings back to the authors and repair them"
                    >
                      <Icon name="check" size={11} /> Repair
                    </button>
                  )}

                  {!service?.remote &&
                    (share?.state === "ready" && share.url ? (
                      <button className="wp-btn" disabled={shareBusy} onClick={() => void unshareRun(runId)}>
                        <Icon name="link" size={11} /> Stop share
                      </button>
                    ) : (
                      <button
                        className="wp-btn"
                        disabled={!serviceReady || shareBusy}
                        onClick={() => void shareRun(runId, share?.state === "needs_approval")}
                        title="Publish this run behind a temporary public URL"
                      >
                        <Icon name="globe" size={11} /> {shareBusy ? "Sharing…" : "Share"}
                      </button>
                    ))}

                  <button
                    className={`wp-auto${autoApprove ? " is-on" : ""}`}
                    onClick={toggleAutoApprove}
                    title="Approve installs, builds and shares without asking again"
                  >
                    <span className="wp-switch" />
                    Auto
                  </button>
                </div>

                {share?.state === "ready" && share.url && (
                  <p className="wp-share">
                    <Icon name="globe" size={11} />
                    <a href={share.url} target="_blank" rel="noreferrer">
                      {share.url}
                    </a>
                    {share.expires_at && <em>until {new Date(share.expires_at).toLocaleTimeString()}</em>}
                  </p>
                )}

                {service?.browser && (
                  <p className={`wp-browser${service.browser.ok ? " is-ok" : " is-bad"}`}>
                    <Icon name={service.browser.ok ? "check" : "alert"} size={11} />
                    {service.browser.skipped
                      ? `skipped · ${service.browser.note ?? "no Chrome"}`
                      : service.browser.ok
                        ? `browser clean · ${service.browser.checked} check${service.browser.checked === 1 ? "" : "s"}`
                        : `browser: ${service.browser.errors} issue${service.browser.errors === 1 ? "" : "s"} · ${
                            service.browser.issues[0]?.detail ?? ""
                          }`}
                  </p>
                )}

                {serviceReady || service?.state === "needs_approval" ? (
                  <iframe
                    key={`${runId}-${frame}-${service?.state}`}
                    className="wp-frame"
                    src={servedUrl(runId)}
                    title="Run preview"
                    sandbox="allow-scripts allow-forms allow-popups allow-modals allow-same-origin"
                  />
                ) : (
                  <div className="wp-answer">
                    <p className="wp-empty">
                      {live || buildStatus === "running" || runDone === false
                        ? "The team is still writing. Run the preview when the run finishes."
                        : buildStatus === "error"
                          ? "This run stopped before it finished, so there is no tree to serve."
                          : "Nothing is served yet. Run the preview to install, build and open this run."}
                    </p>
                    {answer && <pre>{answer.slice(0, 4000)}</pre>}
                  </div>
                )}

                {service && service.logs.length > 0 && (
                  <div className="wp-log">
                    <header>
                      <b>server log</b>
                      <button onClick={() => setLogsOpen((v) => !v)}>
                        {logsOpen ? "hide" : `show · ${service.logs.length}`}
                      </button>
                    </header>
                    {logsOpen && <pre>{service.logs.slice(-40).join("\n")}</pre>}
                  </div>
                )}
              </>
            ) : (
              <div className="wp-answer">
                <p className="wp-empty">
                  No page to preview — this turn answered in the conversation. The answer is below.
                </p>
                <pre>{answer.slice(0, 4000) || "Nothing yet."}</pre>
              </div>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}

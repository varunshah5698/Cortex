import { useEffect, useMemo, useState } from "react";
import { Icon } from "./Icon";
import { roleColor, roleLabel, type Role, type Specialist } from "../lib/providers";
import type { FlockTask } from "../lib/chat";
import { fmtNumber, loadFile, loadFiles, previewUrl, type FileRow } from "../lib/harness";

export type PanelTab = "work" | "files" | "preview";

type Props = {
  open: boolean;
  onClose: () => void;
  tab: PanelTab;
  onTab: (tab: PanelTab) => void;
  /** The flock's parts, once the planner has split the request. */
  tasks: FlockTask[];
  /** The specialists on this turn, for the idle state. */
  team: Specialist[];
  mode: "solo" | "flock";
  /** The run this turn belongs to, if it has one. */
  runId: string | null;
  /** True while the turn is still streaming. */
  live: boolean;
  /** Tokens spent this turn. */
  spent: number;
  /** The answer, for the preview tab's fallback when there is no run. */
  answer: string;
};

const statusLabel: Record<FlockTask["status"], string> = {
  pending: "queued",
  running: "working",
  done: "done",
  error: "failed",
};

/**
 * The right-hand panel. It replaces the build room: the same three things a person
 * actually wants while a turn runs — who is working on what, the files that landed, and
 * the page they add up to — without leaving the conversation.
 */
export function WorkPanel({
  open,
  onClose,
  tab,
  onTab,
  tasks,
  team,
  mode,
  runId,
  live,
  spent,
  answer,
}: Props) {
  const [files, setFiles] = useState<FileRow[]>([]);
  const [selected, setSelected] = useState("");
  const [content, setContent] = useState("");
  const [frame, setFrame] = useState(0);

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

  const done = tasks.filter((t) => t.status === "done").length;
  const failed = tasks.filter((t) => t.status === "error").length;
  const byAgent = useMemo(() => {
    const map = new Map<string, Specialist>();
    team.forEach((s) => map.set(s.id, s));
    return map;
  }, [team]);

  if (!open) return null;

  return (
    <aside className="workpanel">
      <header className="wp-head">
        <div className="wp-tabs">
          {(["work", "files", "preview"] as PanelTab[]).map((key) => (
            <button key={key} className={tab === key ? "is-active" : ""} onClick={() => onTab(key)}>
              {key === "work" ? "Work" : key === "files" ? "Files" : "Preview"}
              {key === "files" && files.length > 0 && <i>{files.length}</i>}
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
                <span>{mode === "flock" ? "The flock" : "The specialist"}</span>
                <em className={live ? "is-live" : ""}>
                  {live ? "live" : tasks.length ? `${done}/${tasks.length}` : "idle"}
                </em>
              </header>

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
                            {roleLabel((owner?.role ?? task.role) as Role)}
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

              {tasks.length === 0 && (
                <p className="wp-note">
                  {mode === "flock"
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
                </header>
                <button
                  className="wp-runid"
                  onClick={() => void navigator.clipboard?.writeText(runId)}
                  title="Copy the run id"
                >
                  <Icon name="link" size={11} /> {runId}
                </button>
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
                  <span>{live ? "the run is still writing" : "served from the run"}</span>
                  <div>
                    <button onClick={() => setFrame((v) => v + 1)} title="Reload the frame">
                      <Icon name="history" size={12} />
                    </button>
                    <a href={previewUrl(runId)} target="_blank" rel="noreferrer" title="Open in a tab">
                      <Icon name="link" size={12} />
                    </a>
                  </div>
                </div>
                <iframe
                  key={`${runId}-${frame}`}
                  className="wp-frame"
                  src={previewUrl(runId)}
                  title="Run preview"
                  sandbox="allow-scripts allow-same-origin allow-forms allow-popups"
                />
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

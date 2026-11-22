import { useEffect, useMemo, useState } from "react";
import { Icon } from "../components/Icon";
import { navigate } from "../lib/router";
import { fmtMs, fmtNumber, loadFile, loadFiles, loadRuns, type FileRow, type RunMetrics } from "../lib/harness";

const lastRun = () => localStorage.getItem("cortex:lastRun") ?? "";

export default function Files() {
  const [runs, setRuns] = useState<RunMetrics[]>([]);
  const [runId, setRunId] = useState(lastRun);
  const [files, setFiles] = useState<FileRow[]>([]);
  const [selected, setSelected] = useState("");
  const [content, setContent] = useState("");
  const [query, setQuery] = useState("");
  const [tick, setTick] = useState(0);

  useEffect(() => {
    loadRuns().then((list) => {
      setRuns(list);
      if (!runId && list[0]) setRunId(list[0].run_id);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!runId) return;
    let alive = true;
    const pull = async () => {
      const result = await loadFiles(runId);
      if (!alive || !result) return;
      setFiles(result.files);
      setSelected((current) => current || result.files[0]?.path || "");
    };
    pull();
    const timer = setInterval(pull, 4000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [runId, tick]);

  useEffect(() => {
    if (!runId || !selected) return;
    let alive = true;
    loadFile(runId, selected).then((text) => alive && setContent(text));
    return () => {
      alive = false;
    };
  }, [runId, selected, tick]);

  const groups = useMemo(() => {
    const map = new Map<string, FileRow[]>();
    files
      .filter((f) => f.path.toLowerCase().includes(query.toLowerCase()))
      .forEach((f) => {
        const dir = f.path.includes("/") ? f.path.slice(0, f.path.lastIndexOf("/")) : "root";
        map.set(dir, [...(map.get(dir) ?? []), f]);
      });
    return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [files, query]);

  const totalLines = files.reduce((sum, f) => sum + (f.lines || 0), 0);
  const current = files.find((f) => f.path === selected);

  return (
    <div className="vault">
      <header className="vault-head">
        <button className="vault-brand" onClick={() => navigate("app")}>
          <span className="site-mark">
            <Icon name="logo" size={14} />
          </span>
          Cortex
        </button>
        <div className="vault-tabs">
          <button className="is-active">
            <Icon name="folder" size={14} /> Files
          </button>
          <button onClick={() => navigate("metrics")}>
            <Icon name="route" size={14} /> Runtime
          </button>
          <button onClick={() => navigate("app")}>
            <Icon name="layers" size={14} /> Chat
          </button>
        </div>
        <div className="vault-actions">
          <select value={runId} onChange={(e) => setRunId(e.target.value)}>
            {runs.map((run) => (
              <option key={run.run_id} value={run.run_id}>
                {run.run_id} · {run.prompt.slice(0, 40)}
              </option>
            ))}
            {!runs.some((r) => r.run_id === runId) && runId && <option value={runId}>{runId}</option>}
          </select>
          <button className="icon-btn" onClick={() => setTick((v) => v + 1)} title="Refresh">
            <Icon name="history" size={15} />
          </button>
        </div>
      </header>

      <div className="vault-body">
        <aside className="vault-tree">
          <div className="vault-tree-head">
            <span>Workspace</span>
            <b>
              {files.length} files · {totalLines} lines
            </b>
          </div>
          <label className="vault-search">
            <Icon name="search" size={13} />
            <input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Filter files" />
          </label>
          {groups.length === 0 && <p className="vault-empty">No files yet. Start a build from the chat and its files land here.</p>}
          {groups.map(([dir, list]) => (
            <div className="vault-group" key={dir}>
              <span className="vault-dir">
                <Icon name="folder" size={12} /> {dir}
              </span>
              {list.map((file) => (
                <button
                  key={file.path}
                  className={`vault-file${file.path === selected ? " is-active" : ""}`}
                  onClick={() => setSelected(file.path)}
                >
                  <span className="vault-file-name">{file.path.split("/").pop()}</span>
                  <span className="vault-file-meta">
                    {file.agent && <i>{file.agent}</i>}
                    {file.lines}L
                  </span>
                </button>
              ))}
            </div>
          ))}
        </aside>

        <main className="vault-view">
          <div className="vault-view-head">
            <span className="vault-path">{selected || "select a file"}</span>
            {current && (
              <span className="vault-badges">
                {current.agent && <i style={{ ["--tone" as string]: "#a08bf0" }}>{current.agent}</i>}
                <i>{current.lines} lines</i>
                <i>{fmtNumber(Math.round(current.lines * 6))} tokens est.</i>
              </span>
            )}
            <div className="vault-view-actions">
              {selected && (
                <button className="ghost-btn" onClick={() => navigator.clipboard?.writeText(selected)}>
                  <Icon name="link" size={13} /> Copy path
                </button>
              )}
              <button className="ghost-btn" onClick={() => navigator.clipboard?.writeText(content)} disabled={!content}>
                <Icon name="download" size={13} /> Copy code
              </button>
              <button
                className="ghost-btn"
                disabled={!content}
                onClick={() => {
                  const blob = new Blob([content], { type: "text/plain;charset=utf-8" });
                  const link = document.createElement("a");
                  link.href = URL.createObjectURL(blob);
                  link.download = selected.split("/").pop() || "file.txt";
                  link.click();
                  URL.revokeObjectURL(link.href);
                }}
              >
                <Icon name="download" size={13} /> Download
              </button>
            </div>
          </div>
          <div className="vault-code">
            {content ? (
              <pre>
                <code>
                  {content.split("\n").map((line, i) => (
                    <span className="vault-line" key={i}>
                      <em>{i + 1}</em>
                      <b>{line || " "}</b>
                    </span>
                  ))}
                </code>
              </pre>
            ) : (
              <p className="vault-empty">Nothing to show yet — the file appears while an agent writes it.</p>
            )}
          </div>
          <footer className="vault-foot">
            <span>{content ? `${content.length} chars` : "—"}</span>
            <span>{runId ? `run ${runId}` : "no run"}</span>
            <span>{current?.done === false ? "writing…" : "saved"}</span>
          </footer>
        </main>
      </div>
    </div>
  );
}

export const filesSummary = (run: RunMetrics) => `${run.files} files · ${fmtMs(run.totals.ms)}`;

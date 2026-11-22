import { useEffect, useMemo, useState } from "react";
import { Icon } from "../components/Icon";
import { navigate } from "../lib/router";
import { fmtMs, fmtNumber, loadRuns, type RunMetrics } from "../lib/harness";

const roleTone: Record<string, string> = {
  planner: "#f2b544",
  research: "#5cc8c8",
  reviewer: "#a08bf0",
  frontend: "#ef7d62",
  backend: "#7fb069",
};
const tone = (role: string) => roleTone[role] ?? "#9aa3b2";

export default function Metrics() {
  const [runs, setRuns] = useState<RunMetrics[]>([]);
  const [selected, setSelected] = useState<string>(() => localStorage.getItem("cortex:lastRun") ?? "");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const pull = () =>
      loadRuns().then((list) => {
        setRuns(list);
        setLoading(false);
        setSelected((current) => current || list[0]?.run_id || "");
      });
    pull();
    const timer = setInterval(pull, 4000);
    return () => clearInterval(timer);
  }, []);

  const run = useMemo(() => runs.find((r) => r.run_id === selected) ?? runs[0], [runs, selected]);
  const agents = run?.agents ?? [];
  const totals = run?.totals;
  const slowest = agents.reduce((max, a) => (a.ms > (max?.ms ?? 0) ? a : max), agents[0]);
  const heaviest = agents.reduce((max, a) => (a.total_tokens > (max?.total_tokens ?? 0) ? a : max), agents[0]);
  const tokenMax = Math.max(1, ...agents.map((a) => a.total_tokens));
  const msMax = Math.max(1, ...agents.map((a) => a.ms));
  const avg = totals && totals.calls ? totals.ms / totals.calls : 0;
  const cost = totals?.cost_usd ?? 0;

  return (
    <div className="mtx">
      <header className="mtx-head">
        <button className="vault-brand" onClick={() => navigate("build")}>
          <span className="site-mark">
            <Icon name="logo" size={14} />
          </span>
          Cortex
        </button>
        <div className="vault-tabs">
          <button onClick={() => navigate("files")}>
            <Icon name="folder" size={14} /> Files
          </button>
          <button className="is-active">
            <Icon name="route" size={14} /> Runtime
          </button>
          <button onClick={() => navigate("build")}>
            <Icon name="layers" size={14} /> Build room
          </button>
        </div>
        <select className="mtx-select" value={selected} onChange={(e) => setSelected(e.target.value)}>
          {runs.map((item) => (
            <option key={item.run_id} value={item.run_id}>
              {item.run_id} · {item.prompt.slice(0, 44)}
            </option>
          ))}
        </select>
      </header>

      <main className="mtx-main">
        <div className="mtx-title">
          <span className="kicker">Runtime</span>
          <h1>Latency, tokens and who did the work.</h1>
          <p>{run ? run.prompt : loading ? "Reading the metrics ledger…" : "No runs recorded yet."}</p>
        </div>

        <div className="mtx-kpis">
          <article>
            <span className="mtx-kpi-label">
              <Icon name="wallet" size={13} /> total tokens
            </span>
            <b>{fmtNumber(totals?.total_tokens ?? 0)}</b>
            <small>
              {fmtNumber(totals?.prompt_tokens ?? 0)} in · {fmtNumber(totals?.completion_tokens ?? 0)} out · $
              {cost.toFixed(4)}
            </small>
          </article>
          <article>
            <span className="mtx-kpi-label">
              <Icon name="history" size={13} /> wall-clock model time
            </span>
            <b>{fmtMs(totals?.ms ?? 0)}</b>
            <small>{fmtMs(avg)} average per call · {totals?.calls ?? 0} calls</small>
          </article>
          <article>
            <span className="mtx-kpi-label">
              <Icon name="cpu" size={13} /> specialists
            </span>
            <b>{agents.length}</b>
            <small>{run?.files ?? 0} files in the workspace</small>
          </article>
          <article>
            <span className="mtx-kpi-label">
              <Icon name="bulb" size={13} /> reasoning chars
            </span>
            <b>{fmtNumber(totals?.thinking_chars ?? 0)}</b>
            <small>streamed before any code was written</small>
          </article>
        </div>

        <section className="mtx-panel">
          <header>
            <h2>Token split</h2>
            <span>prompt vs completion, per specialist</span>
          </header>
          <div className="mtx-bars">
            {agents.map((agent) => {
              const promptShare = agent.total_tokens ? (agent.prompt_tokens / agent.total_tokens) * 100 : 0;
              return (
                <div className="mtx-bar-row" key={agent.id}>
                  <span className="mtx-bar-name">
                    <i style={{ ["--tone" as string]: tone(agent.role) }} />
                    {agent.name}
                  </span>
                  <div className="mtx-bar">
                    <i className="in" style={{ width: `${(agent.total_tokens / tokenMax) * promptShare}%` }} />
                    <i
                      className="out"
                      style={{
                        width: `${(agent.total_tokens / tokenMax) * (100 - promptShare)}%`,
                        ["--tone" as string]: tone(agent.role),
                      }}
                    />
                  </div>
                  <span className="mtx-bar-val">{fmtNumber(agent.total_tokens)}</span>
                </div>
              );
            })}
            {agents.length === 0 && <p className="pane-empty">Nothing recorded for this run yet.</p>}
          </div>
          <footer className="mtx-legend">
            <span>
              <i className="in" /> prompt
            </span>
            <span>
              <i className="out" /> completion
            </span>
          </footer>
        </section>

        <section className="mtx-panel">
          <header>
            <h2>Latency</h2>
            <span>summed model time per specialist</span>
          </header>
          <div className="mtx-bars">
            {agents.map((agent) => (
              <div className="mtx-bar-row" key={agent.id}>
                <span className="mtx-bar-name">
                  <i style={{ ["--tone" as string]: tone(agent.role) }} />
                  {agent.name}
                </span>
                <div className="mtx-bar">
                  <i className="ms" style={{ width: `${(agent.ms / msMax) * 100}%`, ["--tone" as string]: tone(agent.role) }} />
                </div>
                <span className="mtx-bar-val">
                  {fmtMs(agent.ms)} <small>{fmtMs(agent.calls ? agent.ms / agent.calls : 0)}/call</small>
                </span>
              </div>
            ))}
          </div>
        </section>

        <section className="mtx-panel">
          <header>
            <h2>Ledger</h2>
            <span>everything Cortex measured</span>
          </header>
          <div className="mtx-table-wrap">
            <table className="mtx-table">
              <thead>
                <tr>
                  <th>specialist</th>
                  <th>role</th>
                  <th>model</th>
                  <th>calls</th>
                  <th>prompt</th>
                  <th>completion</th>
                  <th>cost</th>
                  <th>time</th>
                </tr>
              </thead>
              <tbody>
                {agents.map((agent) => (
                  <tr key={agent.id}>
                    <td>
                      <i style={{ ["--tone" as string]: tone(agent.role) }} />
                      {agent.name}
                    </td>
                    <td>{agent.role}</td>
                    <td>
                      <code>{agent.model}</code>
                    </td>
                    <td>{agent.calls}</td>
                    <td>{fmtNumber(agent.prompt_tokens)}</td>
                    <td>{fmtNumber(agent.completion_tokens)}</td>
                    <td>${(agent.cost_usd ?? 0).toFixed(4)}</td>
                    <td>{fmtMs(agent.ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <footer className="mtx-foot">
            <span>
              slowest: <b>{slowest?.name ?? "—"}</b>
            </span>
            <span>
              heaviest: <b>{heaviest?.name ?? "—"}</b>
            </span>
            <span>
              state: <b>{run?.done ? "complete" : "in flight"}</b>
            </span>
            <span>
              compiled: <b>{run?.build ? run.build.state : "no"}</b>
            </span>
          </footer>
        </section>
      </main>
    </div>
  );
}

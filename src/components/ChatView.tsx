import { useEffect, useRef, useState } from "react";
import { RichText } from "../lib/markdown";
import type { BuildCard, ChatMessage, ChatPhase, FlockTask, Verdict } from "../lib/chat";
import { BuildCardView } from "./BuildCardView";
import { Icon } from "./Icon";
import { roleColor, type Role, type Specialist } from "../lib/providers";

type Props = {
  messages: ChatMessage[];
  streaming: string | null;
  error: string | null;
  thought: string;
  check: string;
  verdict: Verdict | "";
  phase: ChatPhase;
  /** The flock's parts for the turn in flight, if this is a flock turn. */
  tasks: FlockTask[];
  team: Specialist[];
  synthesizing: boolean;
  onBuild?: (index: number) => void;
  onBuildChange?: (index: number, patch: Partial<BuildCard>) => void;
};

const phaseLabel: Record<ChatPhase, string> = {
  idle: "waiting",
  thinking: "thinking it through",
  check: "questioning its own direction",
  answering: "writing the answer",
};

/** The agent's thinking, as plain text. */
export function Reasoning({
  thought,
  check,
  verdict,
  phase,
  live,
}: {
  thought: string;
  check: string;
  verdict: Verdict | "";
  phase?: ChatPhase;
  live: boolean;
}) {
  const [open, setOpen] = useState(live);
  const bodyRef = useRef<HTMLDivElement>(null);
  const adjusted = verdict === "adjust";

  useEffect(() => {
    if (!live || !open) return;
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [thought, live, open]);

  if (!thought && !check) return null;

  return (
    <div className={`reason${live ? " is-live" : ""}`}>
      <button className="reason-head" type="button" onClick={() => setOpen((v) => !v)}>
        <span className="reason-dot" />
        <b>Reasoning</b>
        {phase && <em>{phaseLabel[phase]}</em>}
        <span className="reason-toggle">{open ? "Hide" : "Show"}</span>
      </button>

      {open && (
        <div className="reason-body" ref={bodyRef}>
          {thought && (
            <p className="reason-thought">{thought.replace(/^#{1,6}\s+/gm, "").replace(/\*\*/g, "")}</p>
          )}
          {live && !check && (
            <span className="typing">
              <i />
              <i />
              <i />
            </span>
          )}
          {check && (
            <div className={`reason-check${adjusted ? " is-adjust" : ""}`}>
              <span className="reason-check-label">
                Self-check · {adjusted ? "changed course" : "on track"}
              </span>
              <p>{check}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/** The flock's parts, shown inline so the split is visible while it happens. */
function FlockBoard({ tasks, team }: { tasks: FlockTask[]; team: Specialist[] }) {
  const owner = (id: string) => team.find((s) => s.id === id);
  const done = tasks.filter((t) => t.status === "done").length;

  return (
    <div className="flockboard">
      <div className="flockboard-head">
        <Icon name="flock" size={13} />
        <b>The flock</b>
        <em>
          {done}/{tasks.length} parts
        </em>
      </div>
      <ul>
        {tasks.map((task) => {
          const spec = owner(task.agent);
          return (
            <li key={task.id} className={`is-${task.status}`}>
              <span className="flock-dot" style={{ background: roleColor((spec?.role ?? task.role) as Role) }} />
              <span className="flock-title">{task.title}</span>
              <span className="flock-who">{task.agentName || spec?.name || "—"}</span>
              <span className="flock-state">
                {task.status === "done" ? (
                  <Icon name="check2" size={11} />
                ) : task.status === "error" ? (
                  <Icon name="x" size={11} />
                ) : (
                  <i className="flock-pulse" />
                )}
              </span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function ChatView({
  messages,
  streaming,
  error,
  thought,
  check,
  verdict,
  phase,
  tasks,
  team,
  synthesizing,
  onBuild,
  onBuildChange,
}: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, streaming, thought, check, tasks.length]);

  return (
    <div className="chat">
      <div className="chat-inner">
        {messages.map((m, i) =>
          m.role === "user" ? (
            <div className="msg-user" key={i}>
              {m.content}
            </div>
          ) : (
            <div className="msg-ai" key={i}>
              <span className="ai-dot" />
              <div className="msg-body">
                {m.flock && m.flock.length > 0 && <FlockBoard tasks={m.flock} team={team} />}
                {m.reasoning && (
                  <Reasoning thought={m.reasoning} check={m.check ?? ""} verdict={m.verdict ?? ""} live={false} />
                )}
                <RichText text={m.content} />
                {m.build ? (
                  <BuildCardView card={m.build} onChange={(patch) => onBuildChange?.(i, patch)} />
                ) : (
                  <div className="msg-actions">
                    <button className="build-this" type="button" onClick={() => onBuild?.(i)}>
                      <Icon name="layers" size={13} />
                      Build this
                    </button>
                  </div>
                )}
              </div>
            </div>
          ),
        )}

        {streaming !== null && (
          <div className="msg-ai">
            <span className="ai-dot is-live" />
            <div className="msg-body">
              {tasks.length > 0 && <FlockBoard tasks={tasks} team={team} />}
              <Reasoning thought={thought} check={check} verdict={verdict} phase={phase} live />
              {streaming ? (
                <RichText text={streaming} />
              ) : synthesizing ? (
                <p className="synth-note">
                  <i className="flock-pulse" />
                  Composing one answer from the parts…
                </p>
              ) : phase === "answering" ? (
                <span className="typing">
                  <i />
                  <i />
                  <i />
                </span>
              ) : null}
            </div>
          </div>
        )}

        {error && (
          <div className="msg-error">
            <strong>Could not reach the model.</strong> {error}
          </div>
        )}
        <div ref={endRef} />
      </div>
    </div>
  );
}

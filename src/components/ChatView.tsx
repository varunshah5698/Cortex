import { useEffect, useRef, useState } from "react";
import { RichText } from "../lib/markdown";
import type { BuildCard, ChatMessage, ChatPhase, Verdict } from "../lib/chat";
import { BuildCardView } from "./BuildCardView";

type Props = {
  messages: ChatMessage[];
  streaming: string | null;
  error: string | null;
  thought: string;
  check: string;
  verdict: Verdict | "";
  phase: ChatPhase;
  onBuild?: (index: number) => void;
  onBuildChange?: (index: number, patch: Partial<BuildCard>) => void;
};

const phaseLabel: Record<ChatPhase, string> = {
  idle: "waiting",
  thinking: "thinking it through",
  check: "questioning its own direction",
  answering: "writing the answer",
};

/** The agent's thinking, as plain text — the same card the build room shows. */
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

  // Follow the newest thought while the agent is still deliberating.
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
          {/* runs recorded before reasoning was plain text keep their markers */}
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

export function ChatView({
  messages,
  streaming,
  error,
  thought,
  check,
  verdict,
  phase,
  onBuild,
  onBuildChange,
}: Props) {
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ block: "end" });
  }, [messages.length, streaming, thought, check]);

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
                {m.reasoning && (
                  <Reasoning
                    thought={m.reasoning}
                    check={m.check ?? ""}
                    verdict={m.verdict ?? ""}
                    live={false}
                  />
                )}
                <RichText text={m.content} />
                {m.build ? (
                  <BuildCardView card={m.build} onChange={(patch) => onBuildChange?.(i, patch)} />
                ) : (
                  <div className="msg-actions">
                    <button className="build-this" type="button" onClick={() => onBuild?.(i)}>
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
              <Reasoning thought={thought} check={check} verdict={verdict} phase={phase} live />
              {streaming ? (
                <RichText text={streaming} />
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

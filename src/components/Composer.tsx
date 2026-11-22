import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import { REASONING, REASONING_ORDER, type ReasoningDepth } from "../lib/chat";

/** 200_000 reads "200K", 1_000_000 reads "1M": a window is not a decimal. */
function compact(n: number) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n % 1_000_000 ? 1 : 0)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}K`;
  return `${n}`;
}
import { roleColor, roleLabel, type Specialist } from "../lib/providers";

/** How the composer spends a turn. `flock` is the multi-model one; `build` writes files. */
export type Mode = "solo" | "flock" | "build";

export type ComposerProps = {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  busy: boolean;
  depth: ReasoningDepth;
  onDepth: (v: ReasoningDepth) => void;
  mode: Mode;
  onMode: (m: Mode) => void;
  specialists: Specialist[];
  selectedId: string;
  onSelect: (id: string) => void;
  /** The team a flock will use, in order. */
  team: string[];
  onTeam: (ids: string[]) => void;
  /** Tokens spent so far this turn, for the live counter. */
  spent: number;
  /** The context window the harness is working in, for the ratio. */
  contextWindow: number;
  placeholder?: string;
};

export function Composer({
  value,
  onChange,
  onSend,
  onStop,
  busy,
  depth,
  onDepth,
  mode,
  onMode,
  specialists,
  selectedId,
  onSelect,
  team,
  onTeam,
  spent,
  contextWindow,
  placeholder,
}: ComposerProps) {
  const budget = REASONING[depth];
  const ref = useRef<HTMLTextAreaElement>(null);
  const [picker, setPicker] = useState<"none" | "model" | "team">("none");
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(Math.max(el.scrollHeight, 26), 220)}px`;
  }, [value]);

  useEffect(() => {
    if (picker === "none") return;
    const close = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setPicker("none");
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [picker]);

  const active = specialists.find((s) => s.id === selectedId) ?? specialists[0];
  const teamMembers = team
    .map((id) => specialists.find((s) => s.id === id))
    .filter((s): s is Specialist => Boolean(s));
  const ratio = contextWindow > 0 ? Math.min(1, spent / contextWindow) : 0;

  const toggleMember = (id: string) => {
    onTeam(team.includes(id) ? team.filter((x) => x !== id) : [...team, id]);
  };

  return (
    <div className="composer" ref={wrapRef}>
      <textarea
        ref={ref}
        value={value}
        rows={1}
        placeholder={placeholder ?? "Describe a task"}
        spellCheck={false}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            onSend();
          }
        }}
      />

      <div className="composer-tools">
        <div className="ct-left">
          {/* Mode: one model, a flock, or a real build. The third one writes files and
              scaffolds the tree; the first two only ever answer in prose. Without it the
              only way to reach the build pipeline was a suggestion card, so "build me a
              portfolio" typed into this box came back as an essay and no files. */}
          <div className="mode-switch" role="group" aria-label="How to answer">
            <button
              type="button"
              className={mode === "solo" ? "is-on" : ""}
              onClick={() => onMode("solo")}
              title="One specialist answers"
            >
              Solo
            </button>
            <button
              type="button"
              className={mode === "flock" ? "is-on" : ""}
              onClick={() => onMode("flock")}
              title="Split the request across several specialists, then compose one answer"
            >
              <Icon name="flock" size={13} />
              Flock
            </button>
            <button
              type="button"
              className={mode === "build" ? "is-on" : ""}
              onClick={() => onMode("build")}
              title="Plan, scaffold and write real files, then serve a preview"
            >
              <Icon name="code" size={13} />
              Build
            </button>
          </div>

          <div className="picker-wrap">
            <button
              type="button"
              className={`chip-btn${picker === "model" ? " is-open" : ""}`}
              onClick={() => setPicker(picker === "model" ? "none" : "model")}
              title={mode === "solo" ? "Which specialist answers" : "The lead specialist"}
            >
              <span className="chip-dot" style={{ background: roleColor(active?.role ?? "general") }} />
              <span className="chip-label">{active?.name ?? "No specialist"}</span>
              <Icon name="chevronDown" size={12} />
            </button>

            {picker === "model" && (
              <div className="picker">
                <p className="picker-head">{mode === "solo" ? "Answer with" : "Lead"}</p>
                {specialists.map((s) => (
                  <button
                    key={s.id}
                    className={`picker-row${s.id === selectedId ? " is-active" : ""}`}
                    onClick={() => {
                      onSelect(s.id);
                      setPicker("none");
                    }}
                  >
                    <span className="chip-dot" style={{ background: roleColor(s.role) }} />
                    <span className="picker-main">
                      <b>{s.name}</b>
                      <em>{s.model || "no model"}</em>
                    </span>
                    <span className="picker-role">{roleLabel(s.role)}</span>
                  </button>
                ))}
                {specialists.length === 0 && <p className="picker-empty">No specialists yet.</p>}
              </div>
            )}
          </div>

          {mode === "flock" && (
            <div className="picker-wrap">
              <button
                type="button"
                className={`chip-btn${picker === "team" ? " is-open" : ""}`}
                onClick={() => setPicker(picker === "team" ? "none" : "team")}
                title="Which specialists join the flock"
              >
                <Icon name="flock" size={13} />
                <span className="chip-label">
                  {teamMembers.length ? `${teamMembers.length} in the flock` : "Pick a team"}
                </span>
                <Icon name="chevronDown" size={12} />
              </button>

              {picker === "team" && (
                <div className="picker">
                  <p className="picker-head">The flock</p>
                  {specialists.map((s) => {
                    const on = team.includes(s.id);
                    return (
                      <button
                        key={s.id}
                        className={`picker-row${on ? " is-active" : ""}`}
                        onClick={() => toggleMember(s.id)}
                      >
                        <span className={`picker-check${on ? " is-on" : ""}`}>
                          {on && <Icon name="check2" size={11} />}
                        </span>
                        <span className="picker-main">
                          <b>{s.name}</b>
                          <em>{s.model || "no model"}</em>
                        </span>
                        <span className="picker-role">{roleLabel(s.role)}</span>
                      </button>
                    );
                  })}
                  <p className="picker-note">
                    {teamMembers.length < 2
                      ? "A flock needs at least two specialists with a model."
                      : "Each part goes to the specialist whose role fits it."}
                  </p>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="ct-right">
          <div className="budget" role="group" aria-label="Reasoning budget">
            <Icon name="sparkles" size={13} />
            {REASONING_ORDER.map((key) => (
              <button
                key={key}
                type="button"
                className={`budget-chip${depth === key ? " is-on" : ""}`}
                onClick={() => onDepth(key)}
                title={REASONING[key].hint}
              >
                {REASONING[key].label}
              </button>
            ))}
          </div>

          <span className="ctx-meter" title={`${spent.toLocaleString()} of ${contextWindow.toLocaleString()} tokens`}>
            <i style={{ width: `${ratio * 100}%` }} />
            <em>
              {compact(spent)} / {compact(contextWindow)}
            </em>
          </span>

          {busy ? (
            <button className="orb-btn is-stop" onClick={onStop} title="Stop">
              <Icon name="stop" size={14} />
            </button>
          ) : (
            <button
              className={`orb-btn${value.trim() ? " is-ready" : ""}`}
              onClick={onSend}
              title={value.trim() ? "Send" : "Type a task first"}
            >
              <Icon name="send" size={16} />
            </button>
          )}
        </div>
      </div>

      <div className="composer-foot">
        <span className="foot-hint">
          {mode === "flock"
            ? "Enter splits the task · the flock answers in parallel"
            : mode === "build"
              ? "Enter builds it · plans, scaffolds and writes real files"
              : "Enter sends · Shift+Enter adds a line"}
        </span>
        <span className="foot-est">
          {budget.calls} call{budget.calls === 1 ? "" : "s"} · ~{budget.seconds}s
        </span>
      </div>
    </div>
  );
}

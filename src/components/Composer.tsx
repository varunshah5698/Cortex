import { useLayoutEffect, useRef } from "react";
import { REASONING, REASONING_ORDER, type ReasoningDepth } from "../lib/chat";
import { Icon } from "./Icon";

type Props = {
  value: string;
  onChange: (v: string) => void;
  onSend: () => void;
  onStop: () => void;
  busy: boolean;
  depth: ReasoningDepth;
  onDepth: (v: ReasoningDepth) => void;
};

export function Composer({ value, onChange, onSend, onStop, busy, depth, onDepth }: Props) {
  const budget = REASONING[depth];
  const ref = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(Math.max(el.scrollHeight, 26), 200)}px`;
  }, [value]);

  return (
    <div className="composer">
      <textarea
        ref={ref}
        value={value}
        rows={1}
        placeholder="Ask me anything..."
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
        <div className="budget" role="group" aria-label="Reasoning budget">
          <Icon name="sparkles" size={14} />
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
        <button className="ghost-icon" title="Add an image">
          <Icon name="image" size={16} />
        </button>
        <button className="ghost-icon" title="Think step by step">
          <Icon name="bulb" size={16} />
        </button>

        <div className="composer-right">
          <button className="ghost-icon" title="Settings">
            <Icon name="sliders" size={17} />
          </button>
          <button className="ghost-icon" title="Search the web">
            <Icon name="globe" size={17} />
          </button>
          <span className="send-estimate" title={budget.hint}>
            {budget.calls} call{budget.calls === 1 ? "" : "s"} · ~{budget.seconds}s
          </span>
          {busy ? (
            <button className="orb-btn" onClick={onStop} title="Stop">
              <Icon name="stop" size={15} />
            </button>
          ) : (
            <button
              className={`orb-btn${value.trim() ? " is-ready" : ""}`}
              onClick={onSend}
              title={value.trim() ? "Send" : "Voice input"}
            >
              <Icon name={value.trim() ? "send" : "mic"} size={value.trim() ? 17 : 16} />
            </button>
          )}
        </div>
      </div>

      <div className="composer-foot">
        <span className="saved-prompts">
          <Icon name="sparkles" size={15} />
          <span>Saved prompts</span>
        </span>
        <button className="mini-btn">
          <Icon name="attach" size={13} />
          <span>Attach file</span>
        </button>
      </div>
    </div>
  );
}

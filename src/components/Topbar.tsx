import { useEffect, useRef, useState } from "react";
import { Icon } from "./Icon";
import { roleLabel, type Specialist } from "../lib/providers";

type Props = {
  specialists: Specialist[];
  selectedId: string;
  onSelect: (id: string) => void;
  onExport: () => void;
  onSetup: () => void;
  onBuild: () => void;
};

export function Topbar({ specialists, selectedId, onSelect, onExport, onSetup, onBuild }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const active = specialists.find((s) => s.id === selectedId) ?? specialists[0];

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <header className="topbar">
      <div className="model-wrap" ref={ref}>
        <button className="model-chip" onClick={() => setOpen((v) => !v)}>
          <span className="model-mark">
            <Icon name="logo" size={12} />
          </span>
          <span className="model-name">{active?.name ?? "Cortex"}</span>
          <span className="model-role">{active ? roleLabel(active.role) : "Generalist"}</span>
          <Icon name="chevronDown" size={13} className="chev" />
        </button>

        {open && (
          <div className="model-menu">
            <p className="model-menu-head">Command chain</p>
            {specialists.map((s) => (
              <button
                key={s.id}
                className={`model-item${s.id === active?.id ? " is-active" : ""}`}
                onClick={() => {
                  onSelect(s.id);
                  setOpen(false);
                }}
              >
                <span className="model-item-main">
                  <b>{s.name}</b>
                  <em>{s.model}</em>
                </span>
                <span className="model-item-role">{roleLabel(s.role)}</span>
              </button>
            ))}
            <button
              className="model-menu-foot"
              onClick={() => {
                setOpen(false);
                onSetup();
              }}
            >
              <Icon name="sliders" size={14} />
              Configure specialists
            </button>
            <button
              className="model-menu-foot"
              onClick={() => {
                setOpen(false);
                onBuild();
              }}
            >
              <Icon name="layers" size={14} />
              Open the build room
            </button>
          </div>
        )}
      </div>

      <div className="topbar-actions">
        <button className="icon-btn more" title="More">
          <Icon name="more" size={18} />
        </button>
        <button className="round-btn" title="Share link">
          <Icon name="link" size={16} />
        </button>
        <button className="outline-btn" onClick={onExport}>
          <Icon name="download" size={15} />
          <span>Export chat</span>
        </button>
        <button className="solid-btn">Upgrade</button>
      </div>
    </header>
  );
}

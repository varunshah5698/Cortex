import { Icon } from "./Icon";
import { roleColor, roleLabel, type Specialist } from "../lib/providers";

type Props = {
  specialists: Specialist[];
  selectedId: string;
  /** Which surface the person is on. The chat is the default; the rest are settings. */
  view: "chat" | "account";
  onView: (view: "chat" | "account") => void;
  panelOpen: boolean;
  onPanel: () => void;
  onExport: () => void;
  onSetup: () => void;
};

export function Topbar({ specialists, selectedId, view, onView, panelOpen, onPanel, onExport, onSetup }: Props) {
  const active = specialists.find((s) => s.id === selectedId) ?? specialists[0];

  return (
    <header className="topbar">
      <div className="topbar-tabs">
        <button className={view === "chat" ? "is-active" : ""} onClick={() => onView("chat")}>
          Chat
        </button>
        <button className={view === "account" ? "is-active" : ""} onClick={() => onView("account")}>
          Account
        </button>
      </div>

      <div className="topbar-actions">
        {active && (
          <span className="topbar-model" title={`${active.name} · ${active.model}`}>
            <span className="chip-dot" style={{ background: roleColor(active.role) }} />
            {active.name}
            <em>{roleLabel(active.role)}</em>
          </span>
        )}
        <button className="icon-btn" onClick={onSetup} title="Configure specialists">
          <Icon name="sliders" size={16} />
        </button>
        <button className="icon-btn" onClick={onExport} title="Export this chat">
          <Icon name="download" size={16} />
        </button>
        <button
          className={`panel-btn${panelOpen ? " is-on" : ""}`}
          onClick={onPanel}
          title={panelOpen ? "Hide the work panel" : "Show the work panel"}
        >
          <Icon name={panelOpen ? "panelOff" : "panel"} size={16} />
        </button>
      </div>
    </header>
  );
}

import type { RefObject } from "react";
import { Icon } from "./Icon";
import { seedPrompts, type Thread } from "../lib/threads";
import { logout, useSession } from "../lib/auth";
import { navigate } from "../lib/router";

type Props = {
  collapsed: boolean;
  onToggle: () => void;
  threads: Thread[];
  activeId: string | null;
  query: string;
  onQuery: (q: string) => void;
  searchRef: RefObject<HTMLInputElement>;
  onNewChat: () => void;
  onOpenThread: (id: string) => void;
  onUsePrompt: (text: string) => void;
  /** How many specialists are configured, for the footer counter. */
  agents: number;
};

const nav = [
  { icon: "plus" as const, label: "New chat", action: "new" },
  { icon: "layers" as const, label: "Artifacts", action: "artifacts" },
  { icon: "clock" as const, label: "Routines", action: "routines" },
];

export function Sidebar({
  collapsed,
  onToggle,
  threads,
  activeId,
  query,
  onQuery,
  searchRef,
  onNewChat,
  onOpenThread,
  onUsePrompt,
  agents,
}: Props) {
  const { user, setUser } = useSession();
  const q = query.trim().toLowerCase();
  const matches = (s: string) => !q || s.toLowerCase().includes(q);
  const visible = threads.filter((t) => matches(t.title));

  return (
    <aside className={`sidebar${collapsed ? " is-collapsed" : ""}`}>
      <div className="sidebar-head">
        <div className="brand">
          <span className="brand-mark">
            <Icon name="logo" size={15} />
          </span>
          <span className="brand-name">Cortex</span>
        </div>
        <button className="icon-btn" onClick={onToggle} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
          <Icon name={collapsed ? "expand" : "collapse"} size={16} />
        </button>
      </div>

      <nav className="nav">
        {nav.map((item) => (
          <button
            className="nav-row"
            key={item.label}
            onClick={() => {
              if (item.action === "new") onNewChat();
              if (item.action === "artifacts") navigate("files");
              if (item.action === "routines") navigate("metrics");
            }}
          >
            <Icon name={item.icon} size={16} />
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <label className="search">
        <Icon name="search" size={13} />
        <input
          ref={searchRef}
          value={query}
          onChange={(e) => onQuery(e.target.value)}
          placeholder="Search"
          spellCheck={false}
        />
        <span className="kbd">
          <span className="kbd-cmd">⌘</span>K
        </span>
      </label>

      <div className="sidebar-body">
        {visible.length > 0 && (
          <section className="group">
            <h3>
              <span className="group-mark" />
              Chats
            </h3>
            {visible.map((t) => (
              <button
                key={t.id}
                className={`thread${t.id === activeId ? " is-active" : ""}`}
                onClick={() => onOpenThread(t.id)}
                title={t.title}
              >
                <span className="thread-title">{t.title}</span>
              </button>
            ))}
          </section>
        )}

        {seedPrompts.map((group) => {
          const items = group.items.filter(matches);
          if (!items.length) return null;
          return (
            <section className="group" key={group.group}>
              <h3>{group.group}</h3>
              {items.map((item) => (
                <button key={item} className="thread" onClick={() => onUsePrompt(item)} title={item}>
                  <span className="thread-title">{item}</span>
                </button>
              ))}
            </section>
          );
        })}
      </div>

      <div className="sidebar-foot">
        <button className="agents-row" onClick={() => navigate("workshop")} title="Configure the specialists">
          <span className="agents-dots">
            <i />
            <i />
            <i />
          </span>
          <span>
            {agents} agent{agents === 1 ? "" : "s"}
          </span>
          <Icon name="arrowRight" size={13} />
        </button>

        <div className="user-card">
          <img className="avatar" src="/avatar.jpg" alt="" />
          <div className="user-meta">
            <span className="user-name">{user?.name ?? "Guest"}</span>
            <span className="user-mail">{user?.email ?? "Not signed in"}</span>
          </div>
          <button
            className="icon-btn logout"
            title="Sign out"
            onClick={() => {
              void logout().finally(() => {
                setUser(null);
                navigate("login");
              });
            }}
          >
            <Icon name="logout" size={16} />
          </button>
        </div>
      </div>
    </aside>
  );
}

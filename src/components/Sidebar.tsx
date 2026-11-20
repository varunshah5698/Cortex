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
};

const nav = [
  { icon: "globe" as const, label: "Explore" },
  { icon: "library" as const, label: "Library" },
  { icon: "folder" as const, label: "Files" },
  { icon: "history" as const, label: "History" },
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
}: Props) {
  const { user, setUser } = useSession();
  const q = query.trim().toLowerCase();
  const matches = (s: string) => !q || s.toLowerCase().includes(q);

  return (
    <aside className={`sidebar${collapsed ? " is-collapsed" : ""}`}>
      <div className="sidebar-head">
        <div className="brand">
          <span className="brand-mark">
            <Icon name="logo" size={16} />
          </span>
          <span className="brand-name">Cortex</span>
        </div>
        <button className="icon-btn" onClick={onToggle} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
          <Icon name={collapsed ? "expand" : "collapse"} size={17} />
        </button>
      </div>

      <button className="new-chat" onClick={onNewChat}>
        <Icon name="plus" size={16} />
        <span>New chat</span>
      </button>

      <label className="search">
        <Icon name="search" size={14} />
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

      <nav className="nav">
        {nav.map((item) => (
          <button className="nav-row" key={item.label}>
            <Icon name={item.icon} size={17} />
            <span>{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-body">
        {threads.filter((t) => matches(t.title)).length > 0 && (
          <section className="group">
            <h3>Today</h3>
            {threads
              .filter((t) => matches(t.title))
              .map((t) => (
                <button
                  key={t.id}
                  className={`thread${t.id === activeId ? " is-active" : ""}`}
                  onClick={() => onOpenThread(t.id)}
                  title={t.title}
                >
                  {t.title}
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
                  {item}
                </button>
              ))}
            </section>
          );
        })}
      </div>

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
          <Icon name="logout" size={17} />
        </button>
      </div>
    </aside>
  );
}

import { useEffect, useRef, useState } from "react";
import { Icon } from "../components/Icon";
import { navigate } from "../lib/router";
import {
  defaultRoster,
  hydrateRoster,
  isConfigured,
  loadRoster,
  newSpecialist,
  providers,
  pushRoster,
  roles,
  roleLabel,
  saveRoster,
  saveSelected,
  type ProviderKind,
  type Role,
  type Specialist,
} from "../lib/providers";
import { testProvider } from "../lib/harness";

type TestState = { state: "idle" | "testing" | "ok" | "error"; message?: string };

type SaveState = "idle" | "saving" | "saved" | "error";

const kindList = Object.keys(providers) as ProviderKind[];

/** What the harness is asked to store, so an unchanged roster is not posted twice. */
const payloadOf = (list: Specialist[]) =>
  JSON.stringify(
    list.map(({ id, name, role, kind, baseUrl, model, apiKey, clearKey }) => ({
      id,
      name,
      role,
      kind,
      baseUrl,
      model,
      apiKey,
      // Part of the payload so "clear the stored key" is a change worth saving, even
      // though the visible field was already empty.
      clearKey: !!clearKey,
    })),
  );

export default function Workshop() {
  const [roster, setRoster] = useState<Specialist[]>(() => loadRoster());
  const [tests, setTests] = useState<Record<string, TestState>>({});
  const [showKeys, setShowKeys] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [save, setSave] = useState<SaveState>("idle");
  const [hydrated, setHydrated] = useState(false);
  const lastSaved = useRef("");

  useEffect(() => {
    saveRoster(roster);
  }, [roster]);

  // The harness is the source of truth for who exists and who already has a key;
  // this browser only keeps the non-secret fields so a reload shows "saved".
  useEffect(() => {
    let alive = true;
    hydrateRoster()
      .then((list) => {
        if (!alive) return;
        // A fresh browser must not turn the four starter drafts into four roster rows
        // every time the workshop opens. Seed them only when the harness has nobody.
        const next = list.length ? list : defaultRoster();
        lastSaved.current = list.length ? payloadOf(next) : "";
        setRoster(next);
      })
      .catch(() => {
        /* harness offline: keep the local copy, and an edit will report the failure */
        if (alive) setRoster((prev) => (prev.length ? prev : defaultRoster()));
      })
      .finally(() => {
        // Only a hydrated roster may be written back: a stale local copy pushed during
        // the first render used to replace the harness's roster and its saved keys.
        if (alive) setHydrated(true);
      });
    return () => {
      alive = false;
    };
  }, []);

  // A key typed here lives only in this tab until it reaches the harness, and the build
  // room resolves specialists by id — so an edit is saved as it is made, not only when
  // Launch is pressed.
  useEffect(() => {
    if (!hydrated || !roster.length) return;
    const payload = payloadOf(roster);
    if (payload === lastSaved.current) return;
    const timer = setTimeout(() => {
      lastSaved.current = payload;
      setSave("saving");
      pushRoster(roster)
        .then((saved) => {
          // Take the harness's answer: it drops the one-shot `clearKey` and reports the
          // new `has_key`, so a later edit cannot re-send a clear the user already got.
          lastSaved.current = payloadOf(saved);
          setRoster(saved);
          setSave("saved");
        })
        .catch(() => {
          lastSaved.current = "";
          setSave("error");
        });
    }, 900);
    return () => clearTimeout(timer);
  }, [roster, hydrated]);

  const update = (id: string, patch: Partial<Specialist>) => {
    setRoster((prev) => prev.map((s) => (s.id === id ? { ...s, ...patch } : s)));
    setNotice(null);
  };

  const changeKind = (s: Specialist, kind: ProviderKind) => {
    const preset = providers[kind];
    const wasDefault = Object.values(providers).some((p) => p.baseUrl === s.baseUrl && p.model === s.model);
    update(s.id, {
      kind,
      baseUrl: wasDefault || !s.baseUrl ? preset.baseUrl : s.baseUrl,
      model: wasDefault || !s.model ? preset.model : s.model,
    });
  };

  const remove = (id: string) => setRoster((prev) => prev.filter((s) => s.id !== id));

  const add = () =>
    setRoster((prev) => [...prev, newSpecialist(prev.length + 1)]);

  const duplicate = (s: Specialist) =>
    setRoster((prev) => [...prev, { ...s, id: crypto.randomUUID(), name: `${s.name} II` }]);

  const test = async (s: Specialist) => {
    setTests((prev) => ({ ...prev, [s.id]: { state: "testing" } }));
    // The harness holds the key, so a saved specialist tests without retyping it.
    const data = await testProvider(s);
    if (data.ok) {
      setTests((prev) => ({
        ...prev,
        [s.id]: {
          state: "ok",
          message: data.count ? `${data.count} models · ${data.models?.slice(0, 2).join(", ")}` : "Reachable",
        },
      }));
    } else {
      setTests((prev) => ({
        ...prev,
        [s.id]: { state: "error", message: data.error ?? data.detail ?? "Request failed" },
      }));
    }
  };

  const ready = roster.filter(isConfigured);
  const canLaunch = ready.length > 0;

  const launch = async () => {
    if (!canLaunch) {
      setNotice("Add at least one specialist with a base URL and model ID.");
      return;
    }
    try {
      const saved = await pushRoster(roster);
      lastSaved.current = payloadOf(saved);
      setSave("saved");
      setRoster(saved);
      saveSelected(ready[0].id);
      navigate("app");
    } catch (err) {
      setNotice(err instanceof Error ? err.message : "Could not save the roster to the harness.");
    }
  };

  return (
    <div className="site workshop">
      <header className="ws-nav">
        <div className="site-nav-inner">
          <button className="site-brand as-button" onClick={() => navigate("landing")}>
            <span className="site-mark">
              <Icon name="logo" size={15} />
            </span>
            <span>Cortex</span>
          </button>
          <div className="ws-steps">
            <span className="ws-step is-done">
              <i>
                <Icon name="check" size={10} />
              </i>
              Account
            </span>
            <span className="ws-line" />
            <span className="ws-step is-active">
              <i>2</i>
              Models
            </span>
            <span className="ws-line" />
            <span className="ws-step">
              <i>3</i>
              Launch
            </span>
          </div>
          <div className="site-actions">
            <button className="link-btn" onClick={() => setRoster(defaultRoster())}>
              Reset team
            </button>
            <button className="site-cta" onClick={launch}>
              Launch harness
              <Icon name="arrowRight" size={15} />
            </button>
          </div>
        </div>
      </header>

      <main className="ws-main">
        <div className="ws-intro">
          <span className="kicker">Workshop</span>
          <h1>Assemble the chain of command.</h1>
          <p>
            Each specialist gets its own provider, key, model and role. Point the planner at a reasoning
            model, frontend and backend at fast coders, review at a second opinion — add as many as the work
            needs.
          </p>
        </div>

        <div className="ws-meta">
          <span>
            <b>{roster.length}</b> specialists
          </span>
          <span className="ws-dot" />
          <span>
            <b>{ready.length}</b> ready to route
          </span>
          <span className="ws-dot" />
          <span className="ws-hint">
            <Icon name="shield" size={13} /> Keys are stored by the harness, not in this browser
          </span>
          {save !== "idle" && (
            <>
              <span className="ws-dot" />
              <span className="ws-hint">
                <Icon name={save === "error" ? "alert" : "check"} size={13} />
                {save === "saving"
                  ? "Saving to the harness…"
                  : save === "saved"
                    ? "Saved to the harness"
                    : "Not saved — the harness is unreachable"}
              </span>
            </>
          )}
        </div>

        <div className="ws-grid">
          {roster.map((s, i) => {
            const t = tests[s.id] ?? { state: "idle" as const };
            const configured = isConfigured(s);
            return (
              <article className={`spec${configured ? "" : " is-draft"}`} key={s.id}>
                <header className="spec-head">
                  <span className="spec-index">{String(i + 1).padStart(2, "0")}</span>
                  <input
                    className="spec-name"
                    value={s.name}
                    placeholder="Agent name"
                    onChange={(e) => update(s.id, { name: e.target.value })}
                  />
                  <span className={`role-pill role-${s.role}`}>{roleLabel(s.role)}</span>
                  <button className="icon-btn spec-remove" onClick={() => remove(s.id)} title="Remove">
                    <Icon name="trash" size={15} />
                  </button>
                </header>

                <div className="spec-fields">
                  <label className="field">
                    <span>Provider</span>
                    <div className="select-shell">
                      <select value={s.kind} onChange={(e) => changeKind(s, e.target.value as ProviderKind)}>
                        {kindList.map((k) => (
                          <option key={k} value={k}>
                            {providers[k].label}
                          </option>
                        ))}
                      </select>
                      <Icon name="chevronDown" size={13} />
                    </div>
                  </label>

                  <label className="field">
                    <span>Role</span>
                    <div className="select-shell">
                      <select value={s.role} onChange={(e) => update(s.id, { role: e.target.value as Role })}>
                        {roles.map((r) => (
                          <option key={r.id} value={r.id}>
                            {r.label}
                          </option>
                        ))}
                      </select>
                      <Icon name="chevronDown" size={13} />
                    </div>
                  </label>

                  <label className="field spec-wide">
                    <span>Base URL</span>
                    <div className="input-shell">
                      <input
                        value={s.baseUrl}
                        placeholder={providers[s.kind].hint}
                        spellCheck={false}
                        onChange={(e) => update(s.id, { baseUrl: e.target.value })}
                      />
                    </div>
                  </label>

                  <label className="field">
                    <span className="field-label">
                      API key
                      {s.has_key && (
                        <button
                          type="button"
                          className="ghost-link"
                          onClick={() => update(s.id, { apiKey: "", clearKey: true, has_key: false })}
                        >
                          Clear stored key
                        </button>
                      )}
                    </span>
                    <div className="input-shell">
                      <input
                        type={showKeys[s.id] ? "text" : "password"}
                        value={s.apiKey}
                        placeholder={
                          s.kind === "ollama"
                            ? "Not required for a local daemon"
                            : s.has_key && !s.apiKey
                              ? "Saved on the harness — type to replace"
                              : "sk-…"
                        }
                        autoComplete="off"
                        spellCheck={false}
                        onChange={(e) => update(s.id, { apiKey: e.target.value })}
                      />
                      <button
                        type="button"
                        className="input-addon"
                        onClick={() => setShowKeys((prev) => ({ ...prev, [s.id]: !prev[s.id] }))}
                        aria-label="Toggle key visibility"
                      >
                        <Icon name={showKeys[s.id] ? "eyeOff" : "eye"} size={15} />
                      </button>
                    </div>
                  </label>

                  <label className="field">
                    <span>Model ID</span>
                    <div className="input-shell">
                      <input
                        value={s.model}
                        placeholder={providers[s.kind].model || "model id"}
                        spellCheck={false}
                        onChange={(e) => update(s.id, { model: e.target.value })}
                      />
                    </div>
                  </label>
                </div>

                <footer className="spec-foot">
                  <button className="test-btn" onClick={() => test(s)} disabled={t.state === "testing"}>
                    {t.state === "testing" ? (
                      <span className="spinner" />
                    ) : (
                      <Icon name="link" size={14} />
                    )}
                    {t.state === "testing" ? "Testing…" : "Test connection"}
                  </button>

                  {t.state === "ok" && (
                    <span className="status is-ok">
                      <Icon name="check" size={12} /> {t.message}
                    </span>
                  )}
                  {t.state === "error" && (
                    <span className="status is-error">
                      <Icon name="alert" size={12} /> {t.message}
                    </span>
                  )}
                  {t.state === "idle" && (
                    <button className="ghost-link" onClick={() => duplicate(s)}>
                      Duplicate
                    </button>
                  )}
                </footer>
              </article>
            );
          })}

          <button className="add-spec" onClick={add}>
            <span className="add-icon">
              <Icon name="plus" size={18} />
            </span>
            <b>Add specialist</b>
            <span>Any provider, any model, unlimited agents</span>
          </button>
        </div>

        {notice && (
          <p className="ws-notice">
            <Icon name="alert" size={14} />
            {notice}
          </p>
        )}
      </main>

      <div className="ws-bar">
        <div className="ws-bar-info">
          <span className="ws-avatar-stack">
            {roster.slice(0, 4).map((s) => (
              <i key={s.id} title={`${s.name} · ${s.model}`}>
                {s.name.slice(0, 1).toUpperCase()}
              </i>
            ))}
            {roster.length > 4 && <i className="more">+{roster.length - 4}</i>}
          </span>
          <span>
            {roster.length} specialist{roster.length === 1 ? "" : "s"} · {ready.length} ready to route
          </span>
        </div>
        <div className="ws-bar-actions">
          <button className="link-btn" onClick={() => navigate("build")}>
            Build room
          </button>
          <button className="link-btn" onClick={() => navigate("landing")}>
            Back to site
          </button>
          <button className="cta-primary" onClick={launch} disabled={!canLaunch}>
            Enter the harness
            <Icon name="arrowRight" size={16} />
          </button>
        </div>
      </div>
    </div>
  );
}

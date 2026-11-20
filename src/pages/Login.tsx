import { useEffect, useState, type FormEvent } from "react";
import { Icon } from "../components/Icon";
import { navigate } from "../lib/router";
import { API } from "../lib/providers";
import { login, signup, type User } from "../lib/auth";

type Mode = "signin" | "signup";

type Props = {
  user: User | null;
  onAuth: (user: User) => void;
  onLogout: () => void;
};

export default function Login({ user, onAuth, onLogout }: Props) {
  const [mode, setMode] = useState<Mode>(() =>
    window.location.hash.includes("mode=signup") ? "signup" : "signin",
  );
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [durable, setDurable] = useState(true);

  useEffect(() => {
    fetch(`${API}/api/health`)
      .then((res) => res.json())
      .then((data: { auth?: { durable?: boolean } }) => setDurable(data.auth?.durable !== false))
      .catch(() => {
        /* health is a hint, not a gate */
      });
  }, []);

  const persistenceNote = durable ? null : (
    <p className="auth-warn" role="status">
      Accounts and sessions reset when this instance restarts. Set <code>HARNESS_AUTH_DB</code> to a
      persistent disk to keep them.
    </p>
  );

  const switchMode = (next: Mode) => {
    setMode(next);
    setError(null);
    const hash = next === "signup" ? "#/login?mode=signup" : "#/login";
    if (window.location.hash !== hash) window.history.replaceState(null, "", hash);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (mode === "signup" && !name.trim()) {
      setError("Enter your name.");
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email.trim())) {
      setError("Enter a valid email address.");
      return;
    }
    if (password.length < 8) {
      setError("Use a password with at least 8 characters.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const account =
        mode === "signup"
          ? await signup(name.trim(), email.trim(), password)
          : await login(email.trim(), password);
      onAuth(account);
      navigate("workshop");
    } catch (err) {
      setError(err instanceof Error ? err.message : "That request did not go through. Try again.");
    } finally {
      setBusy(false);
    }
  };

  if (user) {
    return (
      <div className="auth">
        <div className="auth-card">
          <aside className="auth-aside">
            <button className="auth-brand" onClick={() => navigate("landing")}>
              <span className="site-mark is-light">
                <Icon name="logo" size={15} />
              </span>
              Cortex
            </button>
            <div className="auth-aside-body">
              <div className="auth-orb" />
              <h2>Your models. Your chain of command.</h2>
              <p>You are signed in. Open the workshop to assemble the team or start a build.</p>
            </div>
            <p className="auth-quote">“The unit test for trust: can you show the evidence?”</p>
          </aside>
          <div className="auth-form-wrap">
            <button className="back-link" onClick={() => navigate("landing")}>
              <Icon name="arrowLeft" size={14} /> Back
            </button>
            <h1>You&apos;re signed in</h1>
            <p className="auth-sub">{user.email}</p>
            <button className="submit-btn" onClick={() => navigate("workshop")}>
              Open workshop
              <Icon name="arrowRight" size={16} />
            </button>
            <p className="auth-foot">
              Not {user.name || "you"}?{" "}
              <button className="link-btn is-accent" onClick={onLogout}>
                Sign out
              </button>
            </p>
            {persistenceNote}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="auth">
      <div className="auth-card">
        <aside className="auth-aside">
          <button className="auth-brand" onClick={() => navigate("landing")}>
            <span className="site-mark is-light">
              <Icon name="logo" size={15} />
            </span>
            Cortex
          </button>

          <div className="auth-aside-body">
            <div className="auth-orb" />
            <h2>Your models. Your chain of command.</h2>
            <p>
              {mode === "signup"
                ? "Create an account to assemble specialists, route each role to a different provider, and watch the review loop run."
                : "Sign in to assemble specialists, route each role to a different provider, and watch the review loop run."}
            </p>
            <ul className="auth-points">
              <li>
                <Icon name="check" size={14} /> Claims tied to evidence, not vibes
              </li>
              <li>
                <Icon name="check" size={14} /> One orchestrator, many scoped agents
              </li>
              <li>
                <Icon name="check" size={14} /> Keys stay on the server
              </li>
            </ul>
          </div>

          <p className="auth-quote">“The unit test for trust: can you show the evidence?”</p>
        </aside>

        <div className="auth-form-wrap">
          <button className="back-link" onClick={() => navigate("landing")}>
            <Icon name="arrowLeft" size={14} /> Back
          </button>

          <h1>{mode === "signup" ? "Create your account" : "Welcome back"}</h1>
          <p className="auth-sub">
            {mode === "signup"
              ? "One account, your own way in. No demo credentials."
              : "Sign in to open the Cortex workshop."}
          </p>

          <div className="auth-switch" role="tablist" aria-label="Account access">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "signin"}
              className={mode === "signin" ? "is-on" : ""}
              onClick={() => switchMode("signin")}
            >
              Sign in
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "signup"}
              className={mode === "signup" ? "is-on" : ""}
              onClick={() => switchMode("signup")}
            >
              Create account
            </button>
          </div>

          <form onSubmit={submit} noValidate>
            {mode === "signup" && (
              <label className="field">
                <span>Name</span>
                <div className={`input-shell${error && !name ? " is-error" : ""}`}>
                  <input
                    type="text"
                    value={name}
                    autoComplete="name"
                    placeholder="Ada Lovelace"
                    onChange={(e) => {
                      setName(e.target.value);
                      setError(null);
                    }}
                  />
                </div>
              </label>
            )}

            <label className="field">
              <span>Email</span>
              <div className={`input-shell${error && !email ? " is-error" : ""}`}>
                <input
                  type="email"
                  value={email}
                  autoComplete="email"
                  placeholder="you@company.com"
                  onChange={(e) => {
                    setEmail(e.target.value);
                    setError(null);
                  }}
                />
              </div>
            </label>

            <label className="field">
              <span>Password</span>
              <div className="input-shell">
                <input
                  type={show ? "text" : "password"}
                  value={password}
                  autoComplete={mode === "signup" ? "new-password" : "current-password"}
                  placeholder="At least 8 characters"
                  onChange={(e) => {
                    setPassword(e.target.value);
                    setError(null);
                  }}
                />
                <button
                  type="button"
                  className="input-addon"
                  onClick={() => setShow((v) => !v)}
                  aria-label={show ? "Hide password" : "Show password"}
                >
                  <Icon name={show ? "eyeOff" : "eye"} size={16} />
                </button>
              </div>
            </label>

            {error && (
              <p className="form-error" role="alert">
                <Icon name="alert" size={14} />
                {error}
              </p>
            )}

            <button className="submit-btn" type="submit" disabled={busy}>
              {busy ? "Working…" : mode === "signup" ? "Create account" : "Sign in"}
              {!busy && <Icon name="arrowRight" size={16} />}
            </button>
          </form>

          <p className="auth-foot">
            {mode === "signup" ? "Already have an account? " : "New here? "}
            <button
              className="link-btn is-accent"
              onClick={() => switchMode(mode === "signup" ? "signin" : "signup")}
            >
              {mode === "signup" ? "Sign in" : "Create an account"}
            </button>
          </p>
          {persistenceNote}
        </div>
      </div>
    </div>
  );
}

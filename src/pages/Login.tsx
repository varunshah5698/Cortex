import { useState, type FormEvent } from "react";
import { Icon } from "../components/Icon";
import { navigate } from "../lib/router";

export default function Login() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [remember, setRemember] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email.trim())) {
      setError("Enter a valid email address.");
      return;
    }
    if (password.length < 8) {
      setError("Passwords are at least 8 characters.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const store = remember ? localStorage : sessionStorage;
      store.setItem("cortex.session", JSON.stringify({ email: email.trim(), at: Date.now() }));
    } catch {
      /* storage unavailable */
    }
    setTimeout(() => navigate("workshop"), 450);
  };

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
              Sign in to assemble specialists, route each role to a different provider, and watch the review
              loop run.
            </p>
            <ul className="auth-points">
              <li>
                <Icon name="check" size={14} /> Claims tied to evidence, not vibes
              </li>
              <li>
                <Icon name="check" size={14} /> One orchestrator, many scoped agents
              </li>
              <li>
                <Icon name="check" size={14} /> Keys stay in your workspace
              </li>
            </ul>
          </div>

          <p className="auth-quote">“The unit test for trust: can you show the evidence?”</p>
        </aside>

        <div className="auth-form-wrap">
          <button className="back-link" onClick={() => navigate("landing")}>
            <Icon name="arrowLeft" size={14} /> Back
          </button>

          <h1>Welcome back</h1>
          <p className="auth-sub">Sign in to open the harness workshop.</p>

          <form onSubmit={submit} noValidate>
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
                  autoComplete="current-password"
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

            <div className="auth-row">
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={remember}
                  onChange={(e) => setRemember(e.target.checked)}
                />
                <span className="box">
                  <Icon name="check" size={11} />
                </span>
                Remember me
              </label>
              <button type="button" className="link-btn is-accent">
                Forgot password?
              </button>
            </div>

            {error && (
              <p className="form-error">
                <Icon name="alert" size={14} />
                {error}
              </p>
            )}

            <button className="submit-btn" type="submit" disabled={busy}>
              {busy ? "Opening workshop…" : "Continue"}
              {!busy && <Icon name="arrowRight" size={16} />}
            </button>
          </form>

          <div className="divider">
            <span>or</span>
          </div>

          <div className="social-row">
            <button className="social-btn" type="button" onClick={() => navigate("workshop")}>
              <span className="social-glyph">G</span>
              Google
            </button>
            <button className="social-btn" type="button" onClick={() => navigate("workshop")}>
              <span className="social-glyph">⌘</span>
              GitHub
            </button>
          </div>

          <p className="auth-foot">
            Just exploring?{" "}
            <button className="link-btn is-accent" onClick={() => navigate("workshop")}>
              Open a local workspace
            </button>
          </p>
        </div>
      </div>
    </div>
  );
}

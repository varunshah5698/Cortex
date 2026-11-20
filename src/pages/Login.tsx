import { useEffect, useState, type FormEvent } from "react";
import { Icon } from "../components/Icon";
import { navigate } from "../lib/router";
import { API } from "../lib/providers";
import { finishGoogle, login, signup, startGoogle, type User } from "../lib/auth";

type Mode = "signin" | "signup";

/**
 * Google's mark, inline.
 *
 * An external asset would be a network dependency on the login page, and the one page
 * that must render when the network is having a bad day is the one that signs you in.
 */
function GoogleMark() {
  return (
    <svg width="17" height="17" viewBox="0 0 48 48" aria-hidden="true" focusable="false">
      <path
        fill="#EA4335"
        d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"
      />
      <path
        fill="#4285F4"
        d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"
      />
      <path
        fill="#FBBC05"
        d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24s.92 7.54 2.56 10.78l7.97-6.19z"
      />
      <path
        fill="#34A853"
        d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"
      />
    </svg>
  );
}

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
  const [googleBusy, setGoogleBusy] = useState(false);
  const [durable, setDurable] = useState(true);

  // The Google callback lands here with a token (or an error) in the fragment. Adopting
  // it is what turns "signed in at Convex" into "signed in to /api" — without it the
  // person is authenticated to the data layer and 401 on every build.
  useEffect(() => {
    let alive = true;
    finishGoogle()
      .then((account) => {
        if (!alive || !account) return;
        onAuth(account);
        navigate("app");
      })
      .catch((err: unknown) => {
        if (!alive) return;
        setError(err instanceof Error ? err.message : "Google sign-in did not finish. Try again.");
      })
      .finally(() => {
        if (alive) setGoogleBusy(false);
      });
    return () => {
      alive = false;
    };
  }, [onAuth]);

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

  // A refused redirect (an unconfigured deployment, a blocked popup) must surface here
  // rather than as a button that silently does nothing.
  const google = async () => {
    setError(null);
    setGoogleBusy(true);
    try {
      await startGoogle();
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Google sign-in is not available right now. Try again.",
      );
      setGoogleBusy(false);
    }
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
            <button
              type="button"
              className="google-btn"
              onClick={() => void google()}
              disabled={busy || googleBusy}
            >
              <GoogleMark />
              {googleBusy ? "Opening Google…" : "Continue with Google"}
            </button>
            <div className="auth-divider" role="separator">
              <span>or use your email</span>
            </div>
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

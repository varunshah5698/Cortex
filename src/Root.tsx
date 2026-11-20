import App from "./App";
import Build from "./pages/Build";
import Files from "./pages/Files";
import Landing from "./pages/Landing";
import Login from "./pages/Login";
import Metrics from "./pages/Metrics";
import Workshop from "./pages/Workshop";
import { logout, useSession } from "./lib/auth";
import { navigate, useRoute } from "./lib/router";

const guarded = new Set(["workshop", "app", "build", "files", "metrics"]);

export default function Root() {
  const route = useRoute();
  const { user, loading, setUser } = useSession();

  const signOut = () => {
    void logout().finally(() => {
      setUser(null);
      navigate("login");
    });
  };

  const login = <Login user={user} onAuth={setUser} onLogout={signOut} />;

  // The sign-in form is a server-backed account gate for the UI. The API enforces the
  // same session cookie on a public origin; loopback keeps local development open.
  if (loading) return <div className="auth-loading">Checking your session…</div>;
  if (route === "login") return login;
  if (guarded.has(route) && !user) return login;

  switch (route) {
    case "workshop":
      return <Workshop />;
    case "app":
      return <App />;
    case "build":
      return <Build />;
    case "files":
      return <Files />;
    case "metrics":
      return <Metrics />;
    default:
      return <Landing user={user} onLogout={signOut} />;
  }
}

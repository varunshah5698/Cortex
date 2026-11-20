import App from "./App";
import Build from "./pages/Build";
import Files from "./pages/Files";
import Landing from "./pages/Landing";
import Login from "./pages/Login";
import Metrics from "./pages/Metrics";
import Workshop from "./pages/Workshop";
import { useRoute } from "./lib/router";

export default function Root() {
  const route = useRoute();

  switch (route) {
    case "login":
      return <Login />;
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
      return <Landing />;
  }
}

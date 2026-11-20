import { useState, useEffect } from "react";

export type Route = "landing" | "login" | "workshop" | "app" | "build" | "files" | "metrics";

const paths: Record<Route, string> = {
  landing: "/",
  login: "/login",
  workshop: "/setup",
  app: "/app",
  build: "/build",
  files: "/files",
  metrics: "/metrics",
};

function parse(hash: string): Route {
  const clean = hash.replace(/^#/, "").split("?")[0] || "/";
  const entry = (Object.entries(paths) as [Route, string][]).find(([, p]) => p === clean);
  return entry ? entry[0] : "landing";
}

export function navigate(route: Route) {
  if (window.location.hash.replace(/^#/, "") === paths[route]) return;
  window.location.hash = paths[route];
  window.scrollTo({ top: 0 });
}

export function useRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parse(window.location.hash));

  useEffect(() => {
    const onChange = () => {
      setRoute(parse(window.location.hash));
      window.scrollTo({ top: 0 });
    };
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return route;
}

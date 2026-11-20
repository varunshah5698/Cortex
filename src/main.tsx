import React from "react";
import ReactDOM from "react-dom/client";
import Root from "./Root";
import { installApiClient } from "./lib/api";
import "./styles.css";
import "./site.css";
import "./build.css";

installApiClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);

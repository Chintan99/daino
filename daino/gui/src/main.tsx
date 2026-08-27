import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { DocsApp } from "./components/docs/DocsApp";
import { queryClient } from "./lib/queryClient";
// Applies the saved theme and interface font size before the first paint.
import "./store/settingsStore";
import "./lib/monaco";
import "./styles/global.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* /docs is the documentation reader; every other path is the IDE. */}
      {window.location.pathname.replace(/\/+$/, "") === "/docs" ? <DocsApp /> : <App />}
    </QueryClientProvider>
  </React.StrictMode>,
);

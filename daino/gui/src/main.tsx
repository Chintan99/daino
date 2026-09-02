import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { DocsApp } from "./components/docs/DocsApp";
import { queryClient } from "./lib/queryClient";
// Applies the saved theme and interface font size before the first paint.
import "./store/settingsStore";
// Monaco is deliberately *not* imported here. It is 4 MB, only CODE needs
// it, and an app opened with no file yet open should not pay for it — each
// component that renders an editor brings it along instead.
import "./styles/global.css";

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* /docs is the documentation reader; every other path is the IDE. */}
      {window.location.pathname.replace(/\/+$/, "") === "/docs" ? <DocsApp /> : <App />}
    </QueryClientProvider>
  </React.StrictMode>,
);

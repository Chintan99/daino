import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { DocsApp } from "./components/docs/DocsApp";
import "./lib/monaco";
import "./styles/global.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
      staleTime: 5_000,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      {/* /docs is the documentation reader; every other path is the IDE. */}
      {window.location.pathname.replace(/\/+$/, "") === "/docs" ? <DocsApp /> : <App />}
    </QueryClientProvider>
  </React.StrictMode>,
);

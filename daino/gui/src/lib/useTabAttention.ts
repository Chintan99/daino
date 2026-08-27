// Mark the browser tab when a turn ends while nobody is looking at it.
//
// The desktop notification is the loud signal; this is the quiet one that is
// still there ten minutes later. The title is restored the moment the tab is
// looked at again.
import { useEffect, useRef } from "react";
import { useAgentStore } from "../store/agentStore";
import { BRAND } from "./branding";

const BASE_TITLE = `${BRAND} — local AI coding agent`;

export function useTabAttention(): void {
  const state = useAgentStore((s) => s.activity.state);
  const previous = useRef(state);

  useEffect(() => {
    const ended =
      previous.current !== state && (state === "completed" || state === "failed");
    previous.current = state;
    if (!ended || document.visibilityState === "visible") return;
    document.title = `${state === "failed" ? "✗" : "✓"} ${BASE_TITLE}`;
  }, [state]);

  useEffect(() => {
    const clear = () => {
      if (document.visibilityState === "visible") document.title = BASE_TITLE;
    };
    document.addEventListener("visibilitychange", clear);
    window.addEventListener("focus", clear);
    return () => {
      document.removeEventListener("visibilitychange", clear);
      window.removeEventListener("focus", clear);
    };
  }, []);
}

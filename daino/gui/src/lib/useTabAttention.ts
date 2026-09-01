// Mark the browser tab when long work ends while nobody is looking at it.
//
// The desktop notification the server raises is the loud signal; this is the
// quiet one that is still there ten minutes later. The title is restored the
// moment the tab is looked at again.
//
// Two things qualify: an agent turn ending, and an inspection landing. An
// inspection is the longer of the two and the one whose answer can stop a
// release, so its marker carries the verdict rather than just a tick.
import { useEffect, useRef } from "react";
import { useAgentStore } from "../store/agentStore";
import { useQALatest } from "../api/hooks";
import { BRAND } from "./branding";

const BASE_TITLE = `${BRAND} — local AI coding agent`;

const VERDICT_MARK: Record<string, string> = {
  pass: "✓ Inspection passed",
  warn: "▲ Inspection needs review",
  blocked: "✗ Inspection blocked",
  unknown: "• Inspection finished",
};

export function useTabAttention(): void {
  const state = useAgentStore((s) => s.activity.state);
  const previousState = useRef(state);
  const { data: qa } = useQALatest();
  const wasRunning = useRef(false);

  useEffect(() => {
    const ended =
      previousState.current !== state &&
      (state === "completed" || state === "failed");
    previousState.current = state;
    if (!ended || document.visibilityState === "visible") return;
    document.title = `${state === "failed" ? "✗" : "✓"} ${BASE_TITLE}`;
  }, [state]);

  useEffect(() => {
    const running = !!qa?.running;
    const landed = wasRunning.current && !running && !!qa?.report;
    wasRunning.current = running;
    if (!landed || document.visibilityState === "visible") return;
    const verdict = qa?.report?.verdict ?? "unknown";
    document.title = `${VERDICT_MARK[verdict] ?? VERDICT_MARK.unknown} — ${BRAND}`;
  }, [qa?.running, qa?.report]);

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

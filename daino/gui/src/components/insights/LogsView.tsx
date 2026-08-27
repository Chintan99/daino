import { useEffect, useMemo, useRef, useState } from "react";
import { useLogs } from "../../api/hooks";
import { useAgentStore } from "../../store/agentStore";
import type { AuditEvent, WsEvent } from "../../api/types";
import { fmtTime } from "./format";

type Mode = "summary" | "detailed" | "raw";

/**
 * Render one live mission event as a single safe operational line.
 *
 * Mirrors the TUI's `_live_event_summary`: model reasoning is never echoed
 * here, only the fact that the model is working.
 */
function liveSummary(event: WsEvent): string {
  const get = (key: string) => String(event[key] ?? "");
  switch (event.kind) {
    case "ModelStreamChunk":
    case "ModelReasoningChunk":
      return "";
    case "MissionCreated":
      return "Understanding the prompt";
    case "AgentRoleChanged":
      return `${get("role") || "agent"} preparing the next action`;
    case "ModelSelected":
      return `${get("role") || "agent"} using ${get("model") || "model"}`;
    case "ToolStarted":
    case "ToolProgress":
    case "ToolCompleted":
      return `${get("tool").replace(/^agent\./, "")}: ${get("summary")}${
        event.kind === "ToolCompleted" ? " completed" : ""
      }`.trim();
    case "ToolFailed":
      return `${get("tool")} failed: ${get("error")}`;
    case "TaskStarted":
      return `Task started: ${get("title")}`;
    case "TaskCompleted":
      return `Task completed: ${get("title")}`;
    case "FileChanged":
      return `Changed ${get("path")}`;
    case "TestsStarted":
      return `Running ${(event.commands as unknown[] | undefined)?.length ?? 0} verification command(s)`;
    case "TestsCompleted":
      return `Verification ${event.passed ? "passed" : "failed"}: ${get(
        "passed_count",
      )} passed, ${get("failed_count")} failed`;
    case "MissionCompleted":
      return "Prompt completed";
    case "MissionFailed":
      return `Prompt failed: ${get("error")}`;
    default:
      return String(event.kind ?? "event").replace(/_/g, " ");
  }
}

function renderRow(event: AuditEvent, mode: Mode) {
  const name = String(event.event ?? "event");
  if (mode === "raw") {
    return <span className="rest">{JSON.stringify(event)}</span>;
  }
  if (mode === "detailed") {
    const rest: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(event))
      if (k !== "timestamp" && k !== "event") rest[k] = v;
    return (
      <>
        <span className="ev">{name}</span>
        <span className="rest">{JSON.stringify(rest)}</span>
      </>
    );
  }
  return (
    <>
      <span className="ev">{name}</span>
      <span className="rest">{String(event.mission_id ?? "")}</span>
    </>
  );
}

export function LogsView() {
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<Mode>("summary");
  const [follow, setFollow] = useState(true);
  const { data, isLoading } = useLogs(query);

  const events = useAgentStore((s) => s.events);
  const turnRunning = useAgentStore((s) => s.turnRunning);

  const live = useMemo(
    () =>
      events
        .map((item) => ({ id: item.id, at: item.at, text: liveSummary(item.event) }))
        .filter((item) => item.text)
        .slice(-60),
    [events],
  );

  const liveRef = useRef<HTMLDivElement | null>(null);
  const tailRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = liveRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [live.length]);

  useEffect(() => {
    if (!follow) return;
    const el = tailRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [data, follow]);

  const rows = data?.events ?? [];
  const latest = live.length ? live[live.length - 1].text : "";

  return (
    <div className="split">
      <div className="split-left" style={{ width: 380 }}>
        <div className="panel-header">Live activity</div>
        <div className="log-live">
          <span className={`pulse ${turnRunning ? "live" : "idle"}`} />
          <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>
            {turnRunning
              ? latest || "Working…"
              : latest || "Idle — waiting for the next prompt"}
          </span>
        </div>
        <div className="log-stream" ref={liveRef}>
          {live.length === 0 && (
            <div className="empty">
              Events from the running turn stream in here.
            </div>
          )}
          {live.map((item) => (
            <div className="log-row" key={item.id}>
              <span className="ts">
                {new Date(item.at).toLocaleTimeString([], {
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                })}
              </span>
              <span className="rest">{item.text}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="split-right">
        <div className="toolbar">
          <div className="segmented">
            {(["summary", "detailed", "raw"] as Mode[]).map((m) => (
              <button
                key={m}
                className={mode === m ? "active" : ""}
                onClick={() => setMode(m)}
              >
                {m.toUpperCase()}
              </button>
            ))}
          </div>
          <input
            className="input"
            style={{ maxWidth: 320 }}
            placeholder="Filter mission, agent, tool, severity…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="grow" />
          <label className="row muted" style={{ fontSize: "var(--fs-11)", cursor: "pointer" }}>
            <input
              type="checkbox"
              checked={follow}
              onChange={(e) => setFollow(e.target.checked)}
            />
            Follow
          </label>
          <span className="badge">
            {data ? `${data.matched}/${data.total}` : "—"}
          </span>
        </div>
        <div className="log-stream" ref={tailRef}>
          {isLoading && <div className="empty">Loading…</div>}
          {!isLoading && rows.length === 0 && (
            <div className="empty">
              {query
                ? "No recorded events match that filter."
                : "The audit log is empty. It fills as the agent works."}
            </div>
          )}
          {rows.map((event, i) => (
            <div className="log-row" key={`${event.timestamp}-${i}`}>
              <span className="ts">{fmtTime(String(event.timestamp ?? ""))}</span>
              {renderRow(event, mode)}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

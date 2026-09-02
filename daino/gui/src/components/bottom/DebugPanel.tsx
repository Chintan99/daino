import { useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { openFileInEditor } from "../../lib/openFile";
import { useDebugStore } from "../../store/debugStore";
import { useEditorStore } from "../../store/editorStore";
import type { DebugScope, DebugVariable } from "../../api/types";

/**
 * Run and inspect a program: breakpoints, stepping, stack, and variables.
 *
 * Everything comes from the server's session, so reloading the tab while
 * stopped at a breakpoint shows the same frame rather than an empty panel.
 * Breakpoints outlive the session entirely — they are the user's, not the run's.
 */
export function DebugPanel() {
  const qc = useQueryClient();
  const apply = useDebugStore((s) => s.apply);
  const session = useDebugStore((s) => s.session);
  const breakpoints = useDebugStore((s) => s.breakpoints);
  const selectedFrameId = useDebugStore((s) => s.selectedFrameId);
  const selectFrame = useDebugStore((s) => s.selectFrame);
  const activePath = useEditorStore((s) => s.activePath);
  const [busy, setBusy] = useState("");
  const [expression, setExpression] = useState("");
  const [answer, setAnswer] = useState("");

  const { data: adapters } = useQuery({
    queryKey: ["debug", "adapters"],
    queryFn: api.debugAdapters,
    staleTime: 30_000,
  });
  const { data: status } = useQuery({
    queryKey: ["debug", "state"],
    queryFn: api.debugState,
    // Polled while live: DAP events arrive on the server, and the panel has to
    // notice a breakpoint hit without the user clicking anything.
    refetchInterval: (query) => (query.state.data?.running ? 400 : false),
  });

  useEffect(() => {
    if (status) apply(status);
  }, [status, apply]);

  // A stopped debuggee has a stack; asking for it is what fills the panel.
  useEffect(() => {
    if (session?.state === "stopped" && session.frames.length === 0) {
      void api.debugStack().then(apply).catch(() => undefined);
    }
  }, [session?.state, session?.frames.length, apply]);

  const stopped = session?.state === "stopped";
  const live = !!status?.running;
  const python = (adapters?.adapters ?? []).find((item) => item.id === "debugpy");

  const act = async (label: string, fn: () => Promise<unknown>) => {
    setBusy(label);
    try {
      const result = await fn();
      if (result && typeof result === "object" && "breakpoints" in result) {
        apply(result as never);
      }
      await qc.invalidateQueries({ queryKey: ["debug", "state"] });
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy("");
    }
  };

  const launch = () => {
    if (!activePath) {
      window.alert("Open the file you want to debug first.");
      return;
    }
    void act("launch", () => api.debugLaunch({ program: activePath }));
  };

  const evaluate = async () => {
    if (!expression.trim()) return;
    try {
      const result = await api.debugEvaluate(expression.trim(), selectedFrameId ?? 0);
      setAnswer(result.type ? `${result.result}  (${result.type})` : result.result);
    } catch (err) {
      setAnswer(err instanceof ApiError ? err.message : String(err));
    }
  };

  if (python && !python.available && !live) {
    return (
      <div className="scroll-y" style={{ height: "100%" }}>
        <div className="empty">
          No debug adapter is installed.
          <div style={{ marginTop: 6, fontSize: "var(--fs-11)" }}>
            For Python: <code>{python.install}</code>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="panel" style={{ height: "100%" }}>
      <div className="toolbar">
        {!live ? (
          <button
            className="btn primary sm"
            disabled={!!busy || !activePath}
            title={activePath ? `Debug ${activePath}` : "Open a file to debug"}
            onClick={launch}
          >
            {busy === "launch" ? "Starting…" : "Debug this file"}
          </button>
        ) : (
          <>
            <button
              className="btn primary sm"
              disabled={!!busy || !stopped}
              title="Continue (F5)"
              onClick={() => void act("continue", () => api.debugControl("continue"))}
            >
              ▶
            </button>
            <button
              className="btn subtle sm"
              disabled={!!busy || stopped}
              title="Pause"
              onClick={() => void act("pause", () => api.debugControl("pause"))}
            >
              ⏸
            </button>
            <button
              className="btn subtle sm"
              disabled={!!busy || !stopped}
              title="Step over (F10)"
              onClick={() => void act("over", () => api.debugControl("step-over"))}
            >
              ⤼
            </button>
            <button
              className="btn subtle sm"
              disabled={!!busy || !stopped}
              title="Step into (F11)"
              onClick={() => void act("into", () => api.debugControl("step-into"))}
            >
              ↳
            </button>
            <button
              className="btn subtle sm"
              disabled={!!busy || !stopped}
              title="Step out (⇧F11)"
              onClick={() => void act("out", () => api.debugControl("step-out"))}
            >
              ↰
            </button>
            <button
              className="btn danger sm"
              disabled={!!busy}
              title="Stop"
              onClick={() => void act("stop", () => api.debugControl("stop"))}
            >
              ■
            </button>
          </>
        )}
        <span className="grow" />
        {session && (
          <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
            {session.program} · {session.state}
            {session.stop_reason && ` (${session.stop_reason})`}
            {session.exit_code !== null && ` · exit ${session.exit_code}`}
          </span>
        )}
      </div>

      {session?.error && (
        <div className="problems-gap">
          <strong>The debug session failed</strong>
          <div className="muted mono" style={{ whiteSpace: "pre-wrap" }}>
            {session.error}
          </div>
        </div>
      )}

      <div className="split" style={{ flex: 1, minHeight: 0 }}>
        <div className="split-left" style={{ width: 260 }}>
          <div className="section-title">Call stack</div>
          <div className="scroll-y" style={{ flex: 1 }}>
            {!stopped && <div className="empty">Not stopped.</div>}
            {stopped &&
              session?.frames.map((frame) => (
                <div
                  key={frame.id}
                  className={`debug-frame ${frame.id === selectedFrameId ? "active" : ""}`}
                  onClick={() => {
                    selectFrame(frame.id);
                    if (frame.path) {
                      void openFileInEditor(frame.path, { line: frame.line });
                    }
                  }}
                >
                  <div className="debug-frame-name">{frame.name}</div>
                  <div className="mono muted ellipsis">
                    {frame.path}:{frame.line}
                  </div>
                </div>
              ))}
          </div>

          <div className="section-title">
            Breakpoints
            {breakpoints.length > 0 && (
              <button
                className="btn icon"
                title="Remove all breakpoints"
                onClick={() => void act("clear", () => api.clearBreakpoints())}
              >
                ×
              </button>
            )}
          </div>
          <div className="scroll-y" style={{ maxHeight: 160 }}>
            {breakpoints.length === 0 && (
              <div className="empty" style={{ fontSize: "var(--fs-11)" }}>
                Click a line number in the editor.
              </div>
            )}
            {breakpoints.map((item) => (
              <div
                key={`${item.path}:${item.line}`}
                className="debug-breakpoint"
                onClick={() =>
                  void openFileInEditor(item.path, { line: item.actual_line || item.line })
                }
                title={item.message || `${item.path}:${item.line}`}
              >
                <span className={`bp-dot ${item.verified ? "verified" : "pending"}`} />
                <span className="mono ellipsis">
                  {item.path}:{item.line}
                </span>
                {/* The adapter decides where execution can stop. Showing the
                    click position while it stops elsewhere is a small lie. */}
                {item.moved && (
                  <span className="badge warn" title="Moved to the nearest runnable line">
                    →{item.actual_line}
                  </span>
                )}
                {!item.verified && live && (
                  <span className="badge warn" title={item.message}>
                    not bound
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>

        <div className="split-right">
          <div className="section-title">Variables</div>
          <div className="scroll-y" style={{ flex: 1 }}>
            {!stopped && <div className="empty">Variables appear when execution stops.</div>}
            {stopped && selectedFrameId !== null && (
              <Scopes frameId={selectedFrameId} />
            )}
          </div>
          {stopped && (
            <div className="debug-eval">
              <input
                className="input sm"
                placeholder="Evaluate in this frame"
                value={expression}
                onChange={(e) => setExpression(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") void evaluate();
                }}
              />
              {answer && <pre className="mono debug-answer">{answer}</pre>}
            </div>
          )}
          {session && session.output.length > 0 && (
            <>
              <div className="section-title">Console</div>
              <pre className="mono debug-console">{session.output.join("")}</pre>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/** A frame's scopes, each expandable into its variables. */
function Scopes({ frameId }: { frameId: number }) {
  const { data } = useQuery({
    queryKey: ["debug", "scopes", frameId],
    queryFn: () => api.debugScopes(frameId),
  });
  return (
    <>
      {(data?.scopes ?? []).map((scope) => (
        <ScopeRows key={scope.variables_reference} scope={scope} />
      ))}
    </>
  );
}

function ScopeRows({ scope }: { scope: DebugScope }) {
  // Expensive scopes (globals, usually) stay collapsed: the adapter warned
  // that reading them is slow, and expanding by default would make every stop
  // feel like a hang.
  const [open, setOpen] = useState(!scope.expensive);
  const { data } = useQuery({
    queryKey: ["debug", "variables", scope.variables_reference],
    queryFn: () => api.debugVariables(scope.variables_reference),
    enabled: open,
  });
  return (
    <div className="debug-scope">
      <button className="debug-scope-head" onClick={() => setOpen(!open)}>
        {open ? "▾" : "▸"} {scope.name}
        {scope.expensive && <span className="muted"> (slow)</span>}
      </button>
      {open &&
        (data?.variables ?? []).map((variable) => (
          <VariableRow key={variable.name} variable={variable} depth={1} />
        ))}
    </div>
  );
}

function VariableRow({
  variable,
  depth,
}: {
  variable: DebugVariable;
  depth: number;
}) {
  const [open, setOpen] = useState(false);
  const expandable = variable.variables_reference > 0;
  const { data } = useQuery({
    queryKey: ["debug", "variables", variable.variables_reference],
    queryFn: () => api.debugVariables(variable.variables_reference),
    enabled: open && expandable,
  });
  return (
    <>
      <div
        className="debug-variable"
        style={{ paddingLeft: 10 + depth * 12 }}
        onClick={() => expandable && setOpen(!open)}
      >
        <span className="debug-var-name">
          {expandable ? (open ? "▾ " : "▸ ") : ""}
          {variable.name}
        </span>
        <span className="debug-var-value mono" title={variable.value}>
          {variable.value}
        </span>
        {variable.type && <span className="muted">{variable.type}</span>}
      </div>
      {open &&
        (data?.variables ?? []).map((child) => (
          <VariableRow key={child.name} variable={child} depth={depth + 1} />
        ))}
    </>
  );
}

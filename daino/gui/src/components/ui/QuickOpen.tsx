import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { openFileInEditor } from "../../lib/openFile";
import { useQuickOpenStore } from "../../store/quickOpenStore";
import { openTerminalWith } from "../../lib/runTask";
import type { ProjectTask, SymbolInfo } from "../../api/types";

const KIND_MARK: Record<string, string> = {
  class: "C",
  interface: "I",
  struct: "S",
  enum: "E",
  function: "ƒ",
  method: "ƒ",
  constructor: "ƒ",
  variable: "v",
  constant: "k",
  property: "p",
  field: "p",
  module: "M",
  namespace: "N",
};

/**
 * One overlay, three modes: files, symbols, and runnable tasks.
 *
 * A single palette rather than three, because the thing people want is "get me
 * to the thing I am thinking of" and the type of thing is a detail. The mode
 * prefix (`@` for symbols, `>` for tasks) is the convention every editor uses,
 * so it needs no explaining.
 */
export function QuickOpen() {
  const open = useQuickOpenStore((s) => s.open);
  const initial = useQuickOpenStore((s) => s.initialQuery);
  const close = useQuickOpenStore((s) => s.close);
  const [input, setInput] = useState("");
  const [cursor, setCursor] = useState(0);
  const fieldRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      setInput(initial);
      setCursor(0);
      // A palette that opens without focus is a palette that did not open.
      window.setTimeout(() => fieldRef.current?.focus(), 0);
    }
  }, [open, initial]);

  const mode = input.startsWith("@")
    ? "symbols"
    : input.startsWith(">")
      ? "tasks"
      : "files";
  const term = mode === "files" ? input : input.slice(1);

  const { data: symbols } = useQuery({
    queryKey: ["lsp", "workspace-symbols", term],
    queryFn: () => api.workspaceSymbols(term),
    enabled: open && mode === "symbols" && term.length > 0,
  });
  const { data: tasks } = useQuery({
    queryKey: ["tasks"],
    queryFn: api.projectTasks,
    enabled: open && mode === "tasks",
  });
  const { data: files } = useQuery({
    queryKey: ["files", "search", "glob", term],
    // A file search is a text search over paths; the tree endpoint would need
    // a walk per directory.
    queryFn: () => api.searchFiles(term, { include: `*${term}*` }, 1),
    enabled: false,
  });

  const rows = useMemo(() => {
    if (mode === "symbols") return (symbols?.symbols ?? []).slice(0, 60);
    if (mode === "tasks") {
      const all = tasks?.tasks ?? [];
      if (!term) return all.slice(0, 60);
      const needle = term.toLowerCase();
      return all
        .filter(
          (item) =>
            item.label.toLowerCase().includes(needle) ||
            item.command.toLowerCase().includes(needle),
        )
        .slice(0, 60);
    }
    return [];
  }, [mode, symbols, tasks, term]);

  useEffect(() => setCursor(0), [input]);
  if (!open) return null;

  const choose = (index: number) => {
    const row = rows[index];
    if (!row) return;
    close();
    if (mode === "symbols") {
      const symbol = row as SymbolInfo;
      void openFileInEditor(symbol.path, { line: symbol.line });
      return;
    }
    if (mode === "tasks") {
      void openTerminalWith(row as ProjectTask);
    }
  };

  return (
    <div className="dialog-backdrop" onClick={close}>
      <div
        className="quick-open"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-label="Quick open"
      >
        <input
          ref={fieldRef}
          className="input"
          value={input}
          placeholder="Type to search · @ for symbols · > for tasks"
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") close();
            else if (e.key === "ArrowDown") {
              e.preventDefault();
              setCursor((c) => Math.min(c + 1, rows.length - 1));
            } else if (e.key === "ArrowUp") {
              e.preventDefault();
              setCursor((c) => Math.max(c - 1, 0));
            } else if (e.key === "Enter") {
              e.preventDefault();
              choose(cursor);
            }
          }}
        />
        <div className="quick-open-hint">
          {mode === "symbols" && (
            <>
              Symbols
              {symbols?.source === "index" && (
                <span
                  className="badge warn"
                  title="No language server answered, so these come from the
repository index — less precise, and derived from text."
                >
                  from index
                </span>
              )}
            </>
          )}
          {mode === "tasks" && "Run a project command"}
          {mode === "files" && "Prefix with @ for symbols, or > for tasks"}
        </div>
        <div className="quick-open-list">
          {mode === "files" && (
            <div className="empty">
              Use the Explorer for files, <code>@</code> for symbols, or{" "}
              <code>&gt;</code> for tasks.
            </div>
          )}
          {mode !== "files" && rows.length === 0 && (
            <div className="empty">
              {term ? "Nothing matched." : "Type to narrow."}
            </div>
          )}
          {mode === "symbols" &&
            (rows as SymbolInfo[]).map((symbol, index) => (
              <div
                key={`${symbol.path}:${symbol.line}:${symbol.name}`}
                className={`quick-open-row ${index === cursor ? "active" : ""}`}
                onMouseEnter={() => setCursor(index)}
                onClick={() => choose(index)}
              >
                <span className="quick-kind" title={symbol.kind}>
                  {KIND_MARK[symbol.kind] ?? "·"}
                </span>
                <span className="quick-name">{symbol.name}</span>
                {symbol.signature && (
                  <span className="muted ellipsis">{symbol.signature}</span>
                )}
                <span className="grow" />
                <span className="mono muted ellipsis">
                  {symbol.path}:{symbol.line}
                </span>
              </div>
            ))}
          {mode === "tasks" &&
            (rows as ProjectTask[]).map((task, index) => (
              <div
                key={task.id}
                className={`quick-open-row ${index === cursor ? "active" : ""}`}
                onMouseEnter={() => setCursor(index)}
                onClick={() => choose(index)}
              >
                <span className={`quick-kind kind-${task.kind}`} title={task.kind}>
                  {task.kind[0].toUpperCase()}
                </span>
                <span className="quick-name">{task.label}</span>
                <span className="grow" />
                <span className="mono muted ellipsis" title={task.command}>
                  {task.command}
                </span>
                <span className="badge">{task.source}</span>
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}

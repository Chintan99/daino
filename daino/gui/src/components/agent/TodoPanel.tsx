import { useEffect, useState } from "react";
import { api } from "../../api/client";
import { useAgentStore } from "../../store/agentStore";

/**
 * The agent's checklist, under the model picker.
 *
 * Collapsible and quiet when there is nothing to show, because the plan matters
 * while a turn runs and is history afterwards. The stream carries one line per
 * item as it finishes; this is the standing view of the whole list, which is the
 * part the transcript is bad at — a plan re-printed on every update reads as
 * five plans rather than one making progress.
 */
export function TodoPanel() {
  const sessionId = useAgentStore((s) => s.sessionId);
  const todos = useAgentStore((s) => s.todos);
  const applyTodos = useAgentStore((s) => s.applyTodos);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const [open, setOpen] = useState(true);

  /**
   * Seed only while a turn is actually running.
   *
   * The checklist describes work in progress, so showing the last turn's plan
   * on an idle session is worse than showing nothing: it reads as unfinished
   * work that nobody is doing. A reload mid-turn still recovers the plan,
   * because the server reports the turn as running on connect.
   */
  useEffect(() => {
    if (!sessionId || !turnRunning) return;
    let cancelled = false;
    api
      .sessionTodos(sessionId)
      .then((answer) => {
        if (cancelled) return;
        const incoming = (answer.todos as { content?: string; status?: string }[]).map(
          (todo) => ({
            content: String(todo.content ?? ""),
            status: String(todo.status ?? "pending"),
          }),
        );
        if (incoming.length) applyTodos(incoming);
      })
      .catch(() => {
        /* a session with no plan is the normal case */
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, turnRunning, applyTodos]);

  // A new plan during a turn is worth showing without being asked.
  useEffect(() => {
    if (turnRunning && todos.length) setOpen(true);
  }, [turnRunning, todos.length]);

  if (!turnRunning || todos.length === 0) return null;

  const done = todos.filter((todo) => todo.status === "completed").length;
  const current = todos.find((todo) => todo.status === "in_progress");

  return (
    <div className={`todo-panel ${open ? "open" : ""}`}>
      <button className="todo-head" onClick={() => setOpen(!open)}>
        <span className="chev">{open ? "⌄" : "›"}</span>
        <span className="label">Tasks</span>
        <span className="count">
          {done}/{todos.length}
        </span>
        {!open && current && <span className="current">{current.content}</span>}
      </button>
      {open && (
        <div className="todo-list">
          {todos.map((todo) => (
            <div className={`todo-item ${todo.status}`} key={todo.content}>
              <span className="box">
                {todo.status === "completed"
                  ? "✓"
                  : todo.status === "failed"
                    ? "✗"
                    : todo.status === "in_progress"
                      ? "●"
                      : "○"}
              </span>
              <span className="label">{todo.content}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

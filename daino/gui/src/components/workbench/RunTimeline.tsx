import type { RunStep, WorkspaceRun } from "../../api/types";

const GLYPH: Record<RunStep["kind"], string> = {
  run_started: "▶",
  run_finished: "■",
  task_started: "◉",
  task_completed: "✓",
  task_failed: "✕",
  task_skipped: "⤼",
  artifact: "↳",
  source: "↳",
  note: "·",
  steer: "✎",
  approval: "!",
};

/**
 * What the run has actually done, in the order it did it.
 *
 * Written from the persisted timeline rather than the live event stream, so it
 * reads the same tomorrow as it does now. Deliberately sentences, never tool
 * protocol: the audience is someone waiting for a proposal, not someone
 * debugging an agent — the raw calls are already in the agent panel for anyone
 * who wants them.
 */
export function RunTimeline({ run }: { run: WorkspaceRun }) {
  if (run.steps.length === 0) return null;
  return (
    <div className="ws-timeline">
      <div className="section-title">Activity</div>
      <ul className="ws-timeline-list">
        {run.steps.map((step) => (
          <li key={step.id} className={`ws-timeline-step ${step.kind}`}>
            <span className="ws-timeline-glyph">{GLYPH[step.kind] ?? "·"}</span>
            <span className="ws-timeline-text">{step.message}</span>
            {step.kind === "task_completed" && summaryOf(step) && (
              <span className="ws-timeline-detail">{summaryOf(step)}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function summaryOf(step: RunStep): string {
  const summary = step.detail?.summary;
  if (typeof summary !== "string" || !summary.trim()) return "";
  const collapsed = summary.replace(/\s+/g, " ").trim();
  return collapsed.length > 160 ? `${collapsed.slice(0, 159)}…` : collapsed;
}

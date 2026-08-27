import { useUIStore, type InsightsView } from "../../store/uiStore";
import { useQALatest } from "../../api/hooks";
import { ExecutionMapView } from "./ExecutionMapView";
import { LogsView } from "./LogsView";
import { QAView } from "./QAView";
import { MissionsView } from "./MissionsView";
import { CheckpointsView } from "./CheckpointsView";
import { ApprovalsView } from "./ApprovalsView";
import { RepositoryView } from "./RepositoryView";

const VIEWS: { id: InsightsView; label: string; hint: string }[] = [
  { id: "map", label: "MAP", hint: "Per-prompt execution graph, tokens, and cost" },
  { id: "logs", label: "LOGS", hint: "Live activity and the recorded audit log" },
  { id: "qa", label: "QA", hint: "Comprehensive quality scans and their evidence" },
  { id: "missions", label: "MISSIONS", hint: "Planned work and its persisted evidence" },
  { id: "checkpoints", label: "CHECKPOINTS", hint: "Recoverable workspace snapshots" },
  { id: "approvals", label: "APPROVALS", hint: "Every gated decision and its outcome" },
  { id: "repository", label: "REPOSITORY", hint: "Repository intelligence index" },
];

export function InsightsWorkspace() {
  const view = useUIStore((s) => s.insightsView);
  const setView = useUIStore((s) => s.setInsightsView);
  const { data: qa } = useQALatest();

  return (
    <div className="insights">
      <div className="toolbar">
        <div className="segmented">
          {VIEWS.map((v) => (
            <button
              key={v.id}
              className={view === v.id ? "active" : ""}
              onClick={() => setView(v.id)}
              title={v.hint}
            >
              {v.label}
              {v.id === "qa" && qa?.running ? " ●" : ""}
            </button>
          ))}
        </div>
        <span className="grow" />
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          {VIEWS.find((v) => v.id === view)?.hint}
        </span>
      </div>
      <div className="insights-body">
        {view === "map" && <ExecutionMapView />}
        {view === "logs" && <LogsView />}
        {view === "qa" && <QAView />}
        {view === "missions" && <MissionsView />}
        {view === "checkpoints" && <CheckpointsView />}
        {view === "approvals" && <ApprovalsView />}
        {view === "repository" && <RepositoryView />}
      </div>
    </div>
  );
}

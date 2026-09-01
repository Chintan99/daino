import { useUIStore, type InspectorView } from "../../store/uiStore";
import { usePreviewStatus, useQALatest } from "../../api/hooks";
import { ScanView } from "./ScanView";
import { LiveAppView } from "./LiveAppView";

const VIEWS: { id: InspectorView; label: string; hint: string }[] = [
  {
    id: "scan",
    label: "SCAN",
    hint: "End-to-end QA and vulnerability assessment, with a pre-push verdict",
  },
  {
    id: "live",
    label: "LIVE APP",
    hint: "Run the project; the running URL becomes the scan's live target",
  },
];

/**
 * The pre-production check, in one workspace.
 *
 * SCAN produces the verdict — offline audit, the project's own checks, whatever
 * security scanners are installed, and a probe of the running app. LIVE APP is
 * what it probes. They are two views rather than two workspaces because the
 * only reason to run the app here is to inspect it.
 */
export function InspectorWorkspace() {
  const view = useUIStore((s) => s.inspectorView);
  const setView = useUIStore((s) => s.setInspectorView);
  const { data: qa } = useQALatest();
  const { data: preview } = usePreviewStatus(4000);

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
              {v.id === "scan" && qa?.running ? " ●" : ""}
              {v.id === "live" && preview?.running ? " ●" : ""}
            </button>
          ))}
        </div>
        <span className="grow" />
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          {VIEWS.find((v) => v.id === view)?.hint}
        </span>
      </div>
      <div className="insights-body">
        {view === "scan" && <ScanView />}
        {view === "live" && <LiveAppView />}
      </div>
    </div>
  );
}

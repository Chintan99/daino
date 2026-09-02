import { useUIStore, type BottomTab } from "../../store/uiStore";
import { TerminalPanel } from "./TerminalPanel";
import { OutputPanel } from "./OutputPanel";
import { ProblemsPanel } from "./ProblemsPanel";
import { TestsPanel } from "./TestsPanel";
import { DebugPanel } from "./DebugPanel";
import {
  mergedProblems,
  problemCounts,
  useProblemsStore,
} from "../../store/problemsStore";

const TABS: { id: BottomTab; label: string }[] = [
  { id: "terminal", label: "TERMINAL" },
  { id: "output", label: "OUTPUT" },
  { id: "problems", label: "PROBLEMS" },
  { id: "tests", label: "TESTS" },
  { id: "debug", label: "DEBUG" },
];

export function BottomPanel() {
  const tab = useUIStore((s) => s.bottomTab);
  const setTab = useUIStore((s) => s.setBottomTab);
  const setBottomVisible = useUIStore((s) => s.setBottomVisible);
  // A count on the tab is what makes a problem discoverable from another
  // panel. Errors and warnings only: notes and hints are not something to
  // interrupt someone about.
  const { errors, warnings } = useProblemsStore((s) =>
    problemCounts(mergedProblems(s.byPath, s.editorByPath)),
  );

  return (
    <div className="bottom-panel">
      <div className="bottom-tabs">
        {TABS.map((t) => (
          <button
            key={t.id}
            className={`bottom-tab ${tab === t.id ? "active" : ""}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
            {t.id === "problems" && errors > 0 && (
              <span className="tab-count error" title={`${errors} errors`}>
                {errors}
              </span>
            )}
            {t.id === "problems" && errors === 0 && warnings > 0 && (
              <span className="tab-count warning" title={`${warnings} warnings`}>
                {warnings}
              </span>
            )}
          </button>
        ))}
        <span className="grow" />
        <button
          className="btn icon"
          title="Hide panel"
          onClick={() => setBottomVisible(false)}
        >
          ▾
        </button>
      </div>
      <div className="bottom-body">
        {/* Terminal stays mounted to preserve session state; just hidden. */}
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: tab === "terminal" ? "block" : "none",
          }}
        >
          <TerminalPanel />
        </div>
        {tab === "output" && <OutputPanel />}
        {tab === "problems" && <ProblemsPanel />}
        {tab === "tests" && <TestsPanel />}
        {tab === "debug" && <DebugPanel />}
      </div>
    </div>
  );
}

import { useUIStore, type BottomTab } from "../../store/uiStore";
import { TerminalPanel } from "./TerminalPanel";
import { OutputPanel } from "./OutputPanel";
import { ProblemsPanel } from "./ProblemsPanel";
import { TestsPanel } from "./TestsPanel";

const TABS: { id: BottomTab; label: string }[] = [
  { id: "terminal", label: "TERMINAL" },
  { id: "output", label: "OUTPUT" },
  { id: "problems", label: "PROBLEMS" },
  { id: "tests", label: "TESTS" },
];

export function BottomPanel() {
  const tab = useUIStore((s) => s.bottomTab);
  const setTab = useUIStore((s) => s.setBottomTab);
  const setBottomVisible = useUIStore((s) => s.setBottomVisible);

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
      </div>
    </div>
  );
}

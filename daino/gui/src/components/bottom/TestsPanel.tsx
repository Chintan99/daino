import { useAgentStore } from "../../store/agentStore";

export function TestsPanel() {
  const latest = useAgentStore((s) => s.latestTests);

  if (!latest) {
    return (
      <div className="scroll-y" style={{ height: "100%" }}>
        <div className="empty">No test runs yet.</div>
      </div>
    );
  }

  const total = latest.passed_count + latest.failed_count;
  return (
    <div style={{ padding: 16 }}>
      <div
        className="tool-card"
        style={{ borderColor: latest.passed ? "var(--green)" : "var(--red)" }}
      >
        <div className="head">
          <span
            className="mark"
            style={{ color: latest.passed ? "var(--green)" : "var(--red)" }}
          >
            {latest.passed ? "✓" : "✗"}
          </span>
          <span className="tool">
            {latest.passed ? "All tests passed" : "Tests failed"}
          </span>
        </div>
        <div className="detail">
          {latest.passed_count} / {total} passed · {latest.failed_count} failed
        </div>
        <div className="detail muted">
          {new Date(latest.at).toLocaleTimeString()}
        </div>
      </div>
    </div>
  );
}

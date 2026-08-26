import { useGitStatus } from "../api/hooks";
import { useAgentStore } from "../store/agentStore";
import { useEditorStore } from "../store/editorStore";
import { useUIStore } from "../store/uiStore";
import { BRAND } from "../lib/branding";

export function StatusBar() {
  const { data: git } = useGitStatus();
  const wsStatus = useAgentStore((s) => s.wsStatus);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const latestTests = useAgentStore((s) => s.latestTests);
  const activePath = useEditorStore((s) => s.activePath);
  const buffers = useEditorStore((s) => s.buffers);
  const selection = useEditorStore((s) => s.selection);
  const setActivityView = useUIStore((s) => s.setActivityView);
  const setWorkspaceTab = useUIStore((s) => s.setActiveWorkspaceTab);
  const setInsightsView = useUIStore((s) => s.setInsightsView);

  const buf = activePath ? buffers[activePath] : null;
  const changed =
    (git?.staged.length ?? 0) +
    (git?.modified.length ?? 0) +
    (git?.untracked.length ?? 0);

  return (
    <div className="statusbar">
      <span className="seg">
        <span className={`dot-status dot-${wsStatus}`} />
        {wsStatus === "open"
          ? turnRunning
            ? `${BRAND} · working…`
            : `${BRAND} · ready`
          : `${BRAND} · ${wsStatus}`}
      </span>
      {git?.repository && (
        <span
          className="seg click"
          title="Source Control"
          onClick={() => {
            setWorkspaceTab("code");
            setActivityView("scm");
          }}
        >
          ⑃ {git.branch || "detached"}
          {changed > 0 ? ` · ${changed}` : ""}
        </span>
      )}
      {latestTests && (
        <span className="seg">
          <span style={{ color: latestTests.passed ? "var(--green)" : "var(--red)" }}>
            {latestTests.passed ? "✓" : "✗"}
          </span>
          tests {latestTests.passed_count}/
          {latestTests.passed_count + latestTests.failed_count}
        </span>
      )}
      <span
        className="seg click"
        title="Open the execution map"
        onClick={() => {
          setWorkspaceTab("insights");
          setInsightsView("map");
        }}
      >
        ⧉ map
      </span>
      <span className="spacer" />
      {buf && (
        <span className="seg mono">
          {buf.language}
          {selection && selection.path === activePath
            ? `  ·  Ln ${selection.startLine}${
                selection.endLine !== selection.startLine
                  ? `–${selection.endLine}`
                  : ""
              }`
            : ""}
        </span>
      )}
    </div>
  );
}

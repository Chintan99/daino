import { useGitStatus } from "../api/hooks";
import { useAgentStore } from "../store/agentStore";
import { useEditorStore } from "../store/editorStore";

export function StatusBar() {
  const { data: git } = useGitStatus();
  const wsStatus = useAgentStore((s) => s.wsStatus);
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const latestTests = useAgentStore((s) => s.latestTests);
  const activePath = useEditorStore((s) => s.activePath);
  const buffers = useEditorStore((s) => s.buffers);
  const selection = useEditorStore((s) => s.selection);

  const buf = activePath ? buffers[activePath] : null;

  return (
    <div className="statusbar">
      <span className="seg">
        <span className={`dot-status dot-${wsStatus}`} />
        {wsStatus === "open"
          ? turnRunning
            ? "Daino · working…"
            : "Daino · ready"
          : `Daino · ${wsStatus}`}
      </span>
      {git?.repository && (
        <span className="seg" title="Current branch">
          ⑃ {git.branch || "detached"}
        </span>
      )}
      {latestTests && (
        <span className="seg">
          {latestTests.passed ? "✓" : "✗"} tests {latestTests.passed_count}/
          {latestTests.passed_count + latestTests.failed_count}
        </span>
      )}
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

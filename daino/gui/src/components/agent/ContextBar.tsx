import { useAgentStore, type ContextChip } from "../../store/agentStore";
import { useEditorStore } from "../../store/editorStore";
import { useUIStore } from "../../store/uiStore";
import { useTerminalStore } from "../../store/terminalStore";

export function ContextBar() {
  const chips = useAgentStore((s) => s.chips);
  const addChip = useAgentStore((s) => s.addChip);
  const removeChip = useAgentStore((s) => s.removeChip);

  const activePath = useEditorStore((s) => s.activePath);
  const selection = useEditorStore((s) => s.selection);
  const gitDiffPath = useUIStore((s) => s.lastDiffPath);
  const activeTerminal = useTerminalStore((s) => s.activeId);

  const addActiveFile = () => {
    if (!activePath) return;
    addChip({
      id: "active_file",
      kind: "active_file",
      label: `file: ${activePath}`,
      payload: { active_file: activePath },
    });
  };

  const addSelection = () => {
    if (!activePath || !selection || selection.path !== activePath) return;
    addChip({
      id: "selection",
      kind: "selection",
      label: `selection: ${selection.startLine}–${selection.endLine}`,
      payload: {
        active_file: activePath,
        selection: {
          start_line: selection.startLine,
          end_line: selection.endLine,
        },
      },
    });
  };

  const addGitDiff = () => {
    if (!gitDiffPath) return;
    addChip({
      id: "git_diff",
      kind: "git_diff",
      label: `diff: ${gitDiffPath}`,
      payload: { git_diff: gitDiffPath },
    });
  };

  const addTerminal = () => {
    if (!activeTerminal) return;
    addChip({
      id: "terminal",
      kind: "terminal",
      label: "terminal output",
      payload: { terminal: activeTerminal },
    });
  };

  return (
    <div>
      {chips.length > 0 && (
        <div className="context-bar">
          {chips.map((c: ContextChip) => (
            <span key={c.id} className="chip" title={c.label}>
              <span
                style={{
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {c.label}
              </span>
              <span className="x" onClick={() => removeChip(c.id)}>
                ✕
              </span>
            </span>
          ))}
        </div>
      )}
      <div className="row" style={{ flexWrap: "wrap", marginBottom: 8 }}>
        <button
          className="btn subtle"
          disabled={!activePath}
          onClick={addActiveFile}
          title="Add active file to context"
        >
          + File
        </button>
        <button
          className="btn subtle"
          disabled={!selection || selection.path !== activePath}
          onClick={addSelection}
          title="Add selection to context"
        >
          + Selection
        </button>
        <button
          className="btn subtle"
          disabled={!gitDiffPath}
          onClick={addGitDiff}
          title="Add git diff to context"
        >
          + Diff
        </button>
        <button
          className="btn subtle"
          disabled={!activeTerminal}
          onClick={addTerminal}
          title="Add terminal output to context"
        >
          + Terminal
        </button>
      </div>
    </div>
  );
}

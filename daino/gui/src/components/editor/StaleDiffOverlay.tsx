import { DiffEditor } from "@monaco-editor/react";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { useStaleDiffStore } from "../../lib/staleDiff";
import { reloadBuffer, saveBufferOverwriting } from "../../lib/saveFile";
// Registers the Daino themes and the language workers on the monaco
// instance. Imported here rather than at app start so the 4 MB editor
// arrives with the first component that renders one.
import "../../lib/monaco";

/**
 * The two versions of a file that changed underneath an unsaved buffer.
 *
 * Read-only on purpose: this is for deciding, not for merging. Whichever side
 * wins, the loser is still recoverable — Git holds the disk version and the
 * editor holds yours until you act.
 */
export function StaleDiffOverlay() {
  const open = useStaleDiffStore((s) => s.open);
  const close = useStaleDiffStore((s) => s.close);
  const theme = useMonacoTheme();
  const options = useEditorOptions({
    readOnly: true,
    renderSideBySide: true,
    ignoreTrimWhitespace: false,
    renderOverviewRuler: false,
    originalEditable: false,
  });
  if (!open) return null;

  return (
    <div className="dialog-backdrop" onClick={close}>
      <div
        className="dialog"
        onClick={(e) => e.stopPropagation()}
        style={{ width: "min(1100px, 92vw)", height: "min(680px, 84vh)" }}
      >
        <h3>{open.path} — on disk (left) against your unsaved version (right)</h3>
        <div style={{ flex: 1, minHeight: 0 }}>
          <DiffEditor
            original={open.disk}
            modified={open.mine}
            language={open.language}
            theme={theme}
            options={options}
            height="100%"
          />
        </div>
        <div className="actions">
          <button className="btn subtle" onClick={close}>
            Close
          </button>
          <button
            className="btn subtle"
            onClick={() => {
              close();
              void reloadBuffer(open.path);
            }}
          >
            Take the disk version
          </button>
          <button
            className="btn danger"
            onClick={() => {
              close();
              void saveBufferOverwriting(open.path);
            }}
          >
            Keep mine
          </button>
        </div>
      </div>
    </div>
  );
}

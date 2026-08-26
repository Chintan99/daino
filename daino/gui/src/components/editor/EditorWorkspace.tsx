import { useEditorStore } from "../../store/editorStore";
import { reloadBuffer } from "../../lib/saveFile";
import { EditorTabs } from "./EditorTabs";
import { MonacoFileEditor } from "./MonacoFileEditor";

export function EditorWorkspace() {
  const activePath = useEditorStore((s) => s.activePath);
  const hasBuffer = useEditorStore((s) =>
    activePath ? !!s.buffers[activePath] : false,
  );
  const conflict = useEditorStore((s) =>
    activePath ? s.buffers[activePath]?.conflict : false,
  );

  return (
    <div className="panel" style={{ background: "var(--bg-0)" }}>
      <EditorTabs />
      {activePath && conflict && (
        <div className="conflict-bar">
          <span>
            ⚠ This file changed on disk since you opened it. Saving may overwrite
            those changes.
          </span>
          <button
            className="btn danger"
            onClick={() => void reloadBuffer(activePath)}
          >
            Reload from disk
          </button>
        </div>
      )}
      <div className="panel-body" style={{ position: "relative" }}>
        {activePath && hasBuffer ? (
          <MonacoFileEditor path={activePath} />
        ) : (
          <div className="empty">
            Open a file from the Explorer to start editing.
          </div>
        )}
      </div>
    </div>
  );
}

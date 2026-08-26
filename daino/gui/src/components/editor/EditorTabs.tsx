import { useEditorStore } from "../../store/editorStore";

export function EditorTabs() {
  const order = useEditorStore((s) => s.order);
  const buffers = useEditorStore((s) => s.buffers);
  const activePath = useEditorStore((s) => s.activePath);
  const setActive = useEditorStore((s) => s.setActive);
  const closeBuffer = useEditorStore((s) => s.closeBuffer);

  if (order.length === 0) return null;

  return (
    <div className="editor-tabs">
      {order.map((path) => {
        const buf = buffers[path];
        if (!buf) return null;
        return (
          <div
            key={path}
            className={`editor-tab ${activePath === path ? "active" : ""}`}
            onClick={() => setActive(path)}
            title={path}
          >
            <span>{buf.name}</span>
            {buf.dirty ? (
              <span className="dirty" title="Unsaved changes" />
            ) : (
              <span style={{ width: 8 }} />
            )}
            <span
              className="close"
              title="Close"
              onClick={(e) => {
                e.stopPropagation();
                if (
                  !buf.dirty ||
                  window.confirm(`Discard unsaved changes to ${buf.name}?`)
                )
                  closeBuffer(path);
              }}
            >
              ✕
            </span>
          </div>
        );
      })}
    </div>
  );
}

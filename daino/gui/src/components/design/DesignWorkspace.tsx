import { useCallback, useEffect, useState } from "react";
import { ReactFlowProvider } from "reactflow";
import { useDesigns } from "../../api/hooks";
import { useDesignStore } from "../../store/designStore";
import { DesignCanvas } from "./DesignCanvas";
import { DesignToolbar } from "./DesignToolbar";
import { DesignInspector } from "./DesignInspector";
import { ArtifactViewer } from "./ArtifactViewer";

export function DesignWorkspace() {
  const { data, isLoading } = useDesigns();
  const activeId = useDesignStore((s) => s.activeDesignId);
  const setActive = useDesignStore((s) => s.setActiveDesign);
  const viewerNodeId = useDesignStore((s) => s.viewerNodeId);
  const setViewerNode = useDesignStore((s) => s.setViewerNode);
  const [notice, setNotice] = useState("");

  const designs = data?.designs ?? [];

  // Keep a selection that still exists; otherwise open the most recent canvas.
  useEffect(() => {
    if (designs.length === 0) return;
    if (!activeId || !designs.some((d) => d.id === activeId))
      setActive(designs[0].id);
  }, [activeId, designs, setActive]);

  const onNotice = useCallback((message: string) => {
    setNotice(message);
    window.setTimeout(() => setNotice(""), 6000);
  }, []);

  return (
    <div className="design-workspace">
      <DesignToolbar designId={activeId} onNotice={onNotice} />
      {notice && (
        <div
          className="conflict-bar"
          style={{
            background: "var(--accent-soft)",
            borderColor: "var(--accent-line)",
            color: "var(--accent-bright)",
          }}
        >
          {notice}
        </div>
      )}
      <div className="design-main">
        {isLoading && <div className="empty" style={{ margin: "auto" }}>Loading…</div>}
        {!isLoading && !activeId && (
          <div className="canvas-hint" style={{ position: "relative" }}>
            <div className="big">No canvas yet</div>
            <div>
              Select <strong>+ Canvas</strong> for a blank sheet, or{" "}
              <strong>From code</strong> to sketch this repository's
              architecture.
            </div>
          </div>
        )}
        {activeId && (
          <>
            <ReactFlowProvider key={activeId}>
              <DesignCanvas designId={activeId} onNotice={onNotice} />
            </ReactFlowProvider>
            <DesignInspector designId={activeId} onNotice={onNotice} />
          </>
        )}
      </div>

      {activeId && viewerNodeId && (
        <ArtifactViewer
          key={viewerNodeId}
          designId={activeId}
          nodeId={viewerNodeId}
          onClose={() => setViewerNode(null)}
          onNotice={onNotice}
        />
      )}
    </div>
  );
}

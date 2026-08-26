import { useEffect, useState } from "react";
import { useDesign, useDesignMutations } from "../../api/hooks";
import { useDesignStore } from "../../store/designStore";
import { useAgentStore } from "../../store/agentStore";
import { sendChatMessage } from "../../lib/agent";

export function DesignInspector({ designId }: { designId: string }) {
  const { data: design } = useDesign(designId);
  const m = useDesignMutations(designId);
  const selectedNodeIds = useDesignStore((s) => s.selectedNodeIds);
  const addChip = useAgentStore((s) => s.addChip);
  const removeChip = useAgentStore((s) => s.removeChip);

  const selected =
    design && selectedNodeIds.length === 1
      ? design.nodes.find((n) => n.id === selectedNodeIds[0])
      : undefined;

  const [label, setLabel] = useState("");
  useEffect(() => {
    setLabel(selected?.label ?? "");
  }, [selected?.id, selected?.label]);

  // selecting node(s) adds/updates a design-node context chip
  useEffect(() => {
    if (selectedNodeIds.length > 0) {
      addChip({
        id: "design_node",
        kind: "design_node",
        label: `design: ${selectedNodeIds.join(", ")}`,
        payload: {
          workspace: "design",
          design_id: designId,
          selected_nodes: selectedNodeIds,
        },
      });
    } else {
      removeChip("design_node");
    }
  }, [selectedNodeIds, designId, addChip, removeChip]);

  const implement = () => {
    sendChatMessage(
      `Implement this design (id: ${designId}). First inspect the repository and propose a plan before writing any code.`,
      { withContext: false },
    );
  };

  return (
    <div className="design-inspector">
      <div className="panel-header" style={{ paddingLeft: 0 }}>
        Inspector
      </div>

      <button
        className="btn primary"
        style={{ width: "100%", marginBottom: 14 }}
        onClick={implement}
        title="Ask Daino to implement this design"
      >
        Implement Design
      </button>

      {!selected && (
        <div className="muted" style={{ fontSize: 12 }}>
          {selectedNodeIds.length > 1
            ? `${selectedNodeIds.length} nodes selected`
            : "Select a node to edit its properties."}
        </div>
      )}

      {selected && (
        <>
          <div className="field">
            <label>Node ID</label>
            <div className="mono" style={{ fontSize: 11 }}>
              {selected.id}
            </div>
          </div>
          <div className="field">
            <label>Type</label>
            <div>{selected.type || "default"}</div>
          </div>
          <div className="field">
            <label>Label</label>
            <input
              className="input"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              onBlur={() => {
                if (label !== selected.label)
                  m.patchNode.mutate({
                    nodeId: selected.id,
                    body: { label },
                  });
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              }}
            />
          </div>
          <div className="field">
            <label>Position</label>
            <div className="mono" style={{ fontSize: 11 }}>
              x {Math.round(selected.position.x)}, y{" "}
              {Math.round(selected.position.y)}
            </div>
          </div>
          <button
            className="btn danger"
            style={{ width: "100%" }}
            onClick={() => m.deleteNode.mutate(selected.id)}
          >
            Delete Node
          </button>
        </>
      )}
    </div>
  );
}

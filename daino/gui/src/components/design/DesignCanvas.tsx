import { useCallback, useEffect, useRef, useState } from "react";
import ReactFlow, {
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  type OnSelectionChangeParams,
} from "reactflow";
import { useDesign, useDesignMutations } from "../../api/hooks";
import { api } from "../../api/client";
import { useQueryClient } from "@tanstack/react-query";
import { qk } from "../../api/hooks";
import { useDesignStore } from "../../store/designStore";
import { importFile } from "../../lib/designImport";
import { NODE_TYPES } from "./CanvasNodes";
import { hasMoved, preserveInteractionState } from "./canvasSync";
import type { Design, DesignNode, DesignEdge } from "../../api/types";
import { BRAND } from "../../lib/branding";

function toFlowNode(n: DesignNode): Node {
  const data = (n.data ?? {}) as Record<string, unknown>;
  const isArtifact = n.type === "artifact" || typeof data.kind === "string";
  return {
    id: n.id,
    position: { x: n.position.x, y: n.position.y },
    type: isArtifact ? "artifact" : "box",
    data: { ...data, label: n.label, nodeType: n.type },
    // Artifacts size themselves; a box hugs its label.
    style: isArtifact ? { width: undefined, height: undefined } : undefined,
  };
}

function toFlowEdge(e: DesignEdge): Edge {
  return { id: e.id, source: e.source, target: e.target, label: e.label };
}

export function DesignCanvas({
  designId,
  onNotice,
}: {
  designId: string;
  onNotice: (message: string) => void;
}) {
  const qc = useQueryClient();
  const { data: design } = useDesign(designId);
  const m = useDesignMutations(designId);
  const setSelectedNodes = useDesignStore((s) => s.setSelectedNodes);
  const setViewerNode = useDesignStore((s) => s.setViewerNode);
  const { screenToFlowPosition } = useReactFlow();

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const [dropping, setDropping] = useState(false);
  const dragDepth = useRef(0);

  // Sync from the server design whenever it changes (manual or agent edits),
  // carrying across the selection so a refetch does not undo a click.
  useEffect(() => {
    if (!design) return;
    const incoming = design.nodes.map(toFlowNode);
    setNodes((current) => preserveInteractionState(current, incoming));
    setEdges(design.edges.map(toFlowEdge));
  }, [design, setNodes, setEdges]);

  const onConnect = (c: Connection) => {
    if (c.source && c.target)
      m.addEdge.mutate({ source: c.source, target: c.target });
  };

  const onNodeDragStop = (_e: unknown, node: Node) => {
    // A click arrives here as a zero-distance drag; writing it back would bump
    // the document version and re-sync the canvas out from under the selection.
    if (!hasMoved(design?.nodes.find((item) => item.id === node.id), node)) return;
    m.patchNode.mutate({
      nodeId: node.id,
      body: { x: Math.round(node.position.x), y: Math.round(node.position.y) },
    });
  };

  const onNodesDelete = (deleted: Node[]) => {
    for (const n of deleted) m.deleteNode.mutate(n.id);
  };

  const onEdgesDelete = (deleted: Edge[]) => {
    for (const e of deleted) m.deleteEdge.mutate(e.id);
  };

  const onSelectionChange = (params: OnSelectionChangeParams) => {
    setSelectedNodes(params.nodes.map((n) => n.id));
  };

  /** Add every dropped file, then refresh once rather than per file. */
  const handleFiles = useCallback(
    async (files: File[], origin: { x: number; y: number }) => {
      let latest: Design | null = null;
      let openAfterImport: string | null = null;
      const problems: string[] = [];
      let index = 0;
      for (const file of files) {
        const result = await importFile(file);
        const x = Math.round(origin.x + index * 36);
        const y = Math.round(origin.y + index * 36);
        index += 1;
        if (result.type === "error") {
          problems.push(result.message);
          continue;
        }
        if (result.type === "artifact") {
          const a = result.artifact;
          const previousIds = new Set((latest ?? design)?.nodes.map((n) => n.id) ?? []);
          latest = await api.addNode(designId, {
            label: a.label,
            node_type: "artifact",
            x,
            y,
            data: {
              kind: a.kind,
              content: a.content,
              src: a.src,
              filename: a.filename,
              width: a.width,
              height: a.height,
            },
          });
          if (a.kind === "html" || a.kind === "svg") {
            const added = latest.nodes.find((n) => !previousIds.has(n.id));
            if (added) openAfterImport = added.id;
          }
          continue;
        }
        // A design export: bring its nodes and edges across, remapping ids.
        const idMap = new Map<string, string>();
        for (const [i, node] of result.fragment.nodes.entries()) {
          const created = await api.addNode(designId, {
            label: String(node.label ?? node.id ?? `node ${i + 1}`),
            node_type: String(node.type ?? "default"),
            x: Math.round(x + (node.position?.x ?? 0)),
            y: Math.round(y + (node.position?.y ?? 0)),
            data: (node.data ?? {}) as Record<string, unknown>,
          });
          const added = created.nodes[created.nodes.length - 1];
          if (node.id) idMap.set(node.id, added.id);
          latest = created;
        }
        for (const edge of result.fragment.edges) {
          const source = idMap.get(edge.source);
          const target = idMap.get(edge.target);
          if (!source || !target) continue;
          latest = await api.addEdge(designId, {
            source,
            target,
            label: edge.label ?? "",
          });
        }
        onNotice(`Imported ${result.name}.`);
      }
      if (latest) {
        qc.setQueryData(qk.design(designId), latest);
        void qc.invalidateQueries({ queryKey: qk.designs });
      }
      if (openAfterImport) setViewerNode(openAfterImport);
      if (problems.length) onNotice(problems.join(" "));
    },
    [design, designId, onNotice, qc, setViewerNode],
  );

  const onDrop = (event: React.DragEvent) => {
    event.preventDefault();
    dragDepth.current = 0;
    setDropping(false);
    const files = Array.from(event.dataTransfer.files ?? []);
    const position = screenToFlowPosition({
      x: event.clientX,
      y: event.clientY,
    });
    if (files.length) {
      void handleFiles(files, position);
      return;
    }
    // Plain text dropped from another app becomes a note.
    const text = event.dataTransfer.getData("text/plain");
    if (text.trim()) {
      void api
        .addNode(designId, {
          label: "Note",
          node_type: "artifact",
          x: Math.round(position.x),
          y: Math.round(position.y),
          data: {
            kind: "text",
            content: text,
            filename: "note.txt",
            width: 300,
            height: 190,
          },
        })
        .then((d) => qc.setQueryData(qk.design(designId), d));
    }
  };

  return (
    <div
      className={`design-canvas ${dropping ? "dropping" : ""}`}
      onDragEnter={(e) => {
        e.preventDefault();
        dragDepth.current += 1;
        setDropping(true);
      }}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={() => {
        dragDepth.current = Math.max(0, dragDepth.current - 1);
        if (dragDepth.current === 0) setDropping(false);
      }}
      onDrop={onDrop}
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={NODE_TYPES}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeDragStop={onNodeDragStop}
        onNodesDelete={onNodesDelete}
        onEdgesDelete={onEdgesDelete}
        onSelectionChange={onSelectionChange}
        proOptions={{ hideAttribution: true }}
        deleteKeyCode={["Backspace", "Delete"]}
        minZoom={0.1}
        maxZoom={2.5}
        fitView
        fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          color="#262b28"
          gap={22}
          size={1}
        />
        <Controls showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          style={{ background: "#101211" }}
          maskColor="rgba(0,0,0,0.65)"
          nodeColor="#2d5340"
          nodeStrokeColor="#2a2f2c"
        />
      </ReactFlow>

      {nodes.length === 0 && (
        <div className="canvas-hint">
          <div className="big">Blank canvas</div>
          <div>
            Drop an <kbd>.html</kbd>, <kbd>.svg</kbd>, image, note, or exported
            design onto the canvas — or ask {BRAND} in the side panel to draw one.
          </div>
        </div>
      )}
    </div>
  );
}

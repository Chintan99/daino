import { useEffect } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  type Connection,
  type OnSelectionChangeParams,
} from "reactflow";
import { useDesign, useDesignMutations } from "../../api/hooks";
import { useDesignStore } from "../../store/designStore";

function toNode(n: {
  id: string;
  label: string;
  position: { x: number; y: number };
}): Node {
  return {
    id: n.id,
    position: { x: n.position.x, y: n.position.y },
    data: { label: n.label },
    type: "default",
  };
}

function toEdge(e: {
  id: string;
  source: string;
  target: string;
  label?: string;
}): Edge {
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    label: e.label,
  };
}

export function DesignCanvas({ designId }: { designId: string }) {
  const { data: design } = useDesign(designId);
  const m = useDesignMutations(designId);
  const setSelectedNodes = useDesignStore((s) => s.setSelectedNodes);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);

  // sync from the server design whenever it changes (manual or agent edits)
  useEffect(() => {
    if (!design) return;
    setNodes(design.nodes.map(toNode));
    setEdges(design.edges.map(toEdge));
  }, [design, setNodes, setEdges]);

  const onConnect = (c: Connection) => {
    if (c.source && c.target)
      m.addEdge.mutate({ source: c.source, target: c.target });
  };

  const onNodeDragStop = (_e: unknown, node: Node) => {
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

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onConnect={onConnect}
      onNodeDragStop={onNodeDragStop}
      onNodesDelete={onNodesDelete}
      onEdgesDelete={onEdgesDelete}
      onSelectionChange={onSelectionChange}
      fitView
      proOptions={{ hideAttribution: true }}
      deleteKeyCode={["Backspace", "Delete"]}
    >
      <Background color="#232a36" gap={18} />
      <Controls />
      <MiniMap
        pannable
        zoomable
        style={{ background: "#10141c" }}
        maskColor="rgba(0,0,0,0.6)"
        nodeColor="#2f3948"
      />
    </ReactFlow>
  );
}

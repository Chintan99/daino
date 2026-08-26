import { useEffect, useRef, useState } from "react";
import { Handle, Position, type NodeProps } from "reactflow";
import ReactMarkdown from "react-markdown";
import { useDesignMutations } from "../../api/hooks";
import { useDesignStore } from "../../store/designStore";

export interface ArtifactData {
  kind?: string;
  label?: string;
  content?: string;
  src?: string;
  filename?: string;
  width?: number;
  height?: number;
  nodeType?: string;
}

const MIN_W = 160;
const MIN_H = 90;

/**
 * A dropped file living on the canvas.
 *
 * The preview is inert until the reader asks for it: an HTML frame that eats
 * pointer events would make the canvas impossible to pan across, so frames
 * start pass-through and the header's ◉ toggle hands them the mouse.
 */
export function ArtifactNode({ id, data, selected }: NodeProps<ArtifactData>) {
  const designId = useDesignStore((s) => s.activeDesignId);
  const setSourceNode = useDesignStore((s) => s.setSourceNode);
  const setViewerNode = useDesignStore((s) => s.setViewerNode);
  const m = useDesignMutations(designId);
  const [interactive, setInteractive] = useState(false);
  const [size, setSize] = useState({
    w: data.width ?? 420,
    h: data.height ?? 300,
  });
  const dragRef = useRef<{ x: number; y: number; w: number; h: number } | null>(
    null,
  );

  // Follow server-side size changes (the agent can resize a node too).
  useEffect(() => {
    setSize({ w: data.width ?? 420, h: data.height ?? 300 });
  }, [data.width, data.height]);

  const onResizeStart = (e: React.PointerEvent) => {
    e.stopPropagation();
    e.preventDefault();
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    dragRef.current = { x: e.clientX, y: e.clientY, w: size.w, h: size.h };
  };

  const onResizeMove = (e: React.PointerEvent) => {
    const start = dragRef.current;
    if (!start) return;
    setSize({
      w: Math.max(MIN_W, start.w + (e.clientX - start.x)),
      h: Math.max(MIN_H, start.h + (e.clientY - start.y)),
    });
  };

  const onResizeEnd = (e: React.PointerEvent) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    (e.target as HTMLElement).releasePointerCapture(e.pointerId);
    if (designId)
      m.patchNode.mutate({
        nodeId: id,
        body: { data: { ...data, width: Math.round(size.w), height: Math.round(size.h) } },
      });
  };

  const kind = data.kind ?? "text";
  const frameable = kind === "html" || kind === "svg";

  return (
    <div
      className={`dnode ${selected ? "selected" : ""}`}
      style={{ width: size.w, height: size.h }}
      onDoubleClick={() => setViewerNode(id)}
      title="Double-click to open full screen"
    >
      <Handle type="target" position={Position.Left} />
      <div className="dnode-head">
        <span>{kind}</span>
        <span className="name" title={data.filename || data.label}>
          {data.label || data.filename || "artifact"}
        </span>
        {frameable && (
          <button
            className="act nodrag"
            title={interactive ? "Stop interacting" : "Interact with this preview"}
            onClick={(e) => {
              e.stopPropagation();
              setInteractive(!interactive);
            }}
          >
            {interactive ? "◉" : "○"}
          </button>
        )}
        {kind !== "image" && (
          <button
            className="act nodrag"
            title="Edit source in the inspector"
            onClick={(e) => {
              e.stopPropagation();
              setSourceNode(id);
            }}
          >
            {"</>"}
          </button>
        )}
        <button
          className="act nodrag"
          title="Open full screen"
          onClick={(e) => {
            e.stopPropagation();
            setViewerNode(id);
          }}
        >
          ⛶
        </button>
      </div>

      <div
        className="dnode-body nodrag nowheel"
        style={{ pointerEvents: interactive ? "auto" : "none" }}
      >
        {frameable && (
          <iframe
            title={data.label || id}
            sandbox="allow-scripts"
            srcDoc={data.content ?? ""}
          />
        )}
        {kind === "image" && (
          <div className="svg-host">
            <img src={data.src ?? ""} alt={data.label || "image"} />
          </div>
        )}
        {kind === "markdown" && (
          <div className="note">
            <ReactMarkdown>{data.content ?? ""}</ReactMarkdown>
          </div>
        )}
        {kind === "text" && <div className="note mono">{data.content ?? ""}</div>}
      </div>

      <div
        className="resize nodrag"
        onPointerDown={onResizeStart}
        onPointerMove={onResizeMove}
        onPointerUp={onResizeEnd}
        title="Resize"
      />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

/** A plain architecture box: the diagram primitive the agent's tools emit. */
export function BoxNode({ data, selected }: NodeProps<ArtifactData>) {
  const type = data.nodeType && data.nodeType !== "default" ? data.nodeType : "";
  return (
    <div className={`dnode box ${selected ? "selected" : ""}`}>
      <Handle type="target" position={Position.Left} />
      {type && <div className="kindline">{type}</div>}
      <div>{data.label || "node"}</div>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

export const NODE_TYPES = { artifact: ArtifactNode, box: BoxNode };

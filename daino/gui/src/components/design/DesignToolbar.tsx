import { useDesign, useDesignMutations } from "../../api/hooks";
import { exportHTML, exportJSON, exportSVG } from "../../lib/exportDesign";

export function DesignToolbar({ designId }: { designId: string }) {
  const { data: design } = useDesign(designId);
  const m = useDesignMutations(designId);

  const addNode = () => {
    m.addNode.mutate({
      label: "New Node",
      node_type: "default",
      x: Math.round(120 + Math.random() * 240),
      y: Math.round(80 + Math.random() * 200),
    });
  };

  return (
    <div className="design-toolbar">
      <button className="btn" onClick={addNode} disabled={m.addNode.isPending}>
        + Node
      </button>
      <span className="muted" style={{ fontSize: 12 }}>
        {design ? `${design.name} · v${design.version}` : ""}
      </span>
      <span className="grow" />
      <span className="muted" style={{ fontSize: 11 }}>
        Export
      </span>
      <button
        className="btn subtle"
        disabled={!design}
        onClick={() => design && exportJSON(design)}
      >
        JSON
      </button>
      <button
        className="btn subtle"
        disabled={!design}
        onClick={() => design && exportSVG(design)}
      >
        SVG
      </button>
      <button
        className="btn subtle"
        disabled={!design}
        onClick={() => design && exportHTML(design)}
      >
        HTML
      </button>
    </div>
  );
}

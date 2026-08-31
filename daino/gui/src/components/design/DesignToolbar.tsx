import { useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { qk, useDesign, useDesignMutations, useDesigns } from "../../api/hooks";
import { api } from "../../api/client";
import { useDesignStore } from "../../store/designStore";
import { confirmFor } from "../../store/dialogStore";
import {
  exportHTML,
  exportJSON,
  exportPrototypeZip,
  exportSVG,
} from "../../lib/exportDesign";
import { Menu } from "../ui/Menu";
import { importFile } from "../../lib/designImport";

export function DesignToolbar({
  designId,
  onNotice,
}: {
  designId: string | null;
  onNotice: (message: string) => void;
}) {
  const qc = useQueryClient();
  const { data: designs } = useDesigns();
  const { data: design } = useDesign(designId);
  const m = useDesignMutations(designId);
  const setActive = useDesignStore((s) => s.setActiveDesign);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement | null>(null);

  const newCanvas = async () => {
    setBusy(true);
    try {
      const name = `Canvas ${(designs?.designs.length ?? 0) + 1}`;
      const created = await api.createDesign(name, "prototype");
      qc.setQueryData(qk.design(created.id), created);
      await qc.invalidateQueries({ queryKey: qk.designs });
      setActive(created.id);
    } finally {
      setBusy(false);
    }
  };

  const generate = async () => {
    setBusy(true);
    try {
      const created = await api.generateDesignFromCode();
      qc.setQueryData(qk.design(created.id), created);
      await qc.invalidateQueries({ queryKey: qk.designs });
      setActive(created.id);
      onNotice("Generated an architecture sketch from the codebase.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!designId || !design) return;
    // CONFIRM: deleting a canvas removes its artifacts too.
    const ok = await confirmFor({
      title: "Delete canvas",
      message: `Delete the canvas "${design.name}" and everything on it? This cannot be undone.`,
      confirmLabel: "Delete",
      danger: true,
    });
    if (!ok) return;
    await api.deleteDesign(designId);
    setActive(null);
    await qc.invalidateQueries({ queryKey: qk.designs });
  };

  const pickFiles = async (files: FileList | null) => {
    if (!files?.length || !designId) return;
    let index = 0;
    for (const file of Array.from(files)) {
      const result = await importFile(file);
      if (result.type === "error") {
        onNotice(result.message);
        continue;
      }
      if (result.type !== "artifact") {
        onNotice(`${file.name} is a design export — drop it on the canvas to place it.`);
        continue;
      }
      const a = result.artifact;
      const updated = await api.addNode(designId, {
        label: a.label,
        node_type: "artifact",
        x: 60 + index * 40,
        y: 60 + index * 40,
        data: {
          kind: a.kind,
          content: a.content,
          src: a.src,
          filename: a.filename,
          width: a.width,
          height: a.height,
        },
      });
      qc.setQueryData(qk.design(designId), updated);
      index += 1;
    }
    if (fileRef.current) fileRef.current.value = "";
  };

  return (
    <div className="design-toolbar">
      <span
        className="canvas-title"
        title={design?.name ?? "Canvas"}
        style={{ maxWidth: 220 }}
      >
        {design?.name ?? "No canvas"}
      </span>
      <button className="btn" disabled={busy} onClick={() => void newCanvas()}>
        + Canvas
      </button>
      <button
        className="btn"
        disabled={!designId || m.addNode.isPending}
        onClick={() =>
          m.addNode.mutate({
            label: "New node",
            node_type: "service",
            x: Math.round(80 + Math.random() * 260),
            y: Math.round(60 + Math.random() * 200),
          })
        }
        title="Add an architecture box"
      >
        + Node
      </button>
      <button
        className="btn"
        disabled={!designId}
        onClick={() => fileRef.current?.click()}
        title="Import HTML, SVG, images, notes, or an exported design"
      >
        Import…
      </button>
      <input
        ref={fileRef}
        type="file"
        multiple
        hidden
        accept=".html,.htm,.svg,.md,.markdown,.txt,.json,.mmd,.mermaid,.yaml,.yml,.puml,image/*"
        onChange={(e) => void pickFiles(e.target.files)}
      />
      <button className="btn" disabled={busy} onClick={() => void generate()}>
        From code
      </button>

      <span className="grow" />
      {design && (
        <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
          {design.nodes.length} nodes · {design.edges.length} edges · v
          {design.version}
        </span>
      )}
      <Menu
        label="Export"
        title="Export this canvas"
        items={[
          {
            label: "Prototype bundle (.zip)",
            hint: "index.html, every page and asset, and design.json",
            disabled: !design,
            onSelect: () => design && exportPrototypeZip(design),
          },
          {
            label: "Standalone page (.html)",
            hint: "One page reproducing the canvas layout",
            disabled: !design,
            onSelect: () => design && exportHTML(design),
          },
          {
            label: "Vector image (.svg)",
            disabled: !design,
            onSelect: () => design && exportSVG(design),
          },
          {
            label: "Design document (.json)",
            hint: "Drop it on another canvas to restore it",
            disabled: !design,
            onSelect: () => design && exportJSON(design),
          },
        ]}
      />
      <button
        className="btn icon"
        disabled={!designId}
        title="Delete this canvas"
        onClick={() => void remove()}
      >
        ✕
      </button>
    </div>
  );
}

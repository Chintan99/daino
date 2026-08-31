import { useCallback, useMemo } from "react";
import { useDesign, useDesignMutations } from "../../api/hooks";
import { api, ApiError } from "../../api/client";
import { download, exportArtifact, exportPrototypeZip } from "../../lib/exportDesign";
import type { MenuItem } from "../ui/Menu";
import { VisualHtmlEditor, type EditorChip } from "./VisualHtmlEditor";

/**
 * The full-screen editor for one canvas artifact.
 *
 * A thin adapter: it maps a design node onto {@link VisualHtmlEditor}, saving
 * edits back to the node and exporting through the design library.
 */
export function ArtifactViewer({
  designId,
  nodeId,
  onClose,
  onNotice,
}: {
  designId: string;
  nodeId: string;
  onClose: () => void;
  onNotice: (message: string) => void;
}) {
  const { data: design } = useDesign(designId);
  const m = useDesignMutations(designId);
  const node = design?.nodes.find((n) => n.id === nodeId);
  const data = useMemo(
    () => (node?.data ?? {}) as Record<string, unknown>,
    [node?.data],
  );
  const kind = String(data.kind ?? "text");
  const saved = String(data.content ?? "");
  const filename = String(data.filename || "");
  const title = node?.label || filename || "artifact";

  const onSave = useCallback(
    (html: string) => {
      m.patchNode.mutate({ nodeId, body: { data: { ...data, content: html } } });
    },
    [data, m, nodeId],
  );

  const saveToProject = useCallback(
    async (draft: string) => {
      const suggested = filename || `${node?.label || "artifact"}.html`;
      const path = window.prompt("Save to project path:", suggested);
      if (!path?.trim()) return;
      try {
        try {
          await api.createFile(path.trim(), false);
        } catch (err) {
          if (!(err instanceof ApiError && err.status === 409)) throw err;
        }
        const current = await api.readFile(path.trim());
        await api.writeFile(path.trim(), draft, current.hash);
        onNotice(`Saved ${path.trim()}.`);
      } catch (err) {
        onNotice(`Could not save: ${err instanceof Error ? err.message : String(err)}`);
      }
    },
    [filename, node?.label, onNotice],
  );

  const openInTab = useCallback(
    (draft: string) => {
      const blob = new Blob([draft], {
        type: kind === "svg" ? "image/svg+xml" : "text/html",
      });
      const url = URL.createObjectURL(blob);
      window.open(url, "_blank", "noopener");
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    },
    [kind],
  );

  const exportItems = useCallback(
    (draft: string): MenuItem[] => [
      {
        label: `Download ${filename || "file"}`,
        onSelect: () =>
          node && exportArtifact({ ...node, data: { ...data, content: draft } }),
      },
      {
        label: "Prototype bundle (.zip)",
        hint: "This page as index.html, plus every other artifact",
        onSelect: () => design && exportPrototypeZip(design, nodeId),
      },
      { label: "Save to project…", onSelect: () => void saveToProject(draft) },
      {
        label: "Open in a new tab",
        disabled: !(kind === "html" || kind === "svg"),
        onSelect: () => openInTab(draft),
      },
      {
        label: "Copy source",
        disabled: kind === "image",
        onSelect: () => {
          void navigator.clipboard.writeText(draft);
          onNotice("Source copied to the clipboard.");
        },
      },
      {
        label: "Standalone page (.html)",
        hint: "Wrap the source in a complete document",
        disabled: kind === "image",
        onSelect: () =>
          download(
            `${node?.label || "artifact"}.html`,
            /<html[\s>]/i.test(draft)
              ? draft
              : `<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>${node?.label || "artifact"}</title>\n</head><body>\n${draft}\n</body></html>\n`,
            "text/html",
          ),
      },
    ],
    [data, design, filename, kind, node, nodeId, onNotice, openInTab, saveToProject],
  );

  const chip: EditorChip = useMemo(
    () => ({
      id: "design_artifact",
      kind: "design_node",
      label: `page: ${filename || nodeId}`,
      payload: {
        workspace: "design",
        design_id: designId,
        node_id: nodeId,
        artifact: filename,
        artifact_kind: kind,
      },
    }),
    [designId, nodeId, filename, kind],
  );

  if (!node) return null;

  return (
    <VisualHtmlEditor
      sourceKey={nodeId}
      title={title}
      filename={filename}
      kind={kind}
      imageSrc={String(data.src ?? "")}
      saved={saved}
      savePending={m.patchNode.isPending}
      onSave={onSave}
      onClose={onClose}
      onNotice={onNotice}
      chip={chip}
      exportItems={exportItems}
    />
  );
}

import { useCallback, useMemo } from "react";
import { useDesign, useDesignMutations } from "../../api/hooks";
import { api, ApiError } from "../../api/client";
import { download, exportArtifact, exportPrototypeZip } from "../../lib/exportDesign";
import { sendChatMessage } from "../../lib/agent";
import { promptFor } from "../../store/dialogStore";
import { BRAND } from "../../lib/branding";
import type { ElementInfo } from "../../lib/visualEditor";
import type { MenuItem } from "../ui/Menu";
import { VisualHtmlEditor, type EditorChip } from "./VisualHtmlEditor";

// What each quick action asks the agent to do to the selected element.
const ELEMENT_TASKS: Record<string, string> = {
  match:
    "Restyle this element so its visual design matches the rest of the page — reuse the page's existing colours, typography, spacing, and the styling of similar buttons, cards, and sections. Do not change its text content or structure; adjust only classes and/or inline styles. If the page uses a CSS framework or utility classes, follow that same convention.",
  improve:
    "Improve this component's visual design and UX while keeping it consistent with the rest of the page — refine layout, spacing, hierarchy, and interactive states, but keep its purpose and core content.",
  responsive:
    "Make this element responsive so it reads well on mobile, tablet, and desktop, consistent with how the rest of the page handles responsiveness.",
  fill:
    "Replace the placeholder text and image slots in this element with real, specific, context-appropriate content for this project — draw on the repository's purpose and existing pages. Keep the layout and styling; change only the content (text, headings, list items, and image src/alt). Prefer real copy over lorem ipsum.",
};

// What each whole-page action asks the agent to do.
const PAGE_TASKS: Record<string, string> = {
  polish:
    "Polish the visual design of this entire page — improve spacing, alignment, visual hierarchy, typography, and consistency — without changing its content or purpose.",
  responsive:
    "Make this entire page fully responsive across mobile, tablet, and desktop, adding the necessary responsive CSS or utility classes while preserving its content and its look on desktop.",
  a11y:
    "Improve the accessibility of this entire page: semantic HTML, image alt text, form labels, colour contrast, focus states, and ARIA where needed — without changing its visible content or design intent.",
};

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
      const path = await promptFor({
        title: "Save to project",
        hint: "Path relative to the project root",
        initial: suggested,
        confirmLabel: "Save",
      });
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

  const onElementAsk = useCallback(
    (
      kind: "match" | "improve" | "responsive" | "fill" | "ask",
      selection: ElementInfo,
      freeText?: string,
    ) => {
      const instruction = kind === "ask" ? (freeText ?? "").trim() : ELEMENT_TASKS[kind];
      if (!instruction) return;
      const prompt =
        `You are editing the page "${title}" (design ${designId}, node ${nodeId}).\n\n` +
        `Task: ${instruction}\n\n` +
        "The specific element to change is:\n```html\n" +
        selection.html +
        "\n```\n\n" +
        "This is a small, direct edit. Do NOT make a plan or create todos, and do NOT read other files. " +
        `In one step: read the node once with read_design_artifact (design ${designId}, node ${nodeId}), ` +
        "locate this exact element, apply the change, and save with update_design_node. Change nothing else.";
      if (!sendChatMessage(prompt, { withContext: false }))
        onNotice(`${BRAND} is busy — wait for the current turn to finish, then try again.`);
      else onNotice(`Asked ${BRAND} to update this element…`);
    },
    [designId, nodeId, title, onNotice],
  );

  const onPageAsk = useCallback(
    (
      kind: "polish" | "responsive" | "theme" | "a11y" | "generate",
      freeText?: string,
      anchorHtml?: string,
    ) => {
      let instruction = "";
      if (kind === "theme") {
        instruction = `Restyle this entire page to apply this theme, keeping all content and structure: ${freeText}. Apply it consistently across the whole page.`;
      } else if (kind === "generate") {
        const where = anchorHtml
          ? "Insert it immediately after this element:\n```html\n" + anchorHtml + "\n```"
          : "Add it at the end of the page body.";
        instruction = `Generate a new section for this page: ${freeText}. Match the page's existing visual style — colours, typography, spacing, and any CSS framework or class conventions it already uses. ${where}`;
      } else {
        instruction = PAGE_TASKS[kind] ?? "";
      }
      if (!instruction) return;
      const prompt =
        `You are editing the page "${title}" (design ${designId}, node ${nodeId}).\n\n` +
        `Task: ${instruction}\n\n` +
        "Work directly — do NOT create todos or read unrelated files. " +
        `Read the node once with read_design_artifact (design ${designId}, node ${nodeId}), ` +
        "apply the change, and save the full updated content with update_design_node.";
      if (!sendChatMessage(prompt, { withContext: false }))
        onNotice(`${BRAND} is busy — wait for the current turn to finish, then try again.`);
      else onNotice(`Asked ${BRAND} to update the page…`);
    },
    [designId, nodeId, title, onNotice],
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
      onElementAsk={onElementAsk}
      onPageAsk={onPageAsk}
    />
  );
}

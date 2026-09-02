// Drop a repository file onto a design canvas as a live artifact node.
//
// Mirrors the drag-and-drop import, but sourced from a path the file tree
// already knows rather than a browser File. Text-based files (HTML, SVG,
// markdown, code, notes) are placed verbatim; binaries such as images can't be
// read as text here, so the caller is told to drag them instead.
import { api, ApiError } from "../api/client";
import type { Design, FileRead } from "../api/types";

type PlaceKind = "html" | "svg" | "markdown" | "text";

const SIZES: Record<PlaceKind, [number, number]> = {
  html: [460, 320],
  svg: [340, 260],
  markdown: [320, 220],
  text: [320, 200],
};

const IMAGE_EXTS = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "webp",
  "bmp",
  "ico",
  "avif",
]);

function extensionOf(path: string): string {
  const name = path.split("/").pop() ?? path;
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i + 1).toLowerCase();
}

function basename(path: string): string {
  return path.split("/").pop() || path;
}

function kindFor(path: string): PlaceKind {
  switch (extensionOf(path)) {
    case "html":
    case "htm":
      return "html";
    case "svg":
      return "svg";
    case "md":
    case "markdown":
      return "markdown";
    default:
      return "text";
  }
}

export type PlaceResult =
  | { ok: true; design: Design }
  | { ok: false; message: string };

/**
 * Read `path` and add it to `designId` at (x, y). Returns the updated design so
 * the caller can seed the query cache, or a message to surface on failure.
 */
export async function placeFileOnCanvas(
  designId: string,
  path: string,
  at: { x: number; y: number },
): Promise<PlaceResult> {
  if (IMAGE_EXTS.has(extensionOf(path)))
    return {
      ok: false,
      message: `Drag ${basename(path)} onto the canvas to place an image.`,
    };
  let file: FileRead;
  try {
    file = await api.readFile(path);
  } catch (err) {
    const message =
      err instanceof ApiError
        ? err.status === 415
          ? `${basename(path)} is binary — drag it onto the canvas instead.`
          : err.status === 413
            ? `${basename(path)} is too large to place on the canvas.`
            : err.message
        : String(err);
    return { ok: false, message };
  }
  const content = file.content;
  const kind = kindFor(path);
  const [width, height] = SIZES[kind];
  const design = await api.addNode(designId, {
    label: basename(path),
    node_type: "artifact",
    x: Math.round(at.x),
    y: Math.round(at.y),
    data: {
      kind,
      content,
      filename: basename(path),
      width,
      height,
      // Provenance, not decoration. Without the repository path and the digest
      // of what was read, the node is a detached snapshot: nothing can tell
      // whether the file has moved on, resync it, or write an edit back to the
      // file it came from.
      source_path: path,
      source_digest: file.hash,
      placed_at: new Date().toISOString(),
    },
  });
  return { ok: true, design };
}

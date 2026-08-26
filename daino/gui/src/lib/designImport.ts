// Turn dropped or picked files into canvas artifacts.
//
// Anything the browser can show is kept verbatim — HTML, SVG, images, notes —
// so what lands on the canvas is the real file, not a summary of it. A design
// JSON export is recognised and merged as nodes and edges instead.

export type ArtifactKind = "html" | "svg" | "image" | "markdown" | "text";

export interface ArtifactSpec {
  kind: ArtifactKind;
  label: string;
  filename: string;
  /** Text content for html/svg/markdown/text. */
  content: string;
  /** Data URL for images. */
  src: string;
  width: number;
  height: number;
}

export interface DesignFragment {
  nodes: {
    id?: string;
    label?: string;
    type?: string;
    position?: { x: number; y: number };
    data?: Record<string, unknown>;
  }[];
  edges: { source: string; target: string; label?: string }[];
}

export type ImportResult =
  | { type: "artifact"; artifact: ArtifactSpec }
  | { type: "fragment"; fragment: DesignFragment; name: string }
  | { type: "error"; message: string };

const DEFAULT_SIZE: Record<ArtifactKind, [number, number]> = {
  html: [460, 320],
  svg: [340, 260],
  image: [340, 240],
  markdown: [320, 220],
  text: [320, 200],
};

//: Refuse files big enough to bloat the design document past usefulness.
const MAX_TEXT_BYTES = 1_500_000;
const MAX_IMAGE_BYTES = 3_000_000;

function extensionOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i < 0 ? "" : name.slice(i + 1).toLowerCase();
}

function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(file);
  });
}

function readDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

function kindFor(file: File): ArtifactKind | "design-json" | null {
  const ext = extensionOf(file.name);
  if (file.type.startsWith("image/") && ext !== "svg") return "image";
  switch (ext) {
    case "html":
    case "htm":
      return "html";
    case "svg":
      return "svg";
    case "md":
    case "markdown":
      return "markdown";
    case "json":
      return "design-json";
    case "png":
    case "jpg":
    case "jpeg":
    case "gif":
    case "webp":
    case "avif":
      return "image";
    case "txt":
    case "mmd":
    case "mermaid":
    case "yaml":
    case "yml":
    case "csv":
    case "puml":
      return "text";
    default:
      return file.type.startsWith("text/") ? "text" : null;
  }
}

export function artifactSpec(
  kind: ArtifactKind,
  filename: string,
  content: string,
  src = "",
): ArtifactSpec {
  const [width, height] = DEFAULT_SIZE[kind];
  return { kind, label: filename, filename, content, src, width, height };
}

/** Read one dropped file into something the canvas can hold. */
export async function importFile(file: File): Promise<ImportResult> {
  const kind = kindFor(file);
  if (kind === null) {
    return {
      type: "error",
      message: `${file.name}: unsupported file type for the canvas.`,
    };
  }
  if (kind === "image") {
    if (file.size > MAX_IMAGE_BYTES)
      return { type: "error", message: `${file.name} is larger than 3 MB.` };
    const src = await readDataUrl(file);
    return { type: "artifact", artifact: artifactSpec("image", file.name, "", src) };
  }
  if (file.size > MAX_TEXT_BYTES)
    return { type: "error", message: `${file.name} is larger than 1.5 MB.` };

  const text = await readText(file);
  if (kind === "design-json") {
    try {
      const parsed = JSON.parse(text) as Partial<DesignFragment> & {
        name?: string;
      };
      if (Array.isArray(parsed.nodes)) {
        return {
          type: "fragment",
          fragment: {
            nodes: parsed.nodes,
            edges: Array.isArray(parsed.edges) ? parsed.edges : [],
          },
          name: parsed.name || file.name,
        };
      }
    } catch {
      // Not a design export — fall through and keep it as a plain note.
    }
    return { type: "artifact", artifact: artifactSpec("text", file.name, text) };
  }
  return { type: "artifact", artifact: artifactSpec(kind, file.name, text) };
}

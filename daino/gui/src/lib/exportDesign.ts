// Client-side design exporters (JSON / SVG / HTML / single artifact).
//
// The HTML export is the interesting one: it reproduces the canvas as a
// standalone page — dropped HTML keeps rendering in its own frame, images stay
// images, and connectors are drawn behind everything — so a design can be
// handed to somebody who does not have Daino.
import type { Design, DesignNode } from "../api/types";
import { buildZip, dataUrlBytes, downloadBlob, textBytes, type ZipEntry } from "./zip";
import { BRAND } from "./branding";

export function download(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

const FALLBACK_W = 150;
const FALLBACK_H = 48;

function sizeOf(node: DesignNode): { w: number; h: number } {
  const data = node.data as Record<string, unknown>;
  const w = Number(data?.width);
  const h = Number(data?.height);
  return {
    w: Number.isFinite(w) && w > 0 ? w : FALLBACK_W,
    h: Number.isFinite(h) && h > 0 ? h : FALLBACK_H,
  };
}

function esc(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function exportJSON(design: Design) {
  download(
    `${design.name || "design"}.json`,
    JSON.stringify(design, null, 2),
    "application/json",
  );
}

/** Download one artifact node under its original filename. */
export function exportArtifact(node: DesignNode) {
  const data = (node.data ?? {}) as Record<string, unknown>;
  const kind = String(data.kind ?? "text");
  const filename = String(data.filename || node.label || "artifact");
  if (kind === "image") {
    const src = String(data.src ?? "");
    if (!src) return;
    const a = document.createElement("a");
    a.href = src;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    return;
  }
  const mime =
    kind === "html"
      ? "text/html"
      : kind === "svg"
        ? "image/svg+xml"
        : kind === "markdown"
          ? "text/markdown"
          : "text/plain";
  download(filename, String(data.content ?? ""), mime);
}

function bounds(design: Design) {
  const pad = 48;
  let minX = 0;
  let minY = 0;
  let maxX = FALLBACK_W;
  let maxY = FALLBACK_H;
  for (const node of design.nodes) {
    const { w, h } = sizeOf(node);
    minX = Math.min(minX, node.position.x);
    minY = Math.min(minY, node.position.y);
    maxX = Math.max(maxX, node.position.x + w);
    maxY = Math.max(maxY, node.position.y + h);
  }
  return {
    width: maxX - minX + pad * 2,
    height: maxY - minY + pad * 2,
    ox: pad - minX,
    oy: pad - minY,
  };
}

export function buildSVG(design: Design): string {
  const { width, height, ox, oy } = bounds(design);

  const centre = (id: string) => {
    const node = design.nodes.find((n) => n.id === id);
    if (!node) return null;
    const { w, h } = sizeOf(node);
    return { x: node.position.x + ox + w / 2, y: node.position.y + oy + h / 2 };
  };

  const edgeSvg = design.edges
    .map((edge) => {
      const a = centre(edge.source);
      const b = centre(edge.target);
      if (!a || !b) return "";
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      const label = edge.label
        ? `<text x="${mid.x}" y="${mid.y - 5}" fill="#7f8683" font-size="11" text-anchor="middle">${esc(edge.label)}</text>`
        : "";
      return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="#656b68" stroke-width="1.4" marker-end="url(#arrow)"/>${label}`;
    })
    .join("\n");

  const nodeSvg = design.nodes
    .map((node) => {
      const data = (node.data ?? {}) as Record<string, unknown>;
      const kind = String(data.kind ?? "");
      const { w, h } = sizeOf(node);
      const x = node.position.x + ox;
      const y = node.position.y + oy;
      const frame = `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="8" fill="#141716" stroke="#2a2f2c"/>`;
      if (kind === "image" && data.src) {
        return `<g>${frame}<image x="${x + 6}" y="${y + 6}" width="${w - 12}" height="${h - 12}" href="${esc(String(data.src))}" preserveAspectRatio="xMidYMid meet"/></g>`;
      }
      if (kind === "svg" && data.content) {
        // Inline the artwork itself, scaled into the node's frame.
        const inner = String(data.content).replace(
          /<\?xml[^>]*\?>/g,
          "",
        );
        return `<g>${frame}<g transform="translate(${x + 6} ${y + 6})">${inner}</g></g>`;
      }
      const caption = String(node.label || data.filename || "");
      return `<g>${frame}<text x="${x + w / 2}" y="${y + h / 2 + 4}" fill="#e4e7e5" font-size="12" font-family="sans-serif" text-anchor="middle">${esc(caption)}</text></g>`;
    })
    .join("\n");

  return `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#656b68"/></marker></defs>
<rect width="${width}" height="${height}" fill="#0c0e0d"/>
${edgeSvg}
${nodeSvg}
</svg>`;
}

export function exportSVG(design: Design) {
  download(`${design.name || "design"}.svg`, buildSVG(design), "image/svg+xml");
}

export function buildHTML(design: Design): string {
  const { width, height, ox, oy } = bounds(design);

  const centre = (id: string) => {
    const node = design.nodes.find((n) => n.id === id);
    if (!node) return null;
    const { w, h } = sizeOf(node);
    return { x: node.position.x + ox + w / 2, y: node.position.y + oy + h / 2 };
  };

  const connectors = design.edges
    .map((edge) => {
      const a = centre(edge.source);
      const b = centre(edge.target);
      if (!a || !b) return "";
      return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="#656b68" stroke-width="1.4" marker-end="url(#arrow)"/>`;
    })
    .join("\n");

  const cards = design.nodes
    .map((node) => {
      const data = (node.data ?? {}) as Record<string, unknown>;
      const kind = String(data.kind ?? "box");
      const { w, h } = sizeOf(node);
      const style = `left:${node.position.x + ox}px;top:${node.position.y + oy}px;width:${w}px;height:${h}px`;
      const title = esc(String(node.label || data.filename || ""));
      let body: string;
      if (kind === "html") {
        body = `<iframe sandbox="allow-scripts" srcdoc="${esc(String(data.content ?? ""))}"></iframe>`;
      } else if (kind === "svg") {
        body = `<div class="fit">${String(data.content ?? "")}</div>`;
      } else if (kind === "image") {
        body = `<div class="fit"><img src="${esc(String(data.src ?? ""))}" alt="${title}"></div>`;
      } else if (kind === "markdown" || kind === "text") {
        body = `<pre>${esc(String(data.content ?? ""))}</pre>`;
      } else {
        return `<div class="card box" style="${style}"><span>${title}</span></div>`;
      }
      return `<div class="card" style="${style}"><header>${title}</header><div class="body">${body}</div></div>`;
    })
    .join("\n");

  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${esc(design.name || "design")}</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; background:#0c0e0d; color:#b5bab7;
         font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  h1 { font-size:14px; font-weight:600; color:#e4e7e5; margin:0; padding:14px 18px;
       border-bottom:1px solid #1e2220; letter-spacing:.02em; }
  h1 span { color:#6cbf8d; }
  .stage { position:relative; width:${width}px; height:${height}px; }
  .wires { position:absolute; inset:0; pointer-events:none; }
  .card { position:absolute; background:#141716; border:1px solid #2a2f2c; border-radius:8px;
          overflow:hidden; display:flex; flex-direction:column;
          box-shadow:0 1px 2px rgba(0,0,0,.4); }
  .card header { padding:5px 9px; font-size:11px; color:#7f8683;
                 border-bottom:1px solid #1e2220; background:#101211; }
  .card .body { flex:1; min-height:0; background:#0c0e0d; }
  .card.box { align-items:center; justify-content:center; color:#e4e7e5; }
  iframe { width:100%; height:100%; border:0; background:#fff; display:block; }
  pre { margin:0; padding:9px 11px; height:100%; overflow:auto; white-space:pre-wrap;
        font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:12px; }
  .fit { height:100%; display:flex; align-items:center; justify-content:center; overflow:hidden; }
  .fit img, .fit svg { max-width:100%; max-height:100%; }
</style></head>
<body>
<h1><span>◆</span> ${esc(design.name || "design")}</h1>
<div class="stage">
  <svg class="wires" width="${width}" height="${height}">
    <defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto">
      <path d="M0,0 L8,4 L0,8 z" fill="#656b68"/></marker></defs>
    ${connectors}
  </svg>
  ${cards}
</div>
</body></html>`;
}

export function exportHTML(design: Design) {
  download(`${design.name || "design"}.html`, buildHTML(design), "text/html");
}


// ---- Prototype bundles ----

/** Strip anything that would be awkward in a filename or a zip path. */
function safeName(value: string, fallback: string): string {
  const cleaned = value
    .replace(/[\\/:*?"<>|]+/g, "-")
    .replace(/\s+/g, "-")
    .replace(/^[.-]+/, "")
    .slice(0, 80);
  return cleaned || fallback;
}

function withExtension(name: string, extension: string): string {
  return name.toLowerCase().endsWith(extension) ? name : `${name}${extension}`;
}

const EXTENSION: Record<string, string> = {
  html: ".html",
  svg: ".svg",
  markdown: ".md",
  text: ".txt",
};

/** Give every artifact a unique path inside the archive. */
function assign(taken: Set<string>, candidate: string): string {
  if (!taken.has(candidate)) {
    taken.add(candidate);
    return candidate;
  }
  const dot = candidate.lastIndexOf(".");
  const stem = dot > 0 ? candidate.slice(0, dot) : candidate;
  const extension = dot > 0 ? candidate.slice(dot) : "";
  for (let n = 2; ; n += 1) {
    const next = `${stem}-${n}${extension}`;
    if (!taken.has(next)) {
      taken.add(next);
      return next;
    }
  }
}

/**
 * Package a canvas as a self-contained prototype.
 *
 * `index.html` is either the canvas layout or one chosen page, so unzipping and
 * opening it lands somewhere useful without reading a README first. Every other
 * artifact is written out under its own name, and the design document rides
 * along so the bundle can be dropped back onto a Daino canvas later.
 */
export function buildPrototypeZip(design: Design, rootNodeId?: string): Blob {
  const taken = new Set<string>(["index.html", "design.json"]);
  const entries: ZipEntry[] = [];
  const pages: { path: string; title: string }[] = [];

  const root = rootNodeId
    ? design.nodes.find((node) => node.id === rootNodeId)
    : undefined;

  for (const node of design.nodes) {
    const data = (node.data ?? {}) as Record<string, unknown>;
    const kind = String(data.kind ?? "");
    if (!kind) continue; // a plain diagram box has no file of its own
    const base = safeName(
      String(data.filename || node.label || node.id),
      node.id,
    );

    if (node.id === root?.id && kind === "html") {
      entries.push({ name: "index.html", bytes: textBytes(String(data.content ?? "")) });
      pages.push({ path: "index.html", title: node.label || base });
      continue;
    }
    if (kind === "image") {
      const src = String(data.src ?? "");
      if (!src) continue;
      const path = assign(taken, `assets/${base}`);
      entries.push({ name: path, bytes: dataUrlBytes(src) });
      continue;
    }
    const extension = EXTENSION[kind] ?? ".txt";
    const folder = kind === "html" ? "pages" : "assets";
    const path = assign(taken, `${folder}/${withExtension(base, extension)}`);
    entries.push({ name: path, bytes: textBytes(String(data.content ?? "")) });
    if (kind === "html") pages.push({ path, title: node.label || base });
  }

  if (!root) {
    // No single page was chosen: the canvas layout becomes the entry point.
    entries.unshift({ name: "index.html", bytes: textBytes(buildHTML(design)) });
  }

  entries.push({
    name: "design.json",
    bytes: textBytes(JSON.stringify(design, null, 2)),
  });
  entries.push({
    name: "README.md",
    bytes: textBytes(
      `# ${design.name || "Prototype"}\n\n` +
        `Exported from ${BRAND}.\n\n` +
        `- \`index.html\` — open this first.\n` +
        (pages.length
          ? `- Pages:\n${pages.map((p) => `  - \`${p.path}\` — ${p.title}`).join("\n")}\n`
          : "") +
        `- \`design.json\` — the canvas document; drop it back onto a ${BRAND} canvas to restore it.\n`,
    ),
  });

  return buildZip(entries);
}

export function exportPrototypeZip(design: Design, rootNodeId?: string) {
  downloadBlob(
    `${safeName(design.name || "prototype", "prototype")}.zip`,
    buildPrototypeZip(design, rootNodeId),
  );
}

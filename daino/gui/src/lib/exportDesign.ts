// Client-side, best-effort design exporters (JSON / SVG / HTML).
import type { Design } from "../api/types";

function download(filename: string, content: string, mime: string) {
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

const NODE_W = 140;
const NODE_H = 44;

export function exportJSON(design: Design) {
  download(
    `${design.name || "design"}.json`,
    JSON.stringify(design, null, 2),
    "application/json",
  );
}

export function buildSVG(design: Design): string {
  const pad = 40;
  const xs = design.nodes.map((n) => n.position.x);
  const ys = design.nodes.map((n) => n.position.y);
  const minX = Math.min(0, ...xs);
  const minY = Math.min(0, ...ys);
  const maxX = Math.max(NODE_W, ...xs.map((x) => x + NODE_W));
  const maxY = Math.max(NODE_H, ...ys.map((y) => y + NODE_H));
  const width = maxX - minX + pad * 2;
  const height = maxY - minY + pad * 2;
  const ox = pad - minX;
  const oy = pad - minY;

  const center = (id: string) => {
    const n = design.nodes.find((x) => x.id === id);
    if (!n) return null;
    return { x: n.position.x + ox + NODE_W / 2, y: n.position.y + oy + NODE_H / 2 };
  };

  const esc = (s: string) =>
    s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  const edgeSvg = design.edges
    .map((e) => {
      const a = center(e.source);
      const b = center(e.target);
      if (!a || !b) return "";
      const mid = { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
      return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" stroke="#7c8798" stroke-width="1.5" marker-end="url(#arrow)"/>${
        e.label
          ? `<text x="${mid.x}" y="${mid.y - 4}" fill="#b3bdcc" font-size="11" text-anchor="middle">${esc(e.label)}</text>`
          : ""
      }`;
    })
    .join("\n");

  const nodeSvg = design.nodes
    .map((n) => {
      const x = n.position.x + ox;
      const y = n.position.y + oy;
      return `<g><rect x="${x}" y="${y}" width="${NODE_W}" height="${NODE_H}" rx="8" fill="#151a24" stroke="#2f3948"/><text x="${
        x + NODE_W / 2
      }" y="${y + NODE_H / 2 + 4}" fill="#e6edf3" font-size="12" font-family="sans-serif" text-anchor="middle">${esc(
        n.label,
      )}</text></g>`;
    })
    .join("\n");

  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#7c8798"/></marker></defs>
<rect width="${width}" height="${height}" fill="#0b0e14"/>
${edgeSvg}
${nodeSvg}
</svg>`;
}

export function exportSVG(design: Design) {
  download(`${design.name || "design"}.svg`, buildSVG(design), "image/svg+xml");
}

export function exportHTML(design: Design) {
  const svg = buildSVG(design);
  const html = `<!doctype html>
<html><head><meta charset="utf-8"><title>${design.name || "design"}</title>
<style>body{margin:0;background:#0b0e14;color:#e6edf3;font-family:sans-serif}h1{padding:16px;font-size:16px}</style>
</head><body><h1>${design.name || "design"}</h1><div style="padding:16px">${svg}</div></body></html>`;
  download(`${design.name || "design"}.html`, html, "text/html");
}

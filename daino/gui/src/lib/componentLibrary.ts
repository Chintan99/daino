// Drag-and-drop building blocks for the visual editor.
//
// Markup stays plain HTML with only the inline styles a block genuinely needs to
// hold its shape, so a component dropped into a blank page looks right without
// dragging a framework in, and a component dropped into a styled page inherits
// that page's type and colour instead of fighting it.

export interface ComponentDef {
  id: string;
  group: string;
  label: string;
  icon: string;
  html: string;
}

export const COMPONENTS: ComponentDef[] = [
  // ---- Layout ----
  {
    id: "section",
    group: "Layout",
    label: "Section",
    icon: "▭",
    html: `<section style="padding:48px 24px">\n  <h2>Section title</h2>\n  <p>Describe this section.</p>\n</section>`,
  },
  {
    id: "container",
    group: "Layout",
    label: "Container",
    icon: "▢",
    html: `<div style="max-width:1080px;margin:0 auto;padding:0 24px">\n  <p>Container</p>\n</div>`,
  },
  {
    id: "columns-2",
    group: "Layout",
    label: "Two columns",
    icon: "◫",
    html: `<div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">\n  <div><h3>Left</h3><p>Column one.</p></div>\n  <div><h3>Right</h3><p>Column two.</p></div>\n</div>`,
  },
  {
    id: "columns-3",
    group: "Layout",
    label: "Three columns",
    icon: "⫼",
    html: `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:20px">\n  <div><h3>One</h3><p>Detail.</p></div>\n  <div><h3>Two</h3><p>Detail.</p></div>\n  <div><h3>Three</h3><p>Detail.</p></div>\n</div>`,
  },
  {
    id: "row",
    group: "Layout",
    label: "Flex row",
    icon: "☰",
    html: `<div style="display:flex;gap:12px;align-items:center">\n  <span>First</span>\n  <span>Second</span>\n</div>`,
  },
  {
    id: "divider",
    group: "Layout",
    label: "Divider",
    icon: "—",
    html: `<hr style="border:none;border-top:1px solid currentColor;opacity:.2;margin:28px 0">`,
  },
  {
    id: "spacer",
    group: "Layout",
    label: "Spacer",
    icon: "␣",
    html: `<div style="height:48px"></div>`,
  },

  // ---- Text ----
  { id: "h1", group: "Text", label: "Heading 1", icon: "H1", html: `<h1>Heading</h1>` },
  { id: "h2", group: "Text", label: "Heading 2", icon: "H2", html: `<h2>Subheading</h2>` },
  { id: "h3", group: "Text", label: "Heading 3", icon: "H3", html: `<h3>Small heading</h3>` },
  {
    id: "p",
    group: "Text",
    label: "Paragraph",
    icon: "¶",
    html: `<p>Write something here.</p>`,
  },
  {
    id: "list",
    group: "Text",
    label: "List",
    icon: "•",
    html: `<ul>\n  <li>First item</li>\n  <li>Second item</li>\n  <li>Third item</li>\n</ul>`,
  },
  {
    id: "quote",
    group: "Text",
    label: "Quote",
    icon: "❝",
    html: `<blockquote style="margin:0;padding-left:16px;border-left:3px solid currentColor;opacity:.85">\n  <p>A quotation worth pulling out.</p>\n</blockquote>`,
  },
  {
    id: "code",
    group: "Text",
    label: "Code block",
    icon: "{}",
    html: `<pre style="padding:14px;overflow:auto;background:rgba(127,127,127,.12);border-radius:8px"><code>console.log("hello");</code></pre>`,
  },

  // ---- Media ----
  {
    id: "image",
    group: "Media",
    label: "Image",
    icon: "🖼",
    html: `<img src="https://placehold.co/640x360" alt="Describe this image" style="max-width:100%;height:auto;border-radius:8px">`,
  },
  {
    id: "figure",
    group: "Media",
    label: "Figure",
    icon: "▣",
    html: `<figure style="margin:0">\n  <img src="https://placehold.co/640x360" alt="Describe this image" style="max-width:100%;height:auto;border-radius:8px">\n  <figcaption style="opacity:.7;font-size:13px;margin-top:6px">Caption</figcaption>\n</figure>`,
  },
  {
    id: "video",
    group: "Media",
    label: "Video",
    icon: "▶",
    html: `<video controls style="max-width:100%;border-radius:8px"><source src="" type="video/mp4"></video>`,
  },

  // ---- Interactive ----
  {
    id: "button",
    group: "Interactive",
    label: "Button",
    icon: "⬭",
    html: `<button type="button" style="padding:10px 18px;border-radius:8px;border:1px solid currentColor;background:transparent;font:inherit;cursor:pointer">Click me</button>`,
  },
  {
    id: "link",
    group: "Interactive",
    label: "Link",
    icon: "↗",
    html: `<a href="#">A link</a>`,
  },
  {
    id: "input",
    group: "Interactive",
    label: "Text field",
    icon: "▭",
    html: `<label style="display:block">\n  <span style="display:block;font-size:13px;margin-bottom:4px">Label</span>\n  <input type="text" placeholder="Type here" style="padding:9px 11px;border-radius:8px;border:1px solid rgba(127,127,127,.5);font:inherit;width:100%">\n</label>`,
  },
  {
    id: "textarea",
    group: "Interactive",
    label: "Text area",
    icon: "▤",
    html: `<textarea rows="4" placeholder="Type here" style="padding:9px 11px;border-radius:8px;border:1px solid rgba(127,127,127,.5);font:inherit;width:100%"></textarea>`,
  },
  {
    id: "select",
    group: "Interactive",
    label: "Select",
    icon: "▾",
    html: `<select style="padding:9px 11px;border-radius:8px;border:1px solid rgba(127,127,127,.5);font:inherit">\n  <option>First</option>\n  <option>Second</option>\n</select>`,
  },
  {
    id: "form",
    group: "Interactive",
    label: "Form",
    icon: "✉",
    html: `<form style="display:grid;gap:12px;max-width:420px">\n  <input type="email" placeholder="you@example.com" style="padding:10px 12px;border-radius:8px;border:1px solid rgba(127,127,127,.5);font:inherit">\n  <button type="submit" style="padding:10px 18px;border-radius:8px;border:1px solid currentColor;background:transparent;font:inherit;cursor:pointer">Subscribe</button>\n</form>`,
  },

  // ---- Blocks ----
  {
    id: "nav",
    group: "Blocks",
    label: "Nav bar",
    icon: "⌸",
    html: `<nav style="display:flex;align-items:center;gap:24px;padding:16px 24px;border-bottom:1px solid rgba(127,127,127,.25)">\n  <strong>Brand</strong>\n  <a href="#" style="margin-left:auto">Product</a>\n  <a href="#">Pricing</a>\n  <a href="#">Docs</a>\n</nav>`,
  },
  {
    id: "hero",
    group: "Blocks",
    label: "Hero",
    icon: "★",
    html: `<header style="padding:96px 24px;text-align:center">\n  <h1 style="font-size:44px;margin:0 0 12px">A headline that explains it</h1>\n  <p style="font-size:18px;opacity:.75;max-width:600px;margin:0 auto 28px">One sentence on why it matters.</p>\n  <a href="#" style="display:inline-block;padding:12px 24px;border-radius:8px;border:1px solid currentColor;text-decoration:none">Get started</a>\n</header>`,
  },
  {
    id: "card",
    group: "Blocks",
    label: "Card",
    icon: "▥",
    html: `<article style="padding:20px;border:1px solid rgba(127,127,127,.28);border-radius:12px">\n  <h3 style="margin:0 0 8px">Card title</h3>\n  <p style="margin:0;opacity:.8">Supporting copy for this card.</p>\n</article>`,
  },
  {
    id: "features",
    group: "Blocks",
    label: "Feature grid",
    icon: "⊞",
    html: `<section style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:20px;padding:48px 24px">\n  <div><h3>Fast</h3><p style="opacity:.8">Why it is fast.</p></div>\n  <div><h3>Simple</h3><p style="opacity:.8">Why it is simple.</p></div>\n  <div><h3>Local</h3><p style="opacity:.8">Why it stays local.</p></div>\n</section>`,
  },
  {
    id: "cta",
    group: "Blocks",
    label: "Call to action",
    icon: "◎",
    html: `<section style="padding:64px 24px;text-align:center;border-top:1px solid rgba(127,127,127,.25)">\n  <h2 style="margin:0 0 10px">Ready to start?</h2>\n  <p style="opacity:.75;margin:0 0 22px">Add the closing argument here.</p>\n  <a href="#" style="display:inline-block;padding:12px 24px;border-radius:8px;border:1px solid currentColor;text-decoration:none">Get started</a>\n</section>`,
  },
  {
    id: "table",
    group: "Blocks",
    label: "Table",
    icon: "▦",
    html: `<table style="border-collapse:collapse;width:100%">\n  <thead><tr><th style="text-align:left;padding:8px;border-bottom:1px solid rgba(127,127,127,.35)">Name</th><th style="text-align:left;padding:8px;border-bottom:1px solid rgba(127,127,127,.35)">Value</th></tr></thead>\n  <tbody><tr><td style="padding:8px;border-bottom:1px solid rgba(127,127,127,.18)">First</td><td style="padding:8px;border-bottom:1px solid rgba(127,127,127,.18)">1</td></tr></tbody>\n</table>`,
  },
  {
    id: "footer",
    group: "Blocks",
    label: "Footer",
    icon: "▂",
    html: `<footer style="padding:32px 24px;border-top:1px solid rgba(127,127,127,.25);display:flex;gap:16px;align-items:center">\n  <span style="opacity:.7">© 2026 Your project</span>\n  <a href="#" style="margin-left:auto">Privacy</a>\n  <a href="#">Terms</a>\n</footer>`,
  },
];

export const COMPONENT_GROUPS = [
  "Layout",
  "Text",
  "Media",
  "Interactive",
  "Blocks",
];

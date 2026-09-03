// Documentation-page behaviour: search, sidebar, and the contents rail.
// Loaded only by the generated pages; the landing page does not need any of it.

const dialog = document.querySelector("[data-search-dialog]");
const input = document.querySelector("[data-search-input]");
const results = document.querySelector("[data-search-results]");
const sidebar = document.getElementById("doc-sidebar");
const sidebarToggle = document.querySelector("[data-sidebar-toggle]");

// ---------------------------------------------------------------- sidebar

sidebarToggle?.addEventListener("click", () => {
  const open = sidebar?.classList.toggle("open") ?? false;
  sidebarToggle.setAttribute("aria-expanded", String(open));
});

// Keep the open page visible in a sidebar longer than the viewport, without
// scrolling the page itself.
const current = sidebar?.querySelector("a.current");
if (current && sidebar) {
  const offset = current.offsetTop - sidebar.clientHeight / 2;
  if (offset > 0) sidebar.scrollTop = offset;
}

// ------------------------------------------------------------ contents rail

// Mark the section being read. An IntersectionObserver rather than a scroll
// handler: it reports only when a heading actually crosses the line, so this
// costs nothing while the reader is still.
const tocLinks = [...document.querySelectorAll(".doc-toc-links a")];
if (tocLinks.length) {
  const byId = new Map(tocLinks.map((link) => [link.getAttribute("href").slice(1), link]));
  const seen = new Set();
  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) seen.add(entry.target.id);
        else seen.delete(entry.target.id);
      }
      // The topmost visible heading wins, so scrolling up highlights the
      // section coming back into view rather than the one leaving it.
      const active = tocLinks.find((link) => seen.has(link.getAttribute("href").slice(1)));
      tocLinks.forEach((link) => link.classList.toggle("active", link === active));
    },
    { rootMargin: "-88px 0px -70% 0px" },
  );
  for (const id of byId.keys()) {
    const heading = document.getElementById(id);
    if (heading) observer.observe(heading);
  }
}

// ----------------------------------------------------------------- search

/** @type {{page: string, heading: string, url: string, text: string}[] | null} */
let entries = null;
let loading = null;
let active = -1;

function loadIndex() {
  // Fetched on first use, not on page load: most readers never search, and the
  // index is larger than the page they came for.
  loading ??= fetch(new URL("search-index.json", document.baseURI))
    .then((response) => (response.ok ? response.json() : { pages: [] }))
    .then((data) => {
      entries = data.pages ?? [];
    })
    .catch(() => {
      entries = [];
    });
  return loading;
}

function escapeHtml(value) {
  return value.replace(
    /[&<>"']/g,
    (character) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[character],
  );
}

/**
 * Score one entry against the query's terms.
 *
 * Every term has to appear somewhere, so a two-word query narrows instead of
 * widening. Where it appears decides the rank: a heading match is what the
 * reader is looking for far more often than a passing mention in prose.
 */
function score(entry, terms) {
  const heading = entry.heading.toLowerCase();
  const page = entry.page.toLowerCase();
  const text = entry.text.toLowerCase();
  let total = 0;
  for (const term of terms) {
    const inHeading = heading.includes(term);
    const inPage = page.includes(term);
    const inText = text.includes(term);
    if (!inHeading && !inPage && !inText) return 0;
    if (inHeading) total += heading.startsWith(term) ? 12 : 8;
    if (inPage) total += 4;
    if (inText) total += 1;
  }
  return total;
}

/** The stretch of prose around the first match, so a hit shows its context. */
function excerpt(entry, terms) {
  const text = entry.text;
  const at = text.toLowerCase().indexOf(terms[0]);
  if (at < 0) return text.slice(0, 130);
  const from = Math.max(0, at - 45);
  return (from ? "…" : "") + text.slice(from, from + 150).trim() + (text.length > from + 150 ? "…" : "");
}

function highlight(value, terms) {
  let output = escapeHtml(value);
  for (const term of terms) {
    // Terms come from the reader, so they are escaped for the pattern as well
    // as for the markup.
    const pattern = new RegExp(`(${term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})`, "gi");
    output = output.replace(pattern, "<mark>$1</mark>");
  }
  return output;
}

function render(query) {
  const terms = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) {
    results.innerHTML = '<p class="doc-search-empty">Type to search every page.</p>';
    active = -1;
    return;
  }
  const matches = (entries ?? [])
    .map((entry) => ({ entry, rank: score(entry, terms) }))
    .filter((item) => item.rank > 0)
    .sort((a, b) => b.rank - a.rank)
    .slice(0, 25);

  if (!matches.length) {
    results.innerHTML = `<p class="doc-search-empty">Nothing matches “${escapeHtml(query)}”.</p>`;
    active = -1;
    return;
  }
  results.innerHTML = matches
    .map(
      ({ entry }) => `
      <a class="doc-search-hit" href="${entry.url}">
        <span class="doc-search-crumb">${escapeHtml(entry.page)}${
          entry.heading ? ` <i>/</i> ${highlight(entry.heading, terms)}` : ""
        }</span>
        <span class="doc-search-text">${highlight(excerpt(entry, terms), terms)}</span>
      </a>`,
    )
    .join("");
  active = 0;
  mark();
}

function hits() {
  return [...results.querySelectorAll(".doc-search-hit")];
}

function mark() {
  hits().forEach((hit, index) => {
    const on = index === active;
    hit.classList.toggle("active", on);
    if (on) hit.scrollIntoView({ block: "nearest" });
  });
}

async function open() {
  if (!dialog) return;
  dialog.hidden = false;
  document.body.classList.add("search-open");
  input.focus();
  input.select();
  if (!entries) {
    results.innerHTML = '<p class="doc-search-empty">Loading the index…</p>';
    await loadIndex();
  }
  render(input.value);
}

function close() {
  if (!dialog) return;
  dialog.hidden = true;
  document.body.classList.remove("search-open");
}

document.querySelectorAll("[data-search-open]").forEach((button) => {
  button.addEventListener("click", () => void open());
});
document.querySelectorAll("[data-search-close]").forEach((button) => {
  button.addEventListener("click", close);
});

input?.addEventListener("input", () => render(input.value));

/**
 * Follow one hit, closing the dialog first.
 *
 * A hit on the page already open is only a fragment change, so the browser
 * does not reload — and the dialog would sit over the section it just jumped
 * to. Closing before navigating covers both cases with one path.
 */
function go(href) {
  close();
  window.location.href = href;
}

results?.addEventListener("click", (event) => {
  const hit = event.target.closest?.(".doc-search-hit");
  if (!hit) return;
  event.preventDefault();
  go(hit.getAttribute("href"));
});

input?.addEventListener("keydown", (event) => {
  const list = hits();
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    if (!list.length) return;
    event.preventDefault();
    const step = event.key === "ArrowDown" ? 1 : -1;
    active = (active + step + list.length) % list.length;
    mark();
    return;
  }
  if (event.key === "Enter" && list[active]) {
    event.preventDefault();
    go(list[active].getAttribute("href"));
  }
});

document.addEventListener("keydown", (event) => {
  const open_ = dialog && !dialog.hidden;
  if (event.key === "Escape" && open_) {
    close();
    return;
  }
  // Never steal the key from someone typing into a field.
  const typing = /^(input|textarea|select)$/i.test(event.target?.tagName ?? "");
  if (!open_ && !typing && (event.key === "/" || ((event.metaKey || event.ctrlKey) && event.key === "k"))) {
    event.preventDefault();
    void open();
  }
});

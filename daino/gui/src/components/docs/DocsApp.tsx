import { useEffect, useMemo, useState } from "react";
import ReactMarkdown from "react-markdown";
import { api } from "../../api/client";
import type { DocsPage, DocsIndex } from "../../api/types";
import { BRAND } from "../../lib/branding";

/** Anchor id for a heading, matching what the sidebar links to. */
function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9\s-]/g, "")
    .trim()
    .replace(/\s+/g, "-");
}

function childText(children: React.ReactNode): string {
  if (typeof children === "string") return children;
  if (Array.isArray(children)) return children.map(childText).join("");
  if (children && typeof children === "object" && "props" in children)
    return childText((children as { props: { children?: React.ReactNode } }).props.children);
  return "";
}

/**
 * Daino's own documentation, served at /docs.
 *
 * It runs inside the same bundle as the IDE so it inherits the theme and the
 * markdown renderer, and reads the very markdown that ships in `docs/` — one
 * source of truth rather than a hand-maintained copy.
 */
export function DocsApp() {
  const [index, setIndex] = useState<DocsIndex | null>(null);
  const [page, setPage] = useState<DocsPage | null>(null);
  const [slug, setSlug] = useState<string>(
    () => window.location.hash.replace(/^#\/?/, "").split("#")[0] || "",
  );
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .docsIndex()
      .then((data) => {
        setIndex(data);
        if (!slug && data.pages.length) setSlug(data.pages[0].slug);
      })
      .catch((err) => setError(String(err?.message ?? err)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!slug) return;
    window.location.hash = `/${slug}`;
    api
      .docsPage(slug)
      .then((data) => {
        setPage(data);
        setError(null);
        window.scrollTo({ top: 0 });
      })
      .catch((err) => setError(String(err?.message ?? err)));
  }, [slug]);

  useEffect(() => {
    const onHash = () => {
      const next = window.location.hash.replace(/^#\/?/, "").split("#")[0];
      if (next) setSlug(next);
    };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    document.title = page ? `${page.title} — ${BRAND} docs` : `${BRAND} docs`;
  }, [page]);

  const sections = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const pages = (index?.pages ?? []).filter(
      (p) => !needle || p.title.toLowerCase().includes(needle) || p.slug.includes(needle),
    );
    const grouped: { section: string; pages: typeof pages }[] = [];
    for (const item of pages) {
      const bucket = grouped.find((g) => g.section === item.section);
      if (bucket) bucket.pages.push(item);
      else grouped.push({ section: item.section, pages: [item] });
    }
    return grouped;
  }, [index, query]);

  // On-page table of contents, built from the markdown's own H2s.
  const outline = useMemo(() => {
    if (!page) return [];
    return page.markdown
      .split("\n")
      .filter((line) => /^##\s+/.test(line))
      .map((line) => line.replace(/^##\s+/, "").trim())
      .map((title) => ({ title, id: slugify(title) }));
  }, [page]);

  return (
    <div className="docs">
      <header className="docs-top">
        <a className="wordmark" href="/" title={`Back to the ${BRAND} IDE`}>
          <span className="dot" />
          {BRAND}
        </a>
        <span className="docs-kicker">Documentation</span>
        <span className="grow" />
        <a className="btn subtle sm" href="/">
          Open the IDE
        </a>
        <a className="btn subtle sm" href="/api-docs" target="_blank" rel="noreferrer noopener">
          API reference ↗
        </a>
      </header>

      <div className="docs-body">
        <nav className="docs-nav">
          <input
            className="input"
            placeholder="Search pages"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          {sections.map((group) => (
            <div key={group.section}>
              <div className="section-title">{group.section}</div>
              {group.pages.map((item) => (
                <button
                  key={item.slug}
                  className={`docs-link ${slug === item.slug ? "active" : ""}`}
                  onClick={() => setSlug(item.slug)}
                >
                  {item.title}
                </button>
              ))}
            </div>
          ))}
          {index && index.pages.length === 0 && (
            <div className="empty">No documentation is installed.</div>
          )}
        </nav>

        <main className="docs-main">
          {error && <div className="empty">Could not load documentation: {error}</div>}
          {!error && !page && <div className="empty">Loading…</div>}
          {page && (
            <article className="docs-article md-block">
              <ReactMarkdown
                components={{
                  h1: ({ children }) => <h1 id={slugify(childText(children))}>{children}</h1>,
                  h2: ({ children }) => <h2 id={slugify(childText(children))}>{children}</h2>,
                  h3: ({ children }) => <h3 id={slugify(childText(children))}>{children}</h3>,
                  a: ({ href, children }) => {
                    // Links between markdown pages become in-app navigation.
                    const local = href?.match(/^([a-z0-9-]+)\.md(#.*)?$/i);
                    if (local)
                      return (
                        <a
                          href={`#/${local[1]}`}
                          onClick={() => setSlug(local[1].toLowerCase())}
                        >
                          {children}
                        </a>
                      );
                    return (
                      <a href={href} target="_blank" rel="noreferrer noopener">
                        {children}
                      </a>
                    );
                  },
                }}
              >
                {page.markdown}
              </ReactMarkdown>
            </article>
          )}
        </main>

        {outline.length > 1 && (
          <aside className="docs-outline">
            <div className="section-title">On this page</div>
            {outline.map((item) => (
              <a key={item.id} href={`#${item.id}`}>
                {item.title}
              </a>
            ))}
          </aside>
        )}
      </div>
    </div>
  );
}

import { useEffect, useMemo, useRef, useState } from "react";
import { useSearch } from "../../api/hooks";
import { openFileInEditor } from "../../lib/openFile";
import { useEditorStore } from "../../store/editorStore";
import { useUIStore } from "../../store/uiStore";
import type { SearchMatch } from "../../api/types";

/** Where the query sits inside the matched line, so it can be selected. */
function locate(text: string, query: string): { column: number; length: number } {
  if (!query) return { column: 1, length: 0 };
  let index = text.indexOf(query);
  if (index < 0) index = text.toLowerCase().indexOf(query.toLowerCase());
  if (index < 0) return { column: 1, length: 0 };
  return { column: index + 1, length: query.length };
}

/** Split a line around the match so the hit can be picked out at a glance. */
function highlight(text: string, query: string) {
  const { column, length } = locate(text, query);
  if (!length) return <>{text}</>;
  const start = column - 1;
  return (
    <>
      {text.slice(0, start)}
      <mark>{text.slice(start, start + length)}</mark>
      {text.slice(start + length)}
    </>
  );
}

export function SearchPanel() {
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const { data, isFetching } = useSearch(query);
  const activePath = useEditorStore((s) => s.activePath);
  const reveal = useEditorStore((s) => s.reveal);
  const focusNonce = useUIStore((s) => s.searchFocusNonce);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Edit ▸ Find in files opens this panel; it has to land in the field, or the
  // command only *looks* like it did something.
  useEffect(() => {
    if (!focusNonce) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [focusNonce]);

  // Group by file, the way a search result list is expected to read.
  const groups = useMemo(() => {
    const byFile: { path: string; matches: SearchMatch[] }[] = [];
    for (const match of data?.matches ?? []) {
      const bucket = byFile.find((g) => g.path === match.path);
      if (bucket) bucket.matches.push(match);
      else byFile.push({ path: match.path, matches: [match] });
    }
    return byFile;
  }, [data]);

  const total = data?.matches.length ?? 0;

  const open = (match: SearchMatch) => {
    const { column, length } = locate(match.text, query);
    void openFileInEditor(match.path, { line: match.line, column, length });
  };

  return (
    <div className="panel">
      <div className="panel-header">
        Search
        <span className="spacer" />
        {data && !isFetching && (
          <span className="muted">
            {total} in {groups.length} file{groups.length === 1 ? "" : "s"}
          </span>
        )}
      </div>
      <div style={{ padding: 8, borderBottom: "1px solid var(--border)" }}>
        <input
          className="input"
          ref={inputRef}
          placeholder="Search files…  (Enter)"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") setQuery(input);
          }}
        />
      </div>
      <div className="panel-body">
        {isFetching && <div className="empty">Searching…</div>}
        {!isFetching && data && total === 0 && (
          <div className="empty">No matches</div>
        )}
        {!isFetching && !data && (
          <div className="empty">Type a search and press Enter.</div>
        )}
        {!isFetching &&
          groups.map((group) => (
            <div className="search-group" key={group.path}>
              <div
                className={`search-file ${activePath === group.path ? "active" : ""}`}
                title={group.path}
              >
                <span className="tree-name">{group.path}</span>
                <span className="badge">{group.matches.length}</span>
              </div>
              {group.matches.map((match, i) => {
                const here =
                  reveal?.path === match.path && reveal.line === match.line;
                return (
                  <button
                    key={`${match.path}:${match.line}:${i}`}
                    className={`search-match ${here ? "active" : ""}`}
                    onClick={() => open(match)}
                    title={`${match.path}:${match.line}`}
                  >
                    <span className="ln">{match.line}</span>
                    <span className="line mono">
                      {highlight(match.text.trim().slice(0, 240), query)}
                    </span>
                  </button>
                );
              })}
            </div>
          ))}
      </div>
    </div>
  );
}

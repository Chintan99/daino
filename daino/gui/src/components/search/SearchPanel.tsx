import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../../api/client";
import { openFileInEditor } from "../../lib/openFile";
import { useEditorStore } from "../../store/editorStore";
import { useUIStore } from "../../store/uiStore";
import { confirmFor } from "../../store/dialogStore";
import type { SearchMatch } from "../../api/types";

/** Split a line around the match so the hit can be picked out at a glance. */
function highlight(match: SearchMatch, query: string) {
  const start = (match.column ?? 1) - 1;
  const length = match.length ?? query.length;
  if (!length || start < 0) return <>{match.text}</>;
  return (
    <>
      {match.text.slice(0, start)}
      <mark>{match.text.slice(start, start + length)}</mark>
      {match.text.slice(start + length)}
    </>
  );
}

/**
 * Find text across the repository, and optionally replace it.
 *
 * The replace half is built around a preview. A tree-wide replacement is one of
 * the few editor operations that can quietly ruin a working copy, so every line
 * that would change is shown first, per-file checkboxes decide what is
 * accepted, and the write is a separate deliberate act.
 */
export function SearchPanel() {
  const qc = useQueryClient();
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const [replacement, setReplacement] = useState("");
  const [replacing, setReplacing] = useState(false);
  const [regex, setRegex] = useState(false);
  const [caseSensitive, setCaseSensitive] = useState(false);
  const [wholeWord, setWholeWord] = useState(false);
  const [include, setInclude] = useState("");
  const [exclude, setExclude] = useState("");
  const [showFilters, setShowFilters] = useState(false);
  const [excluded, setExcluded] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  const activePath = useEditorStore((s) => s.activePath);
  const reveal = useEditorStore((s) => s.reveal);
  const focusNonce = useUIStore((s) => s.searchFocusNonce);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const options = useMemo(
    () => ({
      regex,
      case_sensitive: caseSensitive,
      whole_word: wholeWord,
      include,
      exclude,
      // Present makes the request a preview rather than a plain search.
      replace: replacing ? replacement : undefined,
    }),
    [regex, caseSensitive, wholeWord, include, exclude, replacing, replacement],
  );

  const { data, isFetching } = useQuery({
    queryKey: ["files", "search", query, options],
    queryFn: () => api.searchFiles(query, options),
    enabled: query.trim().length > 0,
  });

  // Edit ▸ Find in files opens this panel; it has to land in the field, or the
  // command only *looks* like it did something.
  useEffect(() => {
    if (!focusNonce) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [focusNonce]);

  // Debounced, so typing does not fire a tree walk per keystroke.
  useEffect(() => {
    const timer = window.setTimeout(() => setQuery(input), 250);
    return () => window.clearTimeout(timer);
  }, [input]);

  const matches = data?.matches ?? [];
  const byFile = useMemo(() => {
    const grouped = new Map<string, SearchMatch[]>();
    for (const match of matches) {
      const list = grouped.get(match.path);
      if (list) list.push(match);
      else grouped.set(match.path, [match]);
    }
    return [...grouped.entries()];
  }, [matches]);

  const accepted = byFile
    .map(([path]) => path)
    .filter((path) => !excluded.has(path));

  const apply = async () => {
    const ok = await confirmFor({
      title: `Replace in ${accepted.length} file${accepted.length === 1 ? "" : "s"}`,
      message:
        `${matches.length} occurrence${matches.length === 1 ? "" : "s"} of "${query}" ` +
        `will become "${replacement}". This writes to disk — review the diff ` +
        "afterwards, or use Source Control to discard it.",
      confirmLabel: "Replace",
      danger: true,
    });
    if (!ok) return;
    setBusy(true);
    try {
      const result = await api.replaceInFiles(query, replacement, {
        ...options,
        replace: undefined,
        paths: accepted,
      });
      if (result.errors.length) window.alert(result.errors.join("\n"));
      setExcluded(new Set());
      await qc.invalidateQueries({ queryKey: ["files", "search"] });
      await qc.invalidateQueries({ queryKey: ["git"] });
    } catch (err) {
      window.alert(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <div className="panel-header">
        Search
        <span className="spacer" />
        <button
          className={`btn icon ${replacing ? "active" : ""}`}
          title="Replace"
          onClick={() => setReplacing(!replacing)}
        >
          ⇄
        </button>
        <button
          className={`btn icon ${showFilters ? "active" : ""}`}
          title="Files to include or exclude"
          onClick={() => setShowFilters(!showFilters)}
        >
          ⚙
        </button>
      </div>

      <div className="search-form">
        <div className="search-row">
          <input
            ref={inputRef}
            className="input sm"
            placeholder="Search"
            value={input}
            onChange={(e) => setInput(e.target.value)}
          />
          <button
            className={`btn icon ${caseSensitive ? "active" : ""}`}
            title="Match case"
            onClick={() => setCaseSensitive(!caseSensitive)}
          >
            Aa
          </button>
          <button
            className={`btn icon ${wholeWord ? "active" : ""}`}
            title="Whole word"
            onClick={() => setWholeWord(!wholeWord)}
          >
            ab
          </button>
          <button
            className={`btn icon ${regex ? "active" : ""}`}
            title="Regular expression"
            onClick={() => setRegex(!regex)}
          >
            .*
          </button>
        </div>

        {replacing && (
          <div className="search-row">
            <input
              className="input sm"
              placeholder={regex ? "Replace (\\1 for groups)" : "Replace"}
              value={replacement}
              onChange={(e) => setReplacement(e.target.value)}
            />
            <button
              className="btn primary sm"
              disabled={busy || matches.length === 0 || accepted.length === 0}
              title="Write the replacement to the selected files"
              onClick={() => void apply()}
            >
              {busy ? "…" : `Replace ${matches.length || ""}`}
            </button>
          </div>
        )}

        {showFilters && (
          <>
            <input
              className="input sm"
              placeholder="Files to include — src/**, *.ts"
              value={include}
              onChange={(e) => setInclude(e.target.value)}
            />
            <input
              className="input sm"
              placeholder="Files to exclude — *.test.ts, vendor/**"
              value={exclude}
              onChange={(e) => setExclude(e.target.value)}
            />
          </>
        )}
      </div>

      <div className="search-status">
        {isFetching && "Searching…"}
        {!isFetching && data?.error && <span className="removed">{data.error}</span>}
        {!isFetching && !data?.error && query && (
          <>
            {matches.length} in {data?.files ?? byFile.length} file
            {(data?.files ?? byFile.length) === 1 ? "" : "s"}
            {data?.truncated && " (stopped at the limit)"}
            {!!data?.skipped && ` · ${data.skipped} binary or large file(s) skipped`}
          </>
        )}
      </div>

      <div className="scroll-y" style={{ flex: 1 }}>
        {!query && <div className="empty">Type to search the repository.</div>}
        {query && !isFetching && matches.length === 0 && !data?.error && (
          <div className="empty">No matches.</div>
        )}
        {byFile.map(([path, items]) => (
          <div key={path} className="search-file">
            <div className="search-file-head">
              {replacing && (
                <input
                  type="checkbox"
                  checked={!excluded.has(path)}
                  title="Include this file in the replacement"
                  onChange={() =>
                    setExcluded((current) => {
                      const next = new Set(current);
                      if (next.has(path)) next.delete(path);
                      else next.add(path);
                      return next;
                    })
                  }
                />
              )}
              <span className="mono ellipsis" title={path}>
                {path}
              </span>
              <span className="badge">{items.length}</span>
            </div>
            {items.map((match, index) => {
              const isCurrent =
                activePath === match.path && reveal?.line === match.line;
              return (
                <div
                  key={`${match.line}:${match.column}:${index}`}
                  className={`search-hit ${isCurrent ? "active" : ""}`}
                  onClick={() =>
                    void openFileInEditor(match.path, {
                      line: match.line,
                      column: match.column ?? 1,
                      length: match.length ?? 0,
                    })
                  }
                >
                  <span className="search-line">{match.line}</span>
                  <span className="search-text">{highlight(match, query)}</span>
                  {replacing && match.replacement && (
                    <span className="search-after" title="After replacement">
                      → {match.replacement.trim()}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}

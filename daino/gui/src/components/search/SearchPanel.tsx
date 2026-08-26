import { useState } from "react";
import { useSearch } from "../../api/hooks";
import { openFileInEditor } from "../../lib/openFile";

export function SearchPanel() {
  const [input, setInput] = useState("");
  const [query, setQuery] = useState("");
  const { data, isFetching } = useSearch(query);

  return (
    <div className="panel">
      <div className="panel-header">Search</div>
      <div style={{ padding: 8, borderBottom: "1px solid var(--border)" }}>
        <input
          className="input"
          placeholder="Search files…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") setQuery(input);
          }}
        />
      </div>
      <div className="panel-body">
        {isFetching && <div className="empty">Searching…</div>}
        {data && data.matches.length === 0 && !isFetching && (
          <div className="empty">No matches</div>
        )}
        {data?.matches.map((m, i) => (
          <div
            key={`${m.path}:${m.line}:${i}`}
            className="search-match"
            onClick={() => void openFileInEditor(m.path)}
          >
            <div className="path">
              {m.path}:{m.line}
            </div>
            <div className="line mono">{m.text.trim().slice(0, 200)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

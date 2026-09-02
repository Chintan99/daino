import { useReferencesStore } from "../../store/referencesStore";
import { openLocation } from "../../lib/navigation";

/**
 * Where a symbol is used, as a list you read rather than a jump you take.
 *
 * The `source` badge is load-bearing. Results from a language server are
 * semantic — they are the uses. Results from the index are text matches, which
 * include the same word in a comment or a string, and treating those as
 * references is how a "safe" refactor breaks something.
 */
export function ReferencesPanel() {
  const query = useReferencesStore((s) => s.query);
  const result = useReferencesStore((s) => s.result);
  const loading = useReferencesStore((s) => s.loading);
  const clear = useReferencesStore((s) => s.clear);

  if (!query) {
    return (
      <div className="panel">
        <div className="panel-header">References</div>
        <div className="empty">
          Put the cursor on a symbol and press Shift+F12.
        </div>
      </div>
    );
  }

  const title = query.kind === "implementations" ? "Implementations" : "References";

  return (
    <div className="panel">
      <div className="panel-header">
        {title}
        <span className="spacer" />
        <button className="btn icon" title="Clear" onClick={clear}>
          ×
        </button>
      </div>
      <div className="pad" style={{ paddingBottom: 0 }}>
        <div className="mono ellipsis muted" title={query.path}>
          {query.path}:{query.line}
        </div>
        {result && (
          <div style={{ marginTop: 4 }}>
            <span
              className={`badge ${result.source === "index" ? "warn" : ""}`}
              title={
                result.source === "index"
                  ? "Text matches from the repository index — these include " +
                    "comments and strings, and are not exact references."
                  : "Semantic results from a language server."
              }
            >
              {result.source === "index" ? "text matches" : "exact"}
            </span>{" "}
            <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
              {result.locations.length} found
            </span>
          </div>
        )}
      </div>

      <div className="scroll-y" style={{ flex: 1 }}>
        {loading && <div className="empty">Searching…</div>}
        {!loading && result && result.detail && (
          <div className="empty" style={{ textAlign: "left" }}>
            {result.detail}
          </div>
        )}
        {!loading && result && result.locations.length === 0 && !result.detail && (
          <div className="empty">Nothing uses this.</div>
        )}
        {!loading &&
          result?.locations.map((location, index) => (
            <div
              key={`${location.path}:${location.line}:${index}`}
              className="ws-doc click"
              onClick={() => void openLocation(location)}
            >
              <div className="mono ellipsis" title={location.path}>
                {location.path}
              </div>
              <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
                line {location.line}
              </div>
            </div>
          ))}
      </div>
    </div>
  );
}

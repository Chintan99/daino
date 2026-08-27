import { useEffect, useMemo, useRef, useState } from "react";
import type { CatalogModel } from "../../api/types";

/**
 * The model field.
 *
 * A hosted provider's catalog is hundreds of ids long, so it is a *searchable*
 * field: type to filter, and an unlisted id can still be entered by hand. A
 * local runtime offers the handful of models it has actually pulled, so that is
 * a plain list — there is nothing to search, and a free-text field there is how
 * you end up asking for a model that is not installed.
 */
const VISIBLE_LIMIT = 80;

export function ModelCombo({
  value,
  options,
  searchable,
  loading,
  onChange,
  onReload,
}: {
  value: string;
  options: CatalogModel[];
  searchable: boolean;
  loading: boolean;
  onChange: (value: string) => void;
  onReload: () => void;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const hostRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!hostRef.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open]);

  const matches = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const all = needle
      ? options.filter(
          (item) =>
            item.id.toLowerCase().includes(needle) ||
            item.name.toLowerCase().includes(needle),
        )
      : options;
    return { shown: all.slice(0, VISIBLE_LIMIT), total: all.length };
  }, [options, query]);

  // A plain list for a local runtime: pick from what is installed.
  if (!searchable) {
    return (
      <div className="model-field">
        {options.length > 0 ? (
          <select
            className="input"
            value={value}
            onChange={(e) => onChange(e.target.value)}
          >
            <option value="">
              {`choose from ${options.length} installed model${options.length === 1 ? "" : "s"}…`}
            </option>
            {options.map((item) => (
              <option key={item.id} value={item.id}>
                {item.id}
                {item.detail ? ` — ${item.detail}` : ""}
              </option>
            ))}
            {/* Keep a saved model visible even if it is no longer installed. */}
            {value && !options.some((item) => item.id === value) && (
              <option value={value}>{`${value} (not installed)`}</option>
            )}
          </select>
        ) : (
          <input
            className="input"
            value={value}
            placeholder={loading ? "loading installed models…" : "no models found yet"}
            onChange={(e) => onChange(e.target.value)}
          />
        )}
        <button className="btn subtle sm" disabled={loading} onClick={onReload}>
          {loading ? "Loading…" : "Reload"}
        </button>
      </div>
    );
  }

  const commit = (id: string) => {
    onChange(id);
    setQuery("");
    setOpen(false);
  };

  return (
    <div className="model-field" ref={hostRef}>
      <div className="combo">
        <input
          className="input"
          value={open ? query : value}
          placeholder={
            loading
              ? "loading catalog…"
              : options.length
                ? `search ${options.length} models…`
                : "provider/model-id"
          }
          onFocus={() => {
            setQuery("");
            setOpen(true);
          }}
          onChange={(e) => {
            setQuery(e.target.value);
            setHighlight(0);
            setOpen(true);
            // Typing is also a valid way to name a model the catalog omits.
            onChange(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              e.preventDefault();
              setOpen(true);
              setHighlight((current) => {
                const next = current + (e.key === "ArrowDown" ? 1 : -1);
                const count = matches.shown.length;
                return count ? (next + count) % count : 0;
              });
            } else if (e.key === "Enter" && open && matches.shown[highlight]) {
              e.preventDefault();
              commit(matches.shown[highlight].id);
            } else if (e.key === "Escape" && open) {
              e.preventDefault();
              setOpen(false);
            }
          }}
        />
        {open && (
          <div className="combo-list">
            {matches.shown.length === 0 && (
              <div className="combo-empty">
                {loading
                  ? "Loading…"
                  : options.length
                    ? "No model matches that."
                    : "Catalog not loaded yet."}
              </div>
            )}
            {matches.shown.map((item, index) => (
              <button
                key={item.id}
                className={`combo-item ${index === highlight ? "active" : ""} ${
                  item.id === value ? "chosen" : ""
                }`}
                onMouseEnter={() => setHighlight(index)}
                onClick={() => commit(item.id)}
              >
                <span className="grow mono">{item.id}</span>
                {item.detail && <span className="muted">{item.detail}</span>}
              </button>
            ))}
            {matches.total > matches.shown.length && (
              <div className="combo-empty">
                {`showing ${matches.shown.length} of ${matches.total} — keep typing`}
              </div>
            )}
          </div>
        )}
      </div>
      <button className="btn subtle sm" disabled={loading} onClick={onReload}>
        {loading ? "Loading…" : "Reload"}
      </button>
    </div>
  );
}

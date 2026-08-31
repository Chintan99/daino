import { useState } from "react";
import {
  COMPONENTS,
  COMPONENT_GROUPS,
  type ComponentDef,
} from "../../lib/componentLibrary";
import type { HostMessage } from "../../lib/visualEditor";

// The preview renders the block at a real page width, then scales it down so a
// hovering reader sees what the block actually looks like, not just its name.
const PREVIEW_BASE_W = 620;
const PREVIEW_BOX_W = 280;
const PREVIEW_SCALE = PREVIEW_BOX_W / PREVIEW_BASE_W;

/**
 * The blocks rail.
 *
 * Dragging is the obvious gesture, but a drag that crosses into a sandboxed
 * frame is not guaranteed to deliver its payload, so clicking a block inserts it
 * next to the selection as well. Either way the block lands.
 */
export function ComponentPalette({
  send,
  hasSelection,
  onCollapse,
  autoMatch,
  onToggleAutoMatch,
  onDragActive,
}: {
  send: (message: HostMessage) => void;
  hasSelection: boolean;
  onCollapse: () => void;
  /** When defined, show the "auto-match style" toggle (agent wiring present). */
  autoMatch?: boolean;
  onToggleAutoMatch?: (next: boolean) => void;
  /** Raised while a block is being dragged, so the editor can accept the drop. */
  onDragActive?: (active: boolean) => void;
}) {
  const [query, setQuery] = useState("");
  const [hover, setHover] = useState<{
    item: ComponentDef;
    top: number;
    left: number;
  } | null>(null);

  const needle = query.trim().toLowerCase();
  const matches = needle
    ? COMPONENTS.filter(
        (c) =>
          c.label.toLowerCase().includes(needle) ||
          c.group.toLowerCase().includes(needle),
      )
    : COMPONENTS;

  return (
    <div className="ve-palette">
      <div className="panel-header">
        Blocks
        <span className="spacer" />
        <button className="btn icon" title="Hide the blocks rail" onClick={onCollapse}>
          ‹
        </button>
      </div>
      <div style={{ padding: "8px 8px 0" }}>
        <input
          className="input"
          placeholder="Search blocks"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {onToggleAutoMatch && (
          <label className="ve-automatch" title="When on, a block you add is sent to the agent to match the page's style">
            <input
              type="checkbox"
              checked={!!autoMatch}
              onChange={(e) => onToggleAutoMatch(e.target.checked)}
            />
            <span>✨ Auto-match style on add</span>
          </label>
        )}
      </div>
      <div className="scroll-y" style={{ flex: 1, padding: "6px 8px 12px" }}>
        {COMPONENT_GROUPS.map((group) => {
          const items = matches.filter((c) => c.group === group);
          if (!items.length) return null;
          return (
            <div key={group}>
              <div className="section-title" style={{ padding: "10px 2px 4px" }}>
                {group}
              </div>
              <div className="ve-grid">
                {items.map((item) => (
                  <button
                    key={item.id}
                    className="ve-block"
                    draggable
                    title={`${item.label} — drag onto the page, or click to insert ${
                      hasSelection ? "after the selection" : "at the end"
                    }`}
                    onMouseEnter={(e) => {
                      const r = e.currentTarget.getBoundingClientRect();
                      setHover({ item, top: r.top, left: r.right + 8 });
                    }}
                    onMouseLeave={() =>
                      setHover((h) => (h?.item.id === item.id ? null : h))
                    }
                    onDragStart={() => {
                      setHover(null);
                      send({ t: "dragBegin", html: item.html });
                      onDragActive?.(true);
                    }}
                    onDragEnd={() => {
                      send({ t: "dragEnd" });
                      onDragActive?.(false);
                    }}
                    onClick={() =>
                      send({
                        t: "insert",
                        html: item.html,
                        position: hasSelection ? "after" : "end",
                      })
                    }
                  >
                    <span className="ico">{item.icon}</span>
                    <span className="lbl">{item.label}</span>
                  </button>
                ))}
              </div>
            </div>
          );
        })}
        {matches.length === 0 && <div className="empty">No blocks match.</div>}
      </div>

      {hover && (
        <div
          className="ve-block-preview"
          style={{
            top: Math.max(52, Math.min(hover.top, window.innerHeight - 260)),
            left: hover.left,
          }}
        >
          <div className="ve-block-preview-title">
            {hover.item.label}
            <span className="muted"> · {hover.item.group}</span>
          </div>
          <div className="ve-block-preview-stage">
            <div
              className="ve-block-preview-page"
              style={{
                width: PREVIEW_BASE_W,
                transform: `scale(${PREVIEW_SCALE})`,
              }}
              dangerouslySetInnerHTML={{ __html: hover.item.html }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

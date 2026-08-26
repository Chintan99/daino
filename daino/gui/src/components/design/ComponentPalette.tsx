import { useState } from "react";
import { COMPONENTS, COMPONENT_GROUPS } from "../../lib/componentLibrary";
import type { HostMessage } from "../../lib/visualEditor";

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
}: {
  send: (message: HostMessage) => void;
  hasSelection: boolean;
  onCollapse: () => void;
}) {
  const [query, setQuery] = useState("");

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
                    onDragStart={() => send({ t: "dragBegin", html: item.html })}
                    onDragEnd={() => send({ t: "dragEnd" })}
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
    </div>
  );
}

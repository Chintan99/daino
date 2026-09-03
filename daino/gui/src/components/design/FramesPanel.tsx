import { useState } from "react";
import { useDesign, useDesignMutations } from "../../api/hooks";
import { promptFor } from "../../store/dialogStore";
import type { DesignFrame, DesignFrameElement } from "../../api/types";

/**
 * The design's UI mock-up frames, drawn and editable.
 *
 * Frames were a complete server-side model — create, update, delete, nested
 * elements, versioned like everything else — that nothing on either side ever
 * touched: no agent tool made one and no view drew one, so the whole thing was
 * a parallel design model sitting dormant beside nodes and edges. This is the
 * half that was missing. The agent now has `add_design_frame` and friends, and
 * what it draws shows up here.
 *
 * Deliberately a preview and a property sheet rather than a direct-manipulation
 * editor. Dragging elements around would be a second canvas to build and
 * maintain, and the thing worth having first is being able to *see* the screen
 * the agent laid out and correct its name and size.
 */
export function FramesPanel({ designId }: { designId: string }) {
  const { data: design } = useDesign(designId);
  const m = useDesignMutations(designId);
  const [openId, setOpenId] = useState<string | null>(null);

  const frames = design?.frames ?? [];

  const add = async () => {
    const name = await promptFor({
      title: "New frame",
      hint: "Screen name, e.g. Login",
      initial: "Screen",
      confirmLabel: "Add",
    });
    if (!name?.trim()) return;
    const created = await m.addFrame.mutateAsync({ name: name.trim() });
    // Open the one just added rather than leaving it collapsed in the list.
    const latest = created.frames[created.frames.length - 1];
    if (latest) setOpenId(latest.id);
  };

  if (!design) return null;

  return (
    <div className="frames-panel">
      <div className="row" style={{ alignItems: "center", marginBottom: 6 }}>
        <label style={{ flex: 1 }}>Frames</label>
        <button className="btn subtle sm" onClick={() => void add()}>
          + Add
        </button>
      </div>

      {frames.length === 0 && (
        <div className="muted" style={{ fontSize: "var(--fs-11)" }}>
          No mock-up frames yet. Add one, or ask for a screen in the side panel.
        </div>
      )}

      {frames.map((frame) => (
        <FrameRow
          key={frame.id}
          frame={frame}
          open={openId === frame.id}
          onToggle={() => setOpenId(openId === frame.id ? null : frame.id)}
          onRename={(name) => m.patchFrame.mutate({ frameId: frame.id, body: { name } })}
          onResize={(width, height) =>
            m.patchFrame.mutate({ frameId: frame.id, body: { width, height } })
          }
          onDelete={() => {
            if (openId === frame.id) setOpenId(null);
            m.deleteFrame.mutate(frame.id);
          }}
        />
      ))}
    </div>
  );
}

function FrameRow({
  frame,
  open,
  onToggle,
  onRename,
  onResize,
  onDelete,
}: {
  frame: DesignFrame;
  open: boolean;
  onToggle: () => void;
  onRename: (name: string) => void;
  onResize: (width: number, height: number) => void;
  onDelete: () => void;
}) {
  const [name, setName] = useState(frame.name);
  const [width, setWidth] = useState(String(frame.width));
  const [height, setHeight] = useState(String(frame.height));

  const commitSize = () => {
    const w = Number.parseInt(width, 10);
    const h = Number.parseInt(height, 10);
    // A frame with no width is a frame that cannot be drawn, so a half-typed
    // or cleared field reverts rather than being written.
    if (!Number.isFinite(w) || !Number.isFinite(h) || w < 1 || h < 1) {
      setWidth(String(frame.width));
      setHeight(String(frame.height));
      return;
    }
    if (w !== frame.width || h !== frame.height) onResize(w, h);
  };

  return (
    <div className="frame-row">
      <button className="frame-row-head" onClick={onToggle}>
        <span className="frame-caret">{open ? "▾" : "▸"}</span>
        <span className="frame-name">{frame.name || frame.id}</span>
        <span className="muted frame-dims">
          {frame.width}×{frame.height}
        </span>
        <span className="muted frame-count">
          {frame.children.length} element{frame.children.length === 1 ? "" : "s"}
        </span>
      </button>

      {open && (
        <div className="frame-body">
          <FramePreview frame={frame} />
          <div className="row" style={{ marginTop: 6, alignItems: "center" }}>
            <input
              className="input sm"
              value={name}
              onChange={(e) => setName(e.target.value)}
              onBlur={() => {
                if (name !== frame.name) onRename(name);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              }}
              aria-label="Frame name"
            />
            <input
              className="input sm frame-size"
              value={width}
              onChange={(e) => setWidth(e.target.value)}
              onBlur={commitSize}
              aria-label="Frame width"
            />
            <span className="muted">×</span>
            <input
              className="input sm frame-size"
              value={height}
              onChange={(e) => setHeight(e.target.value)}
              onBlur={commitSize}
              aria-label="Frame height"
            />
            <button
              className="btn danger sm"
              onClick={onDelete}
              title="Delete this frame"
            >
              🗑
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** The frame's viewport, scaled down to fit the inspector's width. */
function FramePreview({ frame }: { frame: DesignFrame }) {
  const PANEL_WIDTH = 260;
  // Never scaled up: a 320-wide phone frame blown up to panel width would read
  // as a desktop layout, which is the one thing a mock-up must not do.
  const scale = Math.min(1, PANEL_WIDTH / Math.max(1, frame.width));
  return (
    <div
      className="frame-preview"
      style={{ width: frame.width * scale, height: frame.height * scale }}
    >
      <div
        className="frame-preview-surface"
        style={{
          width: frame.width,
          height: frame.height,
          transform: `scale(${scale})`,
        }}
      >
        {frame.children.map((element, index) => (
          <FrameElement key={element.id || index} element={element} />
        ))}
      </div>
    </div>
  );
}

function FrameElement({ element }: { element: DesignFrameElement }) {
  const type = String(element.type || "box");
  return (
    <div
      className={`frame-element frame-element-${type}`}
      style={{
        left: element.x ?? 0,
        top: element.y ?? 0,
        width: element.width ?? 200,
        height: element.height ?? 48,
      }}
      title={`${type}${element.label ? `: ${element.label}` : ""}`}
    >
      {element.label ? <span>{element.label}</span> : null}
      {/* Nested elements are positioned against this one, which is what the
          nesting is for: moving a container moves its contents. Absolute
          positioning inside a relatively-positioned parent gives that for free. */}
      {(element.children ?? []).map((child, index) => (
        <FrameElement key={child.id || index} element={child} />
      ))}
    </div>
  );
}

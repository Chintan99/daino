import { useEffect, useState } from "react";
import type { ElementInfo, HostMessage } from "../../lib/visualEditor";

const STYLE_FIELDS: { prop: string; label: string; placeholder: string }[] = [
  { prop: "color", label: "Text colour", placeholder: "inherit" },
  { prop: "background-color", label: "Background", placeholder: "transparent" },
  { prop: "font-size", label: "Font size", placeholder: "16px" },
  { prop: "padding", label: "Padding", placeholder: "0" },
  { prop: "margin", label: "Margin", placeholder: "0" },
];

const ALIGNMENTS = ["left", "center", "right"];

/** Camel-case the CSS property so it can be read back off the inline map. */
function inlineKey(prop: string): string {
  return prop.replace(/-([a-z])/g, (_m, c: string) => c.toUpperCase());
}

export function ElementInspector({
  selection,
  send,
}: {
  selection: ElementInfo | null;
  send: (message: HostMessage) => void;
}) {
  const [text, setText] = useState("");
  const [className, setClassName] = useState("");

  useEffect(() => {
    setText(selection?.text ?? "");
    setClassName(selection?.className ?? "");
  }, [selection?.path.join("."), selection?.text, selection?.className]);

  if (!selection) {
    return (
      <div className="ve-inspector">
        <div className="panel-header">Element</div>
        <div className="pad muted" style={{ fontSize: 12 }}>
          Click anything on the page to select it. Drag to move it, double-click
          to edit its text, and drag a block in from the left.
        </div>
      </div>
    );
  }

  const path = selection.path;
  const styleValue = (prop: string) =>
    selection.inline[inlineKey(prop)] ?? "";

  return (
    <div className="ve-inspector">
      <div className="panel-header">
        {selection.tag}
        <span className="spacer" />
        <button className="btn icon" title="Deselect" onClick={() => send({ t: "clear" })}>
          ✕
        </button>
      </div>

      <div className="ve-crumbs">
        {selection.crumbs.map((crumb, i) => (
          <button
            key={`${crumb.tag}-${i}`}
            className="crumb"
            onClick={() => send({ t: "select", path: crumb.path })}
            title="Select this ancestor"
          >
            {crumb.tag}
          </button>
        ))}
      </div>

      <div className="pad">
        <div className="row" style={{ marginBottom: 10, flexWrap: "wrap" }}>
          <button
            className="btn icon"
            title="Move up"
            disabled={!selection.canMoveUp}
            onClick={() => send({ t: "moveUp", path })}
          >
            ↑
          </button>
          <button
            className="btn icon"
            title="Move down"
            disabled={!selection.canMoveDown}
            onClick={() => send({ t: "moveDown", path })}
          >
            ↓
          </button>
          <button
            className="btn icon"
            title="Duplicate"
            onClick={() => send({ t: "duplicate", path })}
          >
            ⧉
          </button>
          <span className="grow" />
          <button
            className="btn danger sm"
            title="Delete this element"
            onClick={() => send({ t: "remove", path })}
          >
            Delete
          </button>
        </div>

        {selection.editableText && (
          <div className="field">
            <label>Text</label>
            <textarea
              className="input"
              rows={3}
              value={text}
              onChange={(e) => setText(e.target.value)}
              onBlur={() => send({ t: "setText", path, text })}
            />
          </div>
        )}

        {selection.tag === "a" && (
          <div className="field">
            <label>Link target</label>
            <input
              className="input"
              defaultValue={selection.href}
              onBlur={(e) =>
                send({ t: "setAttr", path, name: "href", value: e.target.value })
              }
            />
          </div>
        )}

        {(selection.tag === "img" || selection.tag === "source") && (
          <>
            <div className="field">
              <label>Source</label>
              <input
                className="input"
                defaultValue={selection.src}
                onBlur={(e) =>
                  send({ t: "setAttr", path, name: "src", value: e.target.value })
                }
              />
            </div>
            <div className="field">
              <label>Alt text</label>
              <input
                className="input"
                defaultValue={selection.alt}
                onBlur={(e) =>
                  send({ t: "setAttr", path, name: "alt", value: e.target.value })
                }
              />
            </div>
          </>
        )}

        <div className="field">
          <label>Classes</label>
          <input
            className="input"
            value={className}
            placeholder="none"
            onChange={(e) => setClassName(e.target.value)}
            onBlur={() =>
              send({ t: "setAttr", path, name: "class", value: className })
            }
          />
        </div>

        <div className="field">
          <label>Align</label>
          <div className="segmented" style={{ width: "100%" }}>
            {ALIGNMENTS.map((value) => (
              <button
                key={value}
                className={styleValue("text-align") === value ? "active" : ""}
                style={{ flex: 1 }}
                onClick={() =>
                  send({
                    t: "setStyle",
                    path,
                    prop: "text-align",
                    value: styleValue("text-align") === value ? "" : value,
                  })
                }
              >
                {value}
              </button>
            ))}
          </div>
        </div>

        {STYLE_FIELDS.map((field) => (
          <div className="field" key={field.prop}>
            <label>{field.label}</label>
            <input
              className="input"
              defaultValue={styleValue(field.prop)}
              placeholder={selection.computed[inlineKey(field.prop)] || field.placeholder}
              onBlur={(e) =>
                send({
                  t: "setStyle",
                  path,
                  prop: field.prop,
                  value: e.target.value.trim(),
                })
              }
            />
          </div>
        ))}

        <div className="field">
          <label>Insert a block</label>
          <div className="muted" style={{ fontSize: 11 }}>
            Clicking a block on the left drops it just after this element.
          </div>
        </div>
      </div>
    </div>
  );
}

import { useEffect, useState } from "react";
import type { ElementInfo, HostMessage } from "../../lib/visualEditor";

const ALIGNMENTS = ["left", "center", "right"];
const DISPLAYS = ["", "block", "flex", "inline-block", "inline", "grid", "none"];
const FLEX_DIRS = ["row", "column"];
const JUSTIFY = ["flex-start", "center", "flex-end", "space-between", "space-around"];
const ALIGN_ITEMS = ["stretch", "flex-start", "center", "flex-end"];
const FONT_WEIGHTS = ["", "300", "400", "500", "600", "700", "800"];
const BORDER_STYLES = ["", "solid", "dashed", "dotted", "none"];

/** Camel-case the CSS property so it can be read back off the inline map. */
function inlineKey(prop: string): string {
  return prop.replace(/-([a-z])/g, (_m, c: string) => c.toUpperCase());
}

/** A CSS colour (named, rgb, or hex) as a #rrggbb the colour input accepts. */
function toHex(value: string): string {
  const v = (value || "").trim();
  if (/^#[0-9a-f]{6}$/i.test(v)) return v;
  if (/^#[0-9a-f]{3}$/i.test(v))
    return "#" + v.slice(1).split("").map((c) => c + c).join("");
  const m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i.exec(v);
  if (m) {
    const h = (n: string) => ("0" + parseInt(n, 10).toString(16)).slice(-2);
    return "#" + h(m[1]) + h(m[2]) + h(m[3]);
  }
  return "#000000";
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

  const selKey = selection?.path.join(".") ?? "";
  useEffect(() => {
    setText(selection?.text ?? "");
    setClassName(selection?.className ?? "");
  }, [selKey, selection?.text, selection?.className]);

  if (!selection) {
    return (
      <div className="ve-inspector">
        <div className="panel-header">Element</div>
        <div className="pad muted" style={{ fontSize: "var(--fs-12)" }}>
          Click anything on the page to select it. Drag to move it freely,
          double-click to edit its text, and <strong>right-click</strong> for its
          edit menu. Drag a block in from the left. Delete removes it; ⌘C / ⌘V
          copy and paste; ⌘Z undoes.
        </div>
      </div>
    );
  }

  const path = selection.path;
  const inlineVal = (prop: string) => selection.inline[inlineKey(prop)] ?? "";
  const computedVal = (prop: string) => selection.computed[inlineKey(prop)] ?? "";
  const setStyle = (prop: string, value: string) =>
    send({ t: "setStyle", path, prop, value: value.trim() });

  const isFlex = (inlineVal("display") || computedVal("display")) === "flex";

  // A free-text style value (px, %, keywords, var(...)), reset per selection.
  const StyleText = ({
    prop,
    label,
    placeholder,
  }: {
    prop: string;
    label: string;
    placeholder?: string;
  }) => (
    <div className="field">
      <label>{label}</label>
      <input
        className="input"
        key={`${selKey}:${prop}`}
        defaultValue={inlineVal(prop)}
        placeholder={placeholder ?? computedVal(prop) ?? ""}
        onBlur={(e) => setStyle(prop, e.target.value)}
      />
    </div>
  );

  // A colour swatch plus a text field for named / rgb / var(...) values.
  const StyleColor = ({ prop, label }: { prop: string; label: string }) => {
    const current = inlineVal(prop) || computedVal(prop);
    return (
      <div className="field">
        <label>{label}</label>
        <div className="row" style={{ gap: 6, alignItems: "center" }}>
          <input
            type="color"
            className="swatch"
            value={toHex(current)}
            onChange={(e) => setStyle(prop, e.target.value)}
            title={label}
          />
          <input
            className="input"
            style={{ flex: 1 }}
            key={`${selKey}:${prop}`}
            defaultValue={inlineVal(prop)}
            placeholder={computedVal(prop) || "inherit"}
            onBlur={(e) => setStyle(prop, e.target.value)}
          />
          <button
            className="btn icon"
            title="Clear"
            onClick={() => setStyle(prop, "")}
          >
            ×
          </button>
        </div>
      </div>
    );
  };

  const StyleSelect = ({
    prop,
    label,
    options,
  }: {
    prop: string;
    label: string;
    options: string[];
  }) => (
    <div className="field">
      <label>{label}</label>
      <select
        className="input"
        value={inlineVal(prop)}
        onChange={(e) => setStyle(prop, e.target.value)}
      >
        {options.map((o) => (
          <option key={o || "auto"} value={o}>
            {o || `auto (${computedVal(prop) || "—"})`}
          </option>
        ))}
      </select>
    </div>
  );

  // Four compact inputs for a per-side box property (padding / margin).
  const SpacingRow = ({ base, label }: { base: string; label: string }) => (
    <div className="field">
      <label>{label}</label>
      <div className="spacing-box">
        {["top", "right", "bottom", "left"].map((side) => {
          const prop = `${base}-${side}`;
          return (
            <input
              key={`${selKey}:${prop}`}
              className="input sm"
              title={side}
              defaultValue={inlineVal(prop)}
              placeholder={computedVal(prop) || side[0].toUpperCase()}
              onBlur={(e) => setStyle(prop, e.target.value)}
            />
          );
        })}
      </div>
    </div>
  );

  return (
    <div className="ve-inspector">
      <div className="panel-header">
        {selection.tag}
        <span className="spacer" />
        <button className="btn icon" title="Deselect (Esc)" onClick={() => send({ t: "clear" })}>
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
        <div className="row" style={{ marginBottom: 4, flexWrap: "wrap" }}>
          <button
            className="btn icon"
            title="Move up (Alt+↑)"
            disabled={!selection.canMoveUp}
            onClick={() => send({ t: "moveUp", path })}
          >
            ↑
          </button>
          <button
            className="btn icon"
            title="Move down (Alt+↓)"
            disabled={!selection.canMoveDown}
            onClick={() => send({ t: "moveDown", path })}
          >
            ↓
          </button>
          <button
            className="btn icon"
            title="Duplicate (⌘D)"
            onClick={() => send({ t: "duplicate", path })}
          >
            ⧉
          </button>
          <button
            className="btn icon"
            title="Copy (⌘C)"
            onClick={() => send({ t: "copy", path })}
          >
            ⧉+
          </button>
          <button
            className="btn icon"
            title="Paste after (⌘V)"
            onClick={() => send({ t: "paste", path })}
          >
            ⇲
          </button>
          <button
            className="btn icon"
            title="Wrap in a container"
            onClick={() => send({ t: "wrap", path })}
          >
            ⧈
          </button>
          <span className="grow" />
          <button
            className="btn danger sm"
            title="Delete this element (Del)"
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
              key={`${selKey}:href`}
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
                key={`${selKey}:src`}
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
                key={`${selKey}:alt`}
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

        <div className="section-title">Typography</div>
        <div className="field">
          <label>Align</label>
          <div className="segmented" style={{ width: "100%" }}>
            {ALIGNMENTS.map((value) => (
              <button
                key={value}
                className={inlineVal("text-align") === value ? "active" : ""}
                style={{ flex: 1 }}
                onClick={() =>
                  setStyle(
                    "text-align",
                    inlineVal("text-align") === value ? "" : value,
                  )
                }
              >
                {value}
              </button>
            ))}
          </div>
        </div>
        <StyleColor prop="color" label="Text colour" />
        <div className="grid-2">
          <StyleText prop="font-size" label="Size" placeholder="16px" />
          <StyleSelect prop="font-weight" label="Weight" options={FONT_WEIGHTS} />
          <StyleText prop="line-height" label="Line height" placeholder="1.5" />
          <StyleText prop="letter-spacing" label="Letter spacing" placeholder="0" />
        </div>

        <div className="section-title">Background &amp; border</div>
        <StyleColor prop="background-color" label="Background" />
        <div className="grid-2">
          <StyleText prop="border-width" label="Border width" placeholder="0" />
          <StyleSelect prop="border-style" label="Border style" options={BORDER_STYLES} />
        </div>
        <StyleColor prop="border-color" label="Border colour" />
        <StyleText prop="border-radius" label="Corner radius" placeholder="0" />

        <div className="section-title">Size</div>
        <div className="grid-2">
          <StyleText prop="width" label="Width" placeholder="auto" />
          <StyleText prop="height" label="Height" placeholder="auto" />
        </div>

        <div className="section-title">Spacing</div>
        <SpacingRow base="padding" label="Padding (T R B L)" />
        <SpacingRow base="margin" label="Margin (T R B L)" />

        <div className="section-title">Layout</div>
        <StyleSelect prop="display" label="Display" options={DISPLAYS} />
        {isFlex && (
          <>
            <div className="grid-2">
              <StyleSelect prop="flex-direction" label="Direction" options={FLEX_DIRS} />
              <StyleText prop="gap" label="Gap" placeholder="0" />
            </div>
            <StyleSelect prop="justify-content" label="Justify" options={JUSTIFY} />
            <StyleSelect prop="align-items" label="Align items" options={ALIGN_ITEMS} />
          </>
        )}
      </div>
    </div>
  );
}

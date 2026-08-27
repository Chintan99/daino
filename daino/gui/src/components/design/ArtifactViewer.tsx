import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Editor from "@monaco-editor/react";
import ReactMarkdown from "react-markdown";
import { useDesign, useDesignMutations } from "../../api/hooks";
import { api, ApiError } from "../../api/client";
import { useAgentStore } from "../../store/agentStore";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { download, exportArtifact, exportPrototypeZip } from "../../lib/exportDesign";
import { buildFrameDoc } from "../../lib/visualEditor";
import { Menu } from "../ui/Menu";
import { useVisualEditor } from "./useVisualEditor";
import { ComponentPalette } from "./ComponentPalette";
import { ElementInspector } from "./ElementInspector";
import { BRAND } from "../../lib/branding";

type Mode = "preview" | "design" | "split" | "code";

interface Viewport {
  id: string;
  label: string;
  width: number; // 0 = fill the stage
}

const VIEWPORTS: Viewport[] = [
  { id: "fill", label: "Responsive", width: 0 },
  { id: "desktop", label: "Desktop · 1440", width: 1440 },
  { id: "laptop", label: "Laptop · 1280", width: 1280 },
  { id: "tablet", label: "Tablet · 834", width: 834 },
  { id: "mobile", label: "Mobile · 390", width: 390 },
];

const LANGUAGE: Record<string, string> = {
  html: "html",
  svg: "xml",
  markdown: "markdown",
  text: "plaintext",
};

const MODES: { id: Mode; label: string; hint: string }[] = [
  { id: "preview", label: "Preview", hint: "The page as it runs" },
  { id: "design", label: "Design", hint: "Click, drag, and drop blocks onto the page" },
  { id: "split", label: "Split", hint: "Preview above, source below" },
  { id: "code", label: "Code", hint: "The source alone" },
];

/**
 * The full-screen view of one canvas artifact.
 *
 * Opening a page should feel like opening the page, not like inspecting a card:
 * the preview gets the whole window at a real device width, the source sits
 * behind a control you pull down when you want it, and Design mode turns the
 * preview itself into the editing surface.
 */
export function ArtifactViewer({
  designId,
  nodeId,
  onClose,
  onNotice,
}: {
  designId: string;
  nodeId: string;
  onClose: () => void;
  onNotice: (message: string) => void;
}) {
  const { data: design } = useDesign(designId);
  const m = useDesignMutations(designId);
  const node = design?.nodes.find((n) => n.id === nodeId);
  const data = useMemo(
    () => (node?.data ?? {}) as Record<string, unknown>,
    [node?.data],
  );
  const kind = String(data.kind ?? "text");
  const saved = String(data.content ?? "");

  const monacoTheme = useMonacoTheme();
  const editorOptions = useEditorOptions({ minimap: { enabled: false } });
  const [mode, setMode] = useState<Mode>("preview");
  const [viewport, setViewport] = useState<Viewport>(VIEWPORTS[0]);
  const [draft, setDraft] = useState(saved);
  const [preview, setPreview] = useState(saved);
  const [reloadKey, setReloadKey] = useState(0);
  const [agentEdited, setAgentEdited] = useState(false);
  const [incoming, setIncoming] = useState<string | null>(null);
  const [stage, setStage] = useState({ w: 0, h: 0 });
  const [zoom, setZoom] = useState(1);
  const [fit, setFit] = useState(true);
  const [railOpen, setRailOpen] = useState(true);

  const stageRef = useRef<HTMLDivElement | null>(null);
  const frameRef = useRef<HTMLIFrameElement | null>(null);
  //: The source as we last saw it agree with the server, so an update that
  //: merely echoes our own save is not mistaken for one the agent made.
  const syncedRef = useRef(saved);

  const addChip = useAgentStore((s) => s.addChip);
  const removeChip = useAgentStore((s) => s.removeChip);

  const designing = mode === "design";
  const framed = kind === "html" || kind === "svg";
  const dirty = draft !== saved;

  // Adopt the stored source whenever a different artifact is opened.
  useEffect(() => {
    setDraft(saved);
    setPreview(saved);
    syncedRef.current = saved;
    setIncoming(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodeId]);

  /**
   * Follow the agent live.
   *
   * When the stored source changes underneath us — the agent rewrote this page —
   * adopt it straight away so the change is visible the moment it lands. If
   * there are unsaved local edits that adopting would throw away, hold it in a
   * banner and let the reader choose instead.
   */
  useEffect(() => {
    const previous = syncedRef.current;
    if (saved === previous) return;
    if (saved === draft) {
      syncedRef.current = saved; // our own write, echoed back
      return;
    }
    const hasLocalEdits = draft !== previous;
    syncedRef.current = saved;
    // In Design mode the frame is the live document; adopting silently would
    // reload it mid-edit, so an incoming change is always offered, never forced.
    if (hasLocalEdits || designing) {
      setIncoming(saved);
      return;
    }
    setDraft(saved);
    setPreview(saved);
    setAgentEdited(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved]);

  useEffect(() => {
    if (!agentEdited) return;
    const timer = window.setTimeout(() => setAgentEdited(false), 2600);
    return () => window.clearTimeout(timer);
  }, [agentEdited]);

  // Type, then see it: the frame follows the editor a beat behind. In Design
  // mode the frame *is* the source of truth, so it must not be re-rendered.
  useEffect(() => {
    if (designing) return;
    const timer = window.setTimeout(() => setPreview(draft), 400);
    return () => window.clearTimeout(timer);
  }, [draft, designing]);

  // Tell the agent which page is open, so "make the header bigger" has a subject.
  useEffect(() => {
    addChip({
      id: "design_artifact",
      kind: "design_node",
      label: `page: ${String(data.filename || nodeId)}`,
      payload: {
        workspace: "design",
        design_id: designId,
        node_id: nodeId,
        artifact: String(data.filename || ""),
        artifact_kind: kind,
      },
    });
    return () => removeChip("design_artifact");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [designId, nodeId, kind]);

  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) =>
      setStage({ w: entry.contentRect.width, h: entry.contentRect.height }),
    );
    observer.observe(el);
    setStage({ w: el.clientWidth, h: el.clientHeight });
    return () => observer.disconnect();
  }, [mode]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const persist = useCallback(
    (html: string) => {
      syncedRef.current = html;
      m.patchNode.mutate({ nodeId, body: { data: { ...data, content: html } } });
    },
    [data, m, nodeId],
  );

  /**
   * Direct manipulation commits itself.
   *
   * Dragging a block into place is a decision, not a keystroke, so visual edits
   * save on their own after a short pause. Typed source keeps the explicit Apply
   * button, where a half-finished tag should not be written to disk.
   */
  const saveTimer = useRef<number | null>(null);
  const onVisualChange = useCallback(
    (html: string) => {
      setDraft(html);
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
      saveTimer.current = window.setTimeout(() => persist(html), 700);
    },
    [persist],
  );

  useEffect(
    () => () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    },
    [],
  );

  const { selection, send } = useVisualEditor(frameRef, designing, onVisualChange);

  /**
   * Preview geometry.
   *
   * Scaling only the width left the frame occupying a fraction of the window —
   * a 1440px page shown at 41% was 41% as tall as the stage, and the rest was
   * wasted. The frame is therefore laid out at `stage height / zoom` so that,
   * once scaled, it fills the stage exactly, and the reader gets a zoom control
   * rather than a scale chosen for them.
   */
  const PAD = 20;
  const geometry = useMemo(() => {
    const availableW = Math.max(320, stage.w - PAD * 2);
    const availableH = Math.max(240, stage.h - PAD * 2);
    const baseW = viewport.width || availableW;
    const fitZoom = Math.min(1, availableW / baseW);
    const active = fit ? fitZoom : zoom;
    return {
      fitZoom,
      zoom: active,
      frameW: baseW,
      frameH: availableH / active,
      scaledW: baseW * active,
      scaledH: availableH,
    };
  }, [stage.w, stage.h, viewport.width, fit, zoom]);

  const setZoomTo = useCallback((next: number) => {
    setFit(false);
    setZoom(Math.min(3, Math.max(0.1, Math.round(next * 100) / 100)));
  }, []);

  // Ctrl/Cmd + wheel zooms the canvas, as it does in every design tool.
  useEffect(() => {
    const el = stageRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!e.ctrlKey && !e.metaKey) return;
      e.preventDefault();
      setFit(false);
      setZoom((current) => {
        const base = fit ? geometry.fitZoom : current;
        return Math.min(3, Math.max(0.1, base * (e.deltaY < 0 ? 1.1 : 1 / 1.1)));
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [fit, geometry.fitZoom]);

  /**
   * The document handed to the iframe.
   *
   * While designing, the frame owns the DOM: rebuilding its srcDoc would reload
   * it, throwing away the selection, the scroll position, and any in-flight
   * gesture. So the source is captured once on entering Design mode (and again
   * only when the reader asks for a reload) rather than tracked reactively.
   */
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const frameDoc = useMemo(
    () =>
      designing
        ? buildFrameDoc(draftRef.current, true)
        : buildFrameDoc(preview, false),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [designing, designing ? null : preview, reloadKey, nodeId],
  );

  if (!node) return null;

  const openInTab = () => {
    const blob = new Blob([draft], {
      type: kind === "svg" ? "image/svg+xml" : "text/html",
    });
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank", "noopener");
    // Give the new tab time to fetch before the URL is revoked.
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  const saveToProject = async () => {
    const suggested = String(data.filename || `${node.label || "artifact"}.html`);
    const path = window.prompt("Save to project path:", suggested);
    if (!path?.trim()) return;
    try {
      try {
        await api.createFile(path.trim(), false);
      } catch (err) {
        if (!(err instanceof ApiError && err.status === 409)) throw err;
      }
      const current = await api.readFile(path.trim());
      await api.writeFile(path.trim(), draft, current.hash);
      onNotice(`Saved ${path.trim()}.`);
    } catch (err) {
      onNotice(
        `Could not save: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  };

  const acceptIncoming = () => {
    if (incoming === null) return;
    setDraft(incoming);
    setPreview(incoming);
    syncedRef.current = incoming;
    setIncoming(null);
    setReloadKey((n) => n + 1);
  };

  const showPreview = mode !== "code";
  const showCode = mode === "split" || mode === "code";

  return (
    <div className="viewer">
      <div className="viewer-bar">
        <button className="btn icon" title="Back to the canvas (Esc)" onClick={onClose}>
          ‹
        </button>
        <span className="viewer-title" title={String(data.filename || node.label)}>
          {node.label || String(data.filename) || "artifact"}
        </span>
        <span className="badge">{kind}</span>
        {agentEdited && (
          <span className="badge live" title={`${BRAND} just rewrote this page`}>
            ● updated by {BRAND}
          </span>
        )}
        {designing ? (
          <span className="badge" title="Visual edits save on their own">
            {m.patchNode.isPending ? "saving…" : "auto-saved"}
          </span>
        ) : (
          dirty && <span className="badge warn">unsaved</span>
        )}

        <span className="grow" />

        {kind !== "image" && (
          <div className="segmented">
            {MODES.filter((item) => item.id !== "design" || framed).map((item) => (
              <button
                key={item.id}
                className={mode === item.id ? "active" : ""}
                title={item.hint}
                onClick={() => setMode(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
        )}

        {framed && (
          <Menu
            label={viewport.label}
            title="Preview width"
            items={VIEWPORTS.map((v) => ({
              label: v.label,
              checked: v.id === viewport.id,
              onSelect: () => setViewport(v),
            }))}
          />
        )}

        {showPreview && (
          <div className="zoombar" title="Zoom (⌘/Ctrl + scroll over the page)">
            <button
              className="btn icon"
              onClick={() => setZoomTo(geometry.zoom - 0.1)}
              title="Zoom out"
            >
              −
            </button>
            <input
              type="range"
              min={10}
              max={300}
              step={5}
              value={Math.round(geometry.zoom * 100)}
              onChange={(e) => setZoomTo(Number(e.target.value) / 100)}
              aria-label="Zoom"
            />
            <button
              className="btn icon"
              onClick={() => setZoomTo(geometry.zoom + 0.1)}
              title="Zoom in"
            >
              +
            </button>
            <button
              className={`btn sm ${fit ? "open" : ""}`}
              onClick={() => setFit(true)}
              title="Fit the page to the window"
            >
              {Math.round(geometry.zoom * 100)}%
            </button>
            <button
              className="btn subtle sm"
              onClick={() => setZoomTo(1)}
              title="Actual size"
            >
              1:1
            </button>
          </div>
        )}

        {framed && (
          <button
            className="btn icon"
            title="Reload the preview"
            onClick={() => setReloadKey((n) => n + 1)}
          >
            ⟳
          </button>
        )}
        {framed && (
          <button className="btn icon" title="Open in a new tab" onClick={openInTab}>
            ↗
          </button>
        )}

        {!designing && (
          <button
            className="btn primary sm"
            disabled={!dirty || m.patchNode.isPending}
            onClick={() => persist(draft)}
          >
            {dirty ? "Apply" : "Saved"}
          </button>
        )}

        <Menu
          label="Export"
          title="Export this artifact"
          items={[
            {
              label: `Download ${String(data.filename || "file")}`,
              onSelect: () =>
                exportArtifact({ ...node, data: { ...data, content: draft } }),
            },
            {
              label: "Prototype bundle (.zip)",
              hint: "This page as index.html, plus every other artifact",
              onSelect: () => design && exportPrototypeZip(design, nodeId),
            },
            { label: "Save to project…", onSelect: () => void saveToProject() },
            {
              label: "Copy source",
              disabled: kind === "image",
              onSelect: () => {
                void navigator.clipboard.writeText(draft);
                onNotice("Source copied to the clipboard.");
              },
            },
            {
              label: "Standalone page (.html)",
              hint: "Wrap the source in a complete document",
              disabled: kind === "image",
              onSelect: () =>
                download(
                  `${node.label || "artifact"}.html`,
                  /<html[\s>]/i.test(draft)
                    ? draft
                    : `<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>${node.label || "artifact"}</title>\n</head><body>\n${draft}\n</body></html>\n`,
                  "text/html",
                ),
            },
          ]}
        />
      </div>

      {incoming !== null && (
        <div className="viewer-notice">
          <span className="pulse" />
          <span>
            {BRAND} rewrote this page while you were editing it. Loading it will
            replace your unsaved changes.
          </span>
          <span className="grow" />
          <button className="btn sm" onClick={acceptIncoming}>
            Load {BRAND}&rsquo;s version
          </button>
          <button
            className="btn subtle sm"
            onClick={() => setIncoming(null)}
            title={`Keep editing; Apply will overwrite ${BRAND}’s version`}
          >
            Keep mine
          </button>
        </div>
      )}

      <div className={`viewer-body ${mode}`}>
        {designing &&
          (railOpen ? (
            <ComponentPalette
              send={send}
              hasSelection={!!selection}
              onCollapse={() => setRailOpen(false)}
            />
          ) : (
            <button
              className="ve-rail-tab"
              title="Show the blocks rail"
              onClick={() => setRailOpen(true)}
            >
              ⊞ Blocks
            </button>
          ))}

        {showPreview && (
          <div className="viewer-stage" ref={stageRef}>
            <div
              className="viewer-scaler"
              style={{ width: geometry.scaledW, height: geometry.scaledH }}
            >
              <div
                className="viewer-frame"
                style={{
                  width: geometry.frameW,
                  height: geometry.frameH,
                  transform: `scale(${geometry.zoom})`,
                  transformOrigin: "top left",
                }}
              >
                {framed && (
                  <iframe
                    key={`${nodeId}-${designing ? "design" : "preview"}-${reloadKey}`}
                    ref={frameRef}
                    title={node.label || nodeId}
                    sandbox="allow-scripts allow-forms allow-modals"
                    srcDoc={frameDoc}
                  />
                )}
                {kind === "image" && (
                  <div className="image-host">
                    <img src={String(data.src ?? "")} alt={node.label || "image"} />
                  </div>
                )}
                {kind === "markdown" && (
                  <div className="doc-host">
                    <ReactMarkdown>{preview}</ReactMarkdown>
                  </div>
                )}
                {kind === "text" && <pre className="doc-host mono">{preview}</pre>}
              </div>
            </div>
          </div>
        )}

        {designing && <ElementInspector selection={selection} send={send} />}

        {showCode && (
          <div className="viewer-code">
            <div className="viewer-code-bar">
              <span className="mono">{String(data.filename || "source")}</span>
              <span className="grow" />
              <button
                className="btn subtle sm"
                disabled={!dirty}
                onClick={() => setDraft(saved)}
              >
                Revert
              </button>
            </div>
            <div style={{ flex: 1, minHeight: 0 }}>
              <Editor
                value={draft}
                language={LANGUAGE[kind] ?? "plaintext"}
                theme={monacoTheme}
                onChange={(value) => setDraft(value ?? "")}
                options={editorOptions}
              />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

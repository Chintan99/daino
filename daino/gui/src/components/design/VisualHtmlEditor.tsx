import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Editor from "@monaco-editor/react";
import ReactMarkdown from "react-markdown";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { buildFrameDoc, type ElementInfo } from "../../lib/visualEditor";
import { Menu, type MenuItem } from "../ui/Menu";
import { useAgentStore, type ChipKind } from "../../store/agentStore";
import { promptFor } from "../../store/dialogStore";
import { useVisualEditor } from "./useVisualEditor";
import { ComponentPalette } from "./ComponentPalette";
import { ElementInspector } from "./ElementInspector";
import { BRAND } from "../../lib/branding";
// Registers the Daino themes and the language workers on the monaco
// instance. Imported here rather than at app start so the 4 MB editor
// arrives with the first component that renders one.
import "../../lib/monaco";

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
  { id: "design", label: "Edit", hint: "Click, drag, and drop blocks onto the page" },
  { id: "split", label: "Split", hint: "Preview above, source below" },
  { id: "code", label: "Code", hint: "The source alone" },
];

export interface EditorChip {
  id: string;
  kind: ChipKind;
  label: string;
  payload: Record<string, unknown>;
}

/**
 * A complete visual HTML editor over one source. `ArtifactViewer` backs it with
 * a canvas node. Everything here — the modes, the direct-manipulation surface,
 * the block palette, the style inspector, undo/redo, and following the agent
 * live — is what "Edit" mode offers.
 */
export function VisualHtmlEditor({
  sourceKey,
  title,
  filename,
  kind,
  imageSrc,
  saved,
  savePending = false,
  actorLabel = BRAND,
  onSave,
  onClose,
  onNotice,
  chip,
  exportItems,
  onElementAsk,
  onPageAsk,
}: {
  /** Changing this resets all editing state (a different page was opened). */
  sourceKey: string;
  title: string;
  filename: string;
  kind: string;
  imageSrc?: string;
  saved: string;
  savePending?: boolean;
  actorLabel?: string;
  onSave: (html: string) => void;
  onClose: () => void;
  onNotice: (message: string) => void;
  chip?: EditorChip | null;
  exportItems: (draft: string) => MenuItem[];
  /** Hand a scoped task about the selected element to the agent, if supported. */
  onElementAsk?: (
    kind: "match" | "improve" | "responsive" | "fill" | "ask",
    selection: ElementInfo,
    freeText?: string,
  ) => void;
  /** Hand a whole-page task to the agent (polish, responsive, theme, …). */
  onPageAsk?: (
    kind: "polish" | "responsive" | "theme" | "a11y" | "generate",
    freeText?: string,
    anchorHtml?: string,
  ) => void;
}) {
  const monacoTheme = useMonacoTheme();
  const turnRunning = useAgentStore((s) => s.turnRunning);
  const [autoMatch, setAutoMatch] = useState(false);
  const [draggingBlock, setDraggingBlock] = useState(false);
  // True from when the user hands an edit to the agent until its result lands,
  // so the surface can blur with a "work in progress" veil until it refreshes.
  const [agentEditing, setAgentEditing] = useState(false);
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
  const syncedRef = useRef(saved);
  // Set when the user asks the agent to change the open page, so the result is
  // adopted straight away rather than held behind the "load version" banner.
  const expectAgentEditRef = useRef(false);
  // Persists the latest draft immediately (assigned once refs below exist), so
  // the agent always reads the current content rather than a debounced-away one.
  const flushRef = useRef<() => void>(() => {});

  const addChip = useAgentStore((s) => s.addChip);
  const removeChip = useAgentStore((s) => s.removeChip);

  const designing = mode === "design";
  const framed = kind === "html" || kind === "svg";
  const dirty = draft !== saved;

  // Adopt the stored source whenever a different source is opened.
  useEffect(() => {
    setDraft(saved);
    setPreview(saved);
    syncedRef.current = saved;
    setIncoming(null);
    setAgentEditing(false);
    historyRef.current = [saved];
    histIndexRef.current = 0;
    syncHistoryFlags();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceKey]);

  // Follow background edits (the agent rewrote this source) — adopt them unless
  // adopting would throw away unsaved local edits, in which case offer a banner.
  useEffect(() => {
    const previous = syncedRef.current;
    if (saved === previous) return;
    if (saved === draft) {
      syncedRef.current = saved;
      return;
    }
    const hasLocalEdits = draft !== previous;
    syncedRef.current = saved;
    // A change the user explicitly requested is adopted immediately, even in
    // Edit mode (the frame rebuilds from the new source).
    if (expectAgentEditRef.current) {
      expectAgentEditRef.current = false;
      setDraft(saved);
      setPreview(saved);
      setAgentEdited(true);
      setReloadKey((n) => n + 1);
      setAgentEditing(false); // the result landed — lift the veil and refresh
      return;
    }
    // Protect genuinely unsaved local edits behind a banner; otherwise adopt the
    // agent's change live (rebuilding the frame in Edit mode) so the view just
    // refreshes — including for edits the user asked for in the chat, not a ✨
    // button.
    if (hasLocalEdits) {
      setIncoming(saved);
      return;
    }
    setDraft(saved);
    setPreview(saved);
    setAgentEdited(true);
    if (designing) setReloadKey((n) => n + 1);
    setAgentEditing(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [saved]);

  // If the turn ends without an adopted change (the agent failed, or edited
  // something else), don't leave the surface veiled.
  useEffect(() => {
    if (!turnRunning) setAgentEditing(false);
  }, [turnRunning]);

  useEffect(() => {
    if (!agentEdited) return;
    const timer = window.setTimeout(() => setAgentEdited(false), 2600);
    return () => window.clearTimeout(timer);
  }, [agentEdited]);

  useEffect(() => {
    if (designing) return;
    const timer = window.setTimeout(() => setPreview(draft), 400);
    return () => window.clearTimeout(timer);
  }, [draft, designing]);

  // Tell the agent which page is open, so "make the header bigger" has a subject.
  useEffect(() => {
    if (!chip) return;
    addChip(chip);
    return () => removeChip(chip.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceKey, chip?.id]);

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
      onSave(html);
    },
    [onSave],
  );

  // ---- Edit history (host-owned; the frame's Cmd+Z asks us to step) ----
  const historyRef = useRef<string[]>([saved]);
  const histIndexRef = useRef(0);
  const [canUndo, setCanUndo] = useState(false);
  const [canRedo, setCanRedo] = useState(false);
  const syncHistoryFlags = useCallback(() => {
    setCanUndo(histIndexRef.current > 0);
    setCanRedo(histIndexRef.current < historyRef.current.length - 1);
  }, []);
  const recordHistory = useCallback(
    (html: string) => {
      const hist = historyRef.current;
      const idx = histIndexRef.current;
      if (html === hist[idx]) return;
      const capped = hist.slice(0, idx + 1).concat(html).slice(-100);
      historyRef.current = capped;
      histIndexRef.current = capped.length - 1;
      syncHistoryFlags();
    },
    [syncHistoryFlags],
  );
  const stepHistory = useCallback(
    (dir: -1 | 1) => {
      const next = histIndexRef.current + dir;
      const hist = historyRef.current;
      if (next < 0 || next >= hist.length) return;
      histIndexRef.current = next;
      const html = hist[next];
      setDraft(html);
      setPreview(html);
      persist(html);
      setReloadKey((n) => n + 1);
      syncHistoryFlags();
    },
    [persist, syncHistoryFlags],
  );
  const undo = useCallback(() => stepHistory(-1), [stepHistory]);
  const redo = useCallback(() => stepHistory(1), [stepHistory]);

  useEffect(() => {
    if (!designing) return;
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const key = e.key.toLowerCase();
      if (mod && key === "z") {
        e.preventDefault();
        if (e.shiftKey) redo();
        else undo();
      } else if (mod && key === "y") {
        e.preventDefault();
        redo();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [designing, undo, redo]);

  const saveTimer = useRef<number | null>(null);
  const onVisualChange = useCallback(
    (html: string) => {
      setDraft(html);
      recordHistory(html);
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
      saveTimer.current = window.setTimeout(() => persist(html), 700);
    },
    [persist, recordHistory],
  );

  useEffect(
    () => () => {
      if (saveTimer.current) window.clearTimeout(saveTimer.current);
    },
    [],
  );

  const { selection, send } = useVisualEditor(
    frameRef,
    designing,
    onVisualChange,
    { onUndo: undo, onRedo: redo },
    // Auto-match: a freshly added block is handed to the agent to fit the page.
    (node) => {
      if (autoMatch && onElementAsk) {
        expectAgentEditRef.current = true;
        setAgentEditing(true);
        // Let the insert's change land and persist first, then ask the agent so
        // it reads a page that already contains the new block.
        window.setTimeout(() => {
          flushRef.current();
          onElementAsk("match", node);
        }, 150);
      }
    },
  );

  // A scoped element task: save first so the agent reads the current element.
  const askElement = (
    kind: "match" | "improve" | "responsive" | "fill" | "ask",
    free?: string,
  ) => {
    if (!selection || !onElementAsk) return;
    flushRef.current();
    expectAgentEditRef.current = true;
    setAgentEditing(true);
    onElementAsk(kind, selection, free);
  };

  // Map a host drag event over the overlay into the frame's own coordinate
  // space (undoing the preview zoom) and forward it to the editor runtime.
  const forwardBlockDrag = (
    e: React.DragEvent,
    kind: "hostDragOver" | "hostDrop",
  ) => {
    const frame = frameRef.current;
    if (!frame) return;
    const rect = frame.getBoundingClientRect();
    const z = geometry.zoom || 1;
    send({ t: kind, x: (e.clientX - rect.left) / z, y: (e.clientY - rect.top) / z });
  };

  // Whole-page action, with the current selection as an optional anchor.
  const askPage = (
    kind: "polish" | "responsive" | "theme" | "a11y" | "generate",
    freeText?: string,
  ) => {
    if (!onPageAsk) return;
    flushRef.current();
    expectAgentEditRef.current = true;
    setAgentEditing(true);
    onPageAsk(kind, freeText, selection?.html);
  };

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

  const draftRef = useRef(draft);
  draftRef.current = draft;
  flushRef.current = () => {
    if (saveTimer.current) {
      window.clearTimeout(saveTimer.current);
      saveTimer.current = null;
    }
    persist(draftRef.current);
  };
  const frameDoc = useMemo(
    () =>
      designing
        ? buildFrameDoc(draftRef.current, true)
        : buildFrameDoc(preview, false),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [designing, designing ? null : preview, reloadKey, sourceKey],
  );

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
        <button className="btn icon" title="Close the editor (Esc)" onClick={onClose}>
          ‹
        </button>
        <span className="viewer-title" title={filename || title}>
          {title || filename || "artifact"}
        </span>
        <span className="badge">{kind}</span>
        {agentEdited && (
          <span className="badge live" title={`${actorLabel} just rewrote this page`}>
            ● updated by {actorLabel}
          </span>
        )}
        {designing ? (
          <span className="badge" title="Visual edits save on their own">
            {savePending ? "saving…" : "auto-saved"}
          </span>
        ) : (
          dirty && <span className="badge warn">unsaved</span>
        )}

        <span className="grow" />

        {designing && (
          <div className="segmented" title="Undo / redo (⌘Z, ⌘⇧Z)">
            <button disabled={!canUndo} onClick={undo} title="Undo (⌘Z)">
              ↶
            </button>
            <button disabled={!canRedo} onClick={redo} title="Redo (⌘⇧Z)">
              ↷
            </button>
          </div>
        )}

        {designing && onPageAsk && (
          <Menu
            label={`✨ ${actorLabel}`}
            title={`Ask ${actorLabel} to work on the whole page`}
            items={[
              {
                label: "Polish this page",
                hint: "Spacing, hierarchy, and consistency",
                disabled: turnRunning,
                onSelect: () => askPage("polish"),
              },
              {
                label: "Make responsive",
                disabled: turnRunning,
                onSelect: () => askPage("responsive"),
              },
              {
                label: "Apply a theme…",
                hint: "e.g. dark with indigo accents",
                disabled: turnRunning,
                onSelect: async () => {
                  const t = await promptFor({
                    title: "Apply a theme",
                    hint: "Describe the colours, mood, and style",
                    placeholder: "e.g. dark with indigo accents",
                    confirmLabel: "Apply",
                  });
                  if (t?.trim()) askPage("theme", t.trim());
                },
              },
              {
                label: "Improve accessibility",
                disabled: turnRunning,
                onSelect: () => askPage("a11y"),
              },
              {
                label: "Generate a section…",
                hint: selection ? "Inserted after the selection" : "Added to the page",
                disabled: turnRunning,
                onSelect: async () => {
                  const d = await promptFor({
                    title: "Generate a section",
                    hint: selection
                      ? "It will be inserted after the selected element"
                      : "It will be added to the end of the page",
                    placeholder: "e.g. a pricing section with 3 tiers",
                    confirmLabel: "Generate",
                  });
                  if (d?.trim()) askPage("generate", d.trim());
                },
              },
            ]}
          />
        )}

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
            <button className="btn icon" onClick={() => setZoomTo(geometry.zoom - 0.1)} title="Zoom out">
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
            <button className="btn icon" onClick={() => setZoomTo(geometry.zoom + 0.1)} title="Zoom in">
              +
            </button>
            <button
              className={`btn sm ${fit ? "open" : ""}`}
              onClick={() => setFit(true)}
              title="Fit the page to the window"
            >
              {Math.round(geometry.zoom * 100)}%
            </button>
            <button className="btn subtle sm" onClick={() => setZoomTo(1)} title="Actual size">
              1:1
            </button>
          </div>
        )}

        {framed && (
          <button className="btn icon" title="Reload the preview" onClick={() => setReloadKey((n) => n + 1)}>
            ⟳
          </button>
        )}

        {!designing && (
          <button
            className="btn primary sm"
            disabled={!dirty || savePending}
            onClick={() => persist(draft)}
          >
            {dirty ? "Apply" : "Saved"}
          </button>
        )}

        <Menu label="Export" title="Export this artifact" items={exportItems(draft)} />
      </div>

      {incoming !== null && (
        <div className="viewer-notice">
          <span className="pulse" />
          <span>
            {actorLabel} rewrote this page while you were editing it. Loading it will
            replace your unsaved changes.
          </span>
          <span className="grow" />
          <button className="btn sm" onClick={acceptIncoming}>
            Load {actorLabel}&rsquo;s version
          </button>
          <button
            className="btn subtle sm"
            onClick={() => setIncoming(null)}
            title={`Keep editing; Apply will overwrite ${actorLabel}’s version`}
          >
            Keep mine
          </button>
        </div>
      )}

      <div className={`viewer-body ${mode} ${agentEditing || turnRunning ? "wip" : ""}`}>
        {(agentEditing || turnRunning) && (
          <div className="ve-wip" aria-live="polite">
            <div className="ve-wip-card">
              <span className="ve-wip-spinner" />
              <div className="ve-wip-text">
                <strong>{actorLabel} is working on this page…</strong>
                <span>Work in progress — the view refreshes when it’s done.</span>
              </div>
            </div>
          </div>
        )}
        {designing &&
          (railOpen ? (
            <ComponentPalette
              send={send}
              hasSelection={!!selection}
              onCollapse={() => setRailOpen(false)}
              autoMatch={onElementAsk ? autoMatch : undefined}
              onToggleAutoMatch={onElementAsk ? setAutoMatch : undefined}
              onDragActive={setDraggingBlock}
            />
          ) : (
            <button className="ve-rail-tab" title="Show the blocks rail" onClick={() => setRailOpen(true)}>
              ⊞ Blocks
            </button>
          ))}

        {showPreview && (
          <div className="viewer-stage" ref={stageRef}>
            <div className="viewer-scaler" style={{ width: geometry.scaledW, height: geometry.scaledH }}>
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
                    key={`${sourceKey}-${designing ? "design" : "preview"}-${reloadKey}`}
                    ref={frameRef}
                    title={title || sourceKey}
                    sandbox="allow-scripts allow-forms allow-modals"
                    srcDoc={frameDoc}
                  />
                )}
                {/* A palette drag can't cross into the sandboxed frame, so this
                    host overlay catches it and forwards the position inside. */}
                {framed && designing && draggingBlock && (
                  <div
                    className="ve-drop-overlay"
                    onDragOver={(e) => {
                      e.preventDefault();
                      e.dataTransfer.dropEffect = "copy";
                      forwardBlockDrag(e, "hostDragOver");
                    }}
                    onDrop={(e) => {
                      e.preventDefault();
                      forwardBlockDrag(e, "hostDrop");
                      setDraggingBlock(false);
                    }}
                  />
                )}
                {kind === "image" && (
                  <div className="image-host">
                    <img src={imageSrc ?? ""} alt={title || "image"} />
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

        {designing && (
          <ElementInspector
            selection={selection}
            send={send}
            agentBusy={turnRunning}
            onAsk={onElementAsk ? (kind, free) => askElement(kind, free) : undefined}
          />
        )}

        {showCode && (
          <div className="viewer-code">
            <div className="viewer-code-bar">
              <span className="mono">{filename || "source"}</span>
              <span className="grow" />
              <button className="btn subtle sm" disabled={!dirty} onClick={() => setDraft(saved)}>
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

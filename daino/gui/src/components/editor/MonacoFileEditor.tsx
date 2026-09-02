import { useEffect, useRef } from "react";
import Editor, { type OnMount } from "@monaco-editor/react";
import type { editor } from "monaco-editor";
import { useEditorStore } from "../../store/editorStore";
import { useSettingsStore } from "../../store/settingsStore";
import { saveBuffer } from "../../lib/saveFile";
import { useEditorOptions, useMonacoTheme } from "../../lib/editorPrefs";
import { registerEditor } from "../../lib/editorRegistry";
import { goToLine } from "../../lib/commands";
import {
  useProblemsStore,
  type DiagnosticSeverity,
} from "../../store/problemsStore";
import {
  fetchDiagnostics,
  releaseDiagnostics,
  requestDiagnostics,
} from "../../lib/diagnostics";
import { registerNavigation } from "../../lib/navigation";
import { registerBreakpoints } from "../../lib/breakpoints";
// Registers the Daino themes and the language workers on the monaco
// instance. Imported here rather than at app start so the 4 MB editor
// arrives with the first component that renders one.
import "../../lib/monaco";

/** Auto-save waits for a pause in typing rather than firing on every keystroke. */
const AUTO_SAVE_DELAY_MS = 1000;

/** Monaco's numeric MarkerSeverity, in the panel's terms. */
const SEVERITY: Record<number, DiagnosticSeverity> = {
  8: "error",
  4: "warning",
  2: "info",
  1: "hint",
};

export function MonacoFileEditor({ path }: { path: string }) {
  const buffer = useEditorStore((s) => s.buffers[path]);
  const setContent = useEditorStore((s) => s.setContent);
  const setSelection = useEditorStore((s) => s.setSelection);
  const reveal = useEditorStore((s) => s.reveal);
  const editorRef = useRef<editor.IStandaloneCodeEditor | null>(null);
  const unregisterRef = useRef<(() => void) | null>(null);

  // Leaving a stale editor in the registry would point the Edit and Go menus at
  // a disposed instance, so deregister on unmount as well as on dispose.
  useEffect(
    () => () => {
      unregisterRef.current?.();
      unregisterRef.current = null;
    },
    [],
  );

  const theme = useMonacoTheme();
  const minimap = useSettingsStore((s) => s.minimap);
  const lineNumbers = useSettingsStore((s) => s.lineNumbers);
  const renderWhitespace = useSettingsStore((s) => s.renderWhitespace);
  const stickyScroll = useSettingsStore((s) => s.stickyScroll);
  const tabSize = useSettingsStore((s) => s.tabSize);
  const autoSave = useSettingsStore((s) => s.autoSave);
  const dirty = !!buffer?.dirty;

  const options = useEditorOptions({
    minimap: { enabled: minimap, renderCharacters: false, maxColumn: 80 },
    lineNumbers: lineNumbers ? "on" : "off",
    renderWhitespace: renderWhitespace ? "all" : "selection",
    stickyScroll: { enabled: stickyScroll },
    tabSize,
  });

  /**
   * Scroll to, select, and briefly flash a requested location.
   *
   * Search results have to *land* somewhere: opening the file alone looks like
   * nothing happened when it was already the active tab.
   */
  useEffect(() => {
    const ed = editorRef.current;
    if (!ed || !reveal || reveal.path !== path) return;
    const { line, column, length } = reveal;
    const range = {
      startLineNumber: line,
      startColumn: column,
      endLineNumber: line,
      endColumn: length > 0 ? column + length : column,
    };
    ed.revealRangeInCenterIfOutsideViewport(range);
    ed.setPosition({ lineNumber: line, column });
    if (length > 0) ed.setSelection(range);
    ed.focus();
    const flash = ed.createDecorationsCollection([
      {
        range,
        options: {
          className: length > 0 ? "reveal-flash" : undefined,
          isWholeLine: length === 0,
          linesDecorationsClassName: "reveal-flash-gutter",
        },
      },
    ]);
    const timer = window.setTimeout(() => flash.clear(), 1600);
    return () => {
      window.clearTimeout(timer);
      flash.clear();
    };
  }, [reveal, path]);

  // Auto-save, when enabled: a pause in typing writes the buffer.
  useEffect(() => {
    if (!autoSave || !dirty) return;
    const timer = window.setTimeout(() => void saveBuffer(path), AUTO_SAVE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [autoSave, dirty, path, buffer?.content]);

  const onMount: OnMount = (ed, monaco) => {
    editorRef.current = ed;
    const unregister = registerEditor(ed);
    unregisterRef.current = unregister;
    ed.onDidDispose(unregister);
    // A file opened *by* a search result mounts after the request was made.
    const pending = useEditorStore.getState().reveal;
    if (pending && pending.path === path) {
      ed.revealLineInCenter(pending.line);
      ed.setPosition({ lineNumber: pending.line, column: pending.column });
    }
    // ⌘G matches the Go menu here too; Monaco would otherwise use it for
    // "find next", which the find widget already offers on Enter.
    ed.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyG, () => void goToLine());
    ed.onDidChangeCursorSelection((e) => {
      const sel = e.selection;
      setSelection({
        path,
        startLine: sel.startLineNumber,
        endLine: sel.endLineNumber,
      });
    });

    // Monaco's language workers publish what they find as markers. They are a
    // supplement to the language server, not a substitute: syntax for TS/JS
    // (semantic validation is off — see lib/monaco.ts) and full validation for
    // JSON, CSS and HTML, which need no project context to be right.
    const publish = () => {
      const current = ed.getModel();
      if (!current) return;
      useProblemsStore.getState().setFromEditor(
        path,
        monaco.editor
          .getModelMarkers({ resource: current.uri })
          .map((marker) => ({
            path,
            line: marker.startLineNumber,
            column: marker.startColumn,
            severity: SEVERITY[marker.severity] ?? "info",
            message: marker.message,
            source: marker.source ?? "editor",
          })),
      );
    };
    publish();
    const markers = monaco.editor.onDidChangeMarkers((resources) => {
      const uri = ed.getModel()?.uri.toString();
      if (uri && resources.some((item) => item.toString() === uri)) publish();
    });

    // Go to definition / Find references / Rename, from the language server.
    registerNavigation(ed, monaco, path);
    // Breakpoints: clicking the gutter toggles one, and the markers are drawn
    // from the server's state so they survive a reload and a restart.
    const breakpoints = registerBreakpoints(ed, monaco, path);

    // The first analysis of a freshly opened file, and then on every pause in
    // typing (the change handler below debounces).
    void fetchDiagnostics(path);

    ed.onDidDispose(() => {
      breakpoints.dispose();
      markers.dispose();
      // The file is no longer open, so its diagnostics are no longer current
      // and the server should stop tracking it.
      releaseDiagnostics(path);
    });
  };

  if (!buffer) return <div className="empty">No file open</div>;

  return (
    <Editor
      key={path}
      path={path}
      language={buffer.language}
      value={buffer.content}
      theme={theme}
      onMount={onMount}
      onChange={(value) => {
        setContent(path, value ?? "");
        // Diagnostics describe the buffer, not the last save, so every edit
        // schedules a re-analysis of the text actually on screen.
        requestDiagnostics(path);
      }}
      options={options}
    />
  );
}

// Configure @monaco-editor/react to use the locally bundled monaco-editor
// (no CDN fetch) and wire up web workers via Vite's ?worker imports so the
// editor works fully offline / same-origin.
import * as monaco from "monaco-editor";
import { loader } from "@monaco-editor/react";
import editorWorker from "monaco-editor/esm/vs/editor/editor.worker?worker";
import jsonWorker from "monaco-editor/esm/vs/language/json/json.worker?worker";
import cssWorker from "monaco-editor/esm/vs/language/css/css.worker?worker";
import htmlWorker from "monaco-editor/esm/vs/language/html/html.worker?worker";
import tsWorker from "monaco-editor/esm/vs/language/typescript/ts.worker?worker";

// eslint-disable-next-line @typescript-eslint/no-explicit-any
(self as any).MonacoEnvironment = {
  getWorker(_workerId: string, label: string) {
    if (label === "json") return new jsonWorker();
    if (label === "css" || label === "scss" || label === "less")
      return new cssWorker();
    if (label === "html" || label === "handlebars" || label === "razor")
      return new htmlWorker();
    if (label === "typescript" || label === "javascript") return new tsWorker();
    return new editorWorker();
  },
};

/** The editor theme, kept in step with the TUI's syntax palette. */
export const DAINO_THEME = "daino";

monaco.editor.defineTheme(DAINO_THEME, {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "", foreground: "b5bab7" },
    { token: "comment", foreground: "5d6462", fontStyle: "italic" },
    { token: "keyword", foreground: "b195e8" },
    { token: "keyword.control", foreground: "b195e8" },
    { token: "operator", foreground: "7f8683" },
    { token: "string", foreground: "a9cf8a" },
    { token: "string.escape", foreground: "8ad6a5" },
    { token: "number", foreground: "e8956a" },
    { token: "regexp", foreground: "5cc0cf" },
    { token: "type", foreground: "5cc0cf" },
    { token: "type.identifier", foreground: "5cc0cf" },
    { token: "identifier", foreground: "ccd1ce" },
    { token: "function", foreground: "7fc59a" },
    { token: "variable", foreground: "ccd1ce" },
    { token: "variable.parameter", foreground: "d9b271" },
    { token: "constant", foreground: "e8956a" },
    { token: "tag", foreground: "dc7a83" },
    { token: "attribute.name", foreground: "7fc59a" },
    { token: "attribute.value", foreground: "a9cf8a" },
    { token: "delimiter", foreground: "7f8683" },
    { token: "metatag", foreground: "7a9de8" },
  ],
  colors: {
    "editor.background": "#0c0e0d",
    "editor.foreground": "#b5bab7",
    "editorLineNumber.foreground": "#3d423f",
    "editorLineNumber.activeForeground": "#7f8683",
    "editorCursor.foreground": "#8ad6a5",
    "editor.selectionBackground": "#26302b",
    "editor.inactiveSelectionBackground": "#1a1f1c",
    "editor.lineHighlightBackground": "#101211",
    "editor.lineHighlightBorder": "#00000000",
    "editorIndentGuide.background1": "#1a1e1c",
    "editorIndentGuide.activeBackground1": "#2a2f2c",
    "editorWhitespace.foreground": "#2a2f2c",
    "editorGutter.background": "#0c0e0d",
    "editorGutter.addedBackground": "#68a86e",
    "editorGutter.modifiedBackground": "#d9b271",
    "editorGutter.deletedBackground": "#c96b79",
    "editorWidget.background": "#141716",
    "editorWidget.border": "#2a2f2c",
    "editorSuggestWidget.background": "#141716",
    "editorSuggestWidget.selectedBackground": "#222725",
    "editorHoverWidget.background": "#141716",
    "editorHoverWidget.border": "#2a2f2c",
    "scrollbarSlider.background": "#262b2899",
    "scrollbarSlider.hoverBackground": "#333935",
    "scrollbarSlider.activeBackground": "#2d5340",
    "minimap.background": "#0c0e0d",
    "diffEditor.insertedTextBackground": "#78be7822",
    "diffEditor.removedTextBackground": "#dc7a8322",
    "diffEditor.insertedLineBackground": "#17231a",
    "diffEditor.removedLineBackground": "#28161b",
    "diffEditor.border": "#1e2220",
    "diffEditorGutter.insertedLineBackground": "#17231a",
    "diffEditorGutter.removedLineBackground": "#28161b",
    "editorOverviewRuler.border": "#00000000",
  },
});

loader.config({ monaco });

/** Options every Monaco surface in the app shares, so they look like one editor. */
export const EDITOR_OPTIONS = {
  fontSize: 12.5,
  fontFamily:
    "'SFMono-Regular', 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace",
  lineHeight: 19,
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  automaticLayout: true,
  smoothScrolling: true,
  renderLineHighlight: "none" as const,
  padding: { top: 10, bottom: 10 },
  scrollbar: { verticalScrollbarSize: 9, horizontalScrollbarSize: 9 },
  overviewRulerBorder: false,
  guides: { indentation: true },
};

export { monaco };

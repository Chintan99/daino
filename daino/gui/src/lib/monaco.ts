// Configure @monaco-editor/react to use the locally bundled monaco-editor
// (no CDN fetch) and wire up web workers via Vite's ?worker imports so the
// editor works fully offline / same-origin.
import * as monaco from "monaco-editor";
import { loader } from "@monaco-editor/react";

import {
  DAINO_CONTRAST_THEME,
  DAINO_LIGHT_THEME,
  DAINO_THEME,
} from "./editorTheme";
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

/** The light counterpart, using the same token roles as the CSS light theme. */

monaco.editor.defineTheme(DAINO_LIGHT_THEME, {
  base: "vs",
  inherit: true,
  rules: [
    { token: "", foreground: "343a37" },
    { token: "comment", foreground: "76817c", fontStyle: "italic" },
    { token: "keyword", foreground: "6849bb" },
    { token: "keyword.control", foreground: "6849bb" },
    { token: "operator", foreground: "5e6562" },
    { token: "string", foreground: "2c7a30" },
    { token: "string.escape", foreground: "1c7a4b" },
    { token: "number", foreground: "ad541b" },
    { token: "regexp", foreground: "0e6c79" },
    { token: "type", foreground: "0e6c79" },
    { token: "type.identifier", foreground: "0e6c79" },
    { token: "identifier", foreground: "24302b" },
    { token: "function", foreground: "16603c" },
    { token: "variable", foreground: "24302b" },
    { token: "variable.parameter", foreground: "8a6714" },
    { token: "constant", foreground: "ad541b" },
    { token: "tag", foreground: "b32e3c" },
    { token: "attribute.name", foreground: "16603c" },
    { token: "attribute.value", foreground: "2c7a30" },
    { token: "delimiter", foreground: "5e6562" },
    { token: "metatag", foreground: "2a57b0" },
  ],
  colors: {
    "editor.background": "#fcfcfb",
    "editor.foreground": "#343a37",
    "editorLineNumber.foreground": "#b3bab6",
    "editorLineNumber.activeForeground": "#5e6562",
    "editorCursor.foreground": "#135e39",
    "editor.selectionBackground": "#d3e7db",
    "editor.inactiveSelectionBackground": "#e6ede9",
    "editor.lineHighlightBackground": "#f4f5f3",
    "editor.lineHighlightBorder": "#00000000",
    "editorIndentGuide.background1": "#e2e5e0",
    "editorIndentGuide.activeBackground1": "#c7ccc6",
    "editorWhitespace.foreground": "#c7ccc6",
    "editorGutter.background": "#fcfcfb",
    "editorGutter.addedBackground": "#2c7a30",
    "editorGutter.modifiedBackground": "#8a6714",
    "editorGutter.deletedBackground": "#b32e3c",
    "editorWidget.background": "#f4f5f3",
    "editorWidget.border": "#c7ccc6",
    "editorSuggestWidget.background": "#f4f5f3",
    "editorSuggestWidget.selectedBackground": "#dbe8e0",
    "editorHoverWidget.background": "#f4f5f3",
    "editorHoverWidget.border": "#c7ccc6",
    "scrollbarSlider.background": "#c9cec899",
    "scrollbarSlider.hoverBackground": "#b1b7b0",
    "scrollbarSlider.activeBackground": "#a6d2b8",
    "minimap.background": "#fcfcfb",
    "diffEditor.insertedTextBackground": "#2c7a3022",
    "diffEditor.removedTextBackground": "#b32e3c22",
    "diffEditor.insertedLineBackground": "#eaf5ea",
    "diffEditor.removedLineBackground": "#fdecee",
    "diffEditor.border": "#e2e5e0",
    "diffEditorGutter.insertedLineBackground": "#eaf5ea",
    "diffEditorGutter.removedLineBackground": "#fdecee",
    "editorOverviewRuler.border": "#00000000",
  },
});

/** Maximum separation, for the matching high-contrast interface theme. */

monaco.editor.defineTheme(DAINO_CONTRAST_THEME, {
  base: "hc-black",
  inherit: true,
  rules: [
    { token: "", foreground: "eef1ef" },
    { token: "comment", foreground: "a3aaa7", fontStyle: "italic" },
    { token: "keyword", foreground: "ceb8ff" },
    { token: "string", foreground: "b6f59a" },
    { token: "number", foreground: "ffb082" },
    { token: "type", foreground: "86e6f5" },
    { token: "function", foreground: "7ff0ad" },
    { token: "tag", foreground: "ff9aa2" },
  ],
  colors: {
    "editor.background": "#000000",
    "editor.foreground": "#eef1ef",
    "editorLineNumber.foreground": "#a3aaa7",
    "editorLineNumber.activeForeground": "#ffffff",
    "editorCursor.foreground": "#a9ffc9",
    "editor.selectionBackground": "#2c332f",
    "editorGutter.background": "#000000",
  },
});

// TypeScript/JavaScript diagnostics are syntax-only, deliberately.
//
// The TS worker has no filesystem and no node_modules, so semantic validation
// reports "Cannot find module 'react'" for every real import in the project.
// Those are artefacts of the sandbox, not problems with the code, and a
// diagnostics panel that leads with false errors is worse than one that shows
// fewer true ones. Syntax errors need no resolution to be correct, so they are
// what the Problems panel reports; the project's own type-checker is what
// INSPECTOR runs, with the whole tree available to it.
for (const defaults of [
  monaco.languages.typescript.typescriptDefaults,
  monaco.languages.typescript.javascriptDefaults,
]) {
  defaults.setDiagnosticsOptions({
    noSemanticValidation: true,
    noSyntaxValidation: false,
    noSuggestionDiagnostics: true,
  });
}

loader.config({ monaco });

export { monaco };

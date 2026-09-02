// Editor presentation constants, with no dependency on monaco-editor itself.
//
// Split out from lib/monaco.ts for one reason: that module imports the whole
// monaco-editor package as a *value* (it has to — it registers the themes on
// the instance), so anything importing a theme name from it pulled 4 MB of
// editor into the bundle. The terminal panel wants the xterm palette and the
// settings screen wants the font size; neither should cost an editor.
//
// Only names and plain objects live here. Registering the themes is still
// lib/monaco.ts's job, and it uses these same constants.

/** The registered Monaco theme names. Registration itself is lib/monaco.ts. */
export const DAINO_THEME = "daino";
export const DAINO_LIGHT_THEME = "daino-light";
export const DAINO_CONTRAST_THEME = "daino-contrast";


/** Resolve the interface theme to the editor theme that matches it. */
export function monacoThemeFor(theme: "dark" | "light" | "contrast"): string {
  if (theme === "light") return DAINO_LIGHT_THEME;
  if (theme === "contrast") return DAINO_CONTRAST_THEME;
  return DAINO_THEME;
}

/** Options every Monaco surface in the app shares, so they look like one editor. */
export const EDITOR_OPTIONS = {
  fontSize: 13.5,
  fontFamily:
    "'SFMono-Regular', 'JetBrains Mono', 'Fira Code', Menlo, Consolas, monospace",
  lineHeight: 0, // 0 lets Monaco derive it from fontSize
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

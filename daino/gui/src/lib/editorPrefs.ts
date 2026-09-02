// Live Monaco / xterm presentation derived from user settings.
//
// Every Monaco surface in the app (file editor, diff, design HTML, artifact
// viewer) reads its theme and font size from here, so one Settings change moves
// all of them instead of only the file editor.
import { useMemo } from "react";
import type { editor } from "monaco-editor";
import type { ITheme } from "@xterm/xterm";
import { EDITOR_OPTIONS, monacoThemeFor } from "./editorTheme";
import { useSettingsStore } from "../store/settingsStore";

export function useMonacoTheme(): string {
  return monacoThemeFor(useSettingsStore((s) => s.theme));
}

type Options = editor.IStandaloneEditorConstructionOptions &
  editor.IDiffEditorConstructionOptions;

/** Shared editor options with the user's font size and editor toggles applied. */
export function useEditorOptions(extra?: Options): Options {
  const fontSize = useSettingsStore((s) => s.editorFontSize);
  const wordWrap = useSettingsStore((s) => s.wordWrap);
  return useMemo(
    () => ({
      ...EDITOR_OPTIONS,
      fontSize,
      wordWrap: wordWrap ? "on" : "off",
      ...extra,
    }),
    // `extra` is written inline at every call site, so compare it by value.
    [fontSize, wordWrap, JSON.stringify(extra ?? {})],
  );
}

const XTERM_THEMES: Record<string, ITheme> = {
  dark: {
    background: "#0c0e0d",
    foreground: "#d5dad7",
    cursor: "#8ad6a5",
    cursorAccent: "#0c0e0d",
    selectionBackground: "#26302b",
    black: "#0c0e0d",
    brightBlack: "#7f8683",
    red: "#dc7a83",
    green: "#62c489",
    yellow: "#d9b271",
    blue: "#7a9de8",
    magenta: "#b195e8",
    cyan: "#5cc0cf",
    white: "#e4e7e5",
  },
  light: {
    background: "#fcfcfb",
    foreground: "#343a37",
    cursor: "#135e39",
    cursorAccent: "#fcfcfb",
    selectionBackground: "#d3e7db",
    black: "#161a18",
    brightBlack: "#5e6562",
    red: "#b32e3c",
    green: "#1c7a4b",
    yellow: "#8a6714",
    blue: "#2a57b0",
    magenta: "#6849bb",
    cyan: "#0e6c79",
    white: "#343a37",
  },
  contrast: {
    background: "#000000",
    foreground: "#eef1ef",
    cursor: "#a9ffc9",
    cursorAccent: "#000000",
    selectionBackground: "#2c332f",
    black: "#000000",
    brightBlack: "#a3aaa7",
    red: "#ff9aa2",
    green: "#7ff0ad",
    yellow: "#ffd479",
    blue: "#9fc0ff",
    magenta: "#ceb8ff",
    cyan: "#86e6f5",
    white: "#ffffff",
  },
};

export function xtermTheme(theme: string): ITheme {
  return XTERM_THEMES[theme] ?? XTERM_THEMES.dark;
}

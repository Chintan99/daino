// User preferences: appearance, editor behaviour, agent defaults, diagnostics.
//
// These are *interface* preferences and live in the browser, so they survive a
// reload without a round-trip and without writing anything into the project's
// `.daino/config.yaml`. Everything that belongs to the project or the agent
// runtime — provider, model routing, runtime, log level — is server state and
// is changed through `/api/settings` instead.
import { create } from "zustand";
import { persist } from "zustand/middleware";

export type ThemeName = "dark" | "light" | "contrast";

export const UI_FONT_MIN = 11;
export const UI_FONT_MAX = 20;
export const CODE_FONT_MIN = 9;
export const CODE_FONT_MAX = 24;

/** One point above the original 13px baseline. */
export const UI_FONT_DEFAULT = 14;
export const EDITOR_FONT_DEFAULT = 13.5;
export const TERMINAL_FONT_DEFAULT = 13;

export interface SettingsValues {
  theme: ThemeName;
  uiFontSize: number;
  editorFontSize: number;
  terminalFontSize: number;

  // Editor
  wordWrap: boolean;
  minimap: boolean;
  lineNumbers: boolean;
  renderWhitespace: boolean;
  stickyScroll: boolean;
  tabSize: number;
  autoSave: boolean;
  confirmDirtyClose: boolean;

  // Agent
  sendWithContext: boolean;
  showThinking: boolean;

  // Diagnostics
  verboseEvents: boolean;
}

const DEFAULTS: SettingsValues = {
  theme: "dark",
  uiFontSize: UI_FONT_DEFAULT,
  editorFontSize: EDITOR_FONT_DEFAULT,
  terminalFontSize: TERMINAL_FONT_DEFAULT,

  wordWrap: false,
  minimap: true,
  lineNumbers: true,
  renderWhitespace: false,
  stickyScroll: true,
  tabSize: 2,
  autoSave: false,
  confirmDirtyClose: true,

  sendWithContext: true,
  showThinking: true,

  verboseEvents: false,
};

interface SettingsState extends SettingsValues {
  set: <K extends keyof SettingsValues>(key: K, value: SettingsValues[K]) => void;
  toggle: (key: BooleanSetting) => void;
  nudgeUIFont: (delta: number) => void;
  nudgeEditorFont: (delta: number) => void;
  nudgeTerminalFont: (delta: number) => void;
  resetFonts: () => void;
  resetAll: () => void;
}

/** The keys `toggle` accepts, so a menu item cannot flip a number by mistake. */
export type BooleanSetting = {
  [K in keyof SettingsValues]: SettingsValues[K] extends boolean ? K : never;
}[keyof SettingsValues];

const clamp = (value: number, min: number, max: number) =>
  Math.min(max, Math.max(min, Math.round(value * 2) / 2));

export const useSettingsStore = create<SettingsState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      set: (key, value) => set({ [key]: value } as Partial<SettingsValues>),
      toggle: (key) => set((s) => ({ [key]: !s[key] }) as Partial<SettingsValues>),
      nudgeUIFont: (delta) =>
        set((s) => ({ uiFontSize: clamp(s.uiFontSize + delta, UI_FONT_MIN, UI_FONT_MAX) })),
      nudgeEditorFont: (delta) =>
        set((s) => ({
          editorFontSize: clamp(s.editorFontSize + delta, CODE_FONT_MIN, CODE_FONT_MAX),
        })),
      nudgeTerminalFont: (delta) =>
        set((s) => ({
          terminalFontSize: clamp(s.terminalFontSize + delta, CODE_FONT_MIN, CODE_FONT_MAX),
        })),
      resetFonts: () =>
        set({
          uiFontSize: UI_FONT_DEFAULT,
          editorFontSize: EDITOR_FONT_DEFAULT,
          terminalFontSize: TERMINAL_FONT_DEFAULT,
        }),
      resetAll: () => set({ ...DEFAULTS }),
    }),
    {
      name: "daino.settings.v1",
      // Persist the values, never the actions.
      partialize: ({ theme, uiFontSize, editorFontSize, terminalFontSize, wordWrap, minimap, lineNumbers, renderWhitespace, stickyScroll, tabSize, autoSave, confirmDirtyClose, sendWithContext, showThinking, verboseEvents }): SettingsValues => ({
        theme,
        uiFontSize,
        editorFontSize,
        terminalFontSize,
        wordWrap,
        minimap,
        lineNumbers,
        renderWhitespace,
        stickyScroll,
        tabSize,
        autoSave,
        confirmDirtyClose,
        sendWithContext,
        showThinking,
        verboseEvents,
      }),
    },
  ),
);

/**
 * Push the two settings the stylesheet owns onto the document.
 *
 * `--ui-font` is the root of the type scale in global.css, so moving it moves
 * every label, tree row, and badge together; `data-theme` swaps the palette.
 */
function applyToDocument(values: SettingsValues): void {
  const root = document.documentElement;
  root.dataset.theme = values.theme;
  root.style.setProperty("--ui-font", `${values.uiFontSize}px`);
}

applyToDocument(useSettingsStore.getState());
useSettingsStore.subscribe(applyToDocument);

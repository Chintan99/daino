// Global keyboard shortcuts.
//
// Every binding here calls the same function the matching menu item calls, and
// the hint printed in the menu is the binding written below. Editor-local keys
// (⌘F, ⌘Z, ⌘/, ⇧⌘O) are deliberately absent: Monaco already owns them while it
// has focus, and intercepting them globally would break the browser's own find
// in the panels that are not editors.
import { useEffect } from "react";
import * as cmd from "./commands";
import { cycleAutonomy, cycleModel } from "../components/agent/ComposerControls";
import { queryClient } from "./queryClient";

/** True when focus is inside a Monaco editor, which has its own keymap. */
function inEditor(): boolean {
  const active = document.activeElement;
  return !!active && !!(active as Element).closest?.(".monaco-editor");
}

export function useShortcuts(): void {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey;
      const key = e.key.toLowerCase();

      // Shift+Tab cycles autonomy and ⌘M the model, exactly as in the TUI.
      if (e.shiftKey && e.key === "Tab" && !mod && !e.altKey) {
        e.preventDefault();
        void cycleAutonomy(queryClient);
        return;
      }
      if (mod && !e.shiftKey && key === "m") {
        e.preventDefault();
        void cycleModel();
        return;
      }
      // Ctrl+` opens a terminal on every platform (⌘` is taken by the OS).
      if (e.ctrlKey && !e.metaKey && e.key === "`") {
        e.preventDefault();
        cmd.openNewTerminal();
        return;
      }
      // Alt+W closes the editor; ⌘W belongs to the browser and cannot be caught.
      if (e.altKey && !mod && key === "w") {
        e.preventDefault();
        cmd.closeActiveTab();
        return;
      }
      if (!mod) return;

      if (e.altKey && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
        e.preventDefault();
        cmd.cycleTab(e.key === "ArrowRight" ? 1 : -1);
        return;
      }

      switch (key) {
        case "b":
          e.preventDefault();
          cmd.toggleSidebar();
          break;
        case "j":
          e.preventDefault();
          cmd.toggleBottomPanel();
          break;
        case "i":
          e.preventDefault();
          cmd.toggleAgentPanel();
          break;
        case "s":
          e.preventDefault();
          if (e.shiftKey) cmd.saveAll();
          else cmd.saveActive();
          break;
        case "o":
          if (e.shiftKey) return; // ⇧⌘O is Monaco's "go to symbol"
          e.preventDefault();
          void cmd.openFileByPath();
          break;
        case "f":
          if (!e.shiftKey) return; // plain ⌘F stays with Monaco / the browser
          e.preventDefault();
          cmd.findInFiles();
          break;
        case "g":
          // Monaco binds its own ⌘G while focused (see MonacoFileEditor).
          if (inEditor()) return;
          e.preventDefault();
          void cmd.goToLine();
          break;
        case "=":
        case "+":
          e.preventDefault();
          cmd.zoomIn();
          break;
        case "-":
        case "_":
          e.preventDefault();
          cmd.zoomOut();
          break;
        case "0":
          e.preventDefault();
          cmd.zoomReset();
          break;
        default:
          break;
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

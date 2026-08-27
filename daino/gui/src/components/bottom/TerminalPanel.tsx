import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { useSettingsStore } from "../../store/settingsStore";
import { useTerminalStore } from "../../store/terminalStore";
import { useTerminalSocket } from "../../ws/useTerminalSocket";
import { xtermTheme } from "../../lib/editorPrefs";
import { registerTerminal } from "../../lib/terminalRegistry";
import { closeTerminal, createTerminal, restoreTerminals } from "../../lib/terminals";

function XtermView({ id, active }: { id: string; active: boolean }) {
  const holderRef = useRef<HTMLDivElement | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const [term, setTerm] = useState<Terminal | null>(null);
  const theme = useSettingsStore((s) => s.theme);
  const fontSize = useSettingsStore((s) => s.terminalFontSize);

  useEffect(() => {
    const holder = holderRef.current;
    if (!holder) return;
    const t = new Terminal({
      fontFamily: "'SFMono-Regular', 'JetBrains Mono', Menlo, monospace",
      fontSize: useSettingsStore.getState().terminalFontSize,
      cursorBlink: true,
      theme: xtermTheme(useSettingsStore.getState().theme),
      convertEol: true,
    });
    const fit = new FitAddon();
    t.loadAddon(fit);
    t.open(holder);
    fitRef.current = fit;
    setTerm(t);
    const unregister = registerTerminal(id, t);
    const raf = requestAnimationFrame(() => {
      try {
        fit.fit();
      } catch {
        /* ignore */
      }
    });
    const ro = new ResizeObserver(() => {
      try {
        fit.fit();
      } catch {
        /* ignore */
      }
    });
    ro.observe(holder);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      unregister();
      t.dispose();
    };
  }, [id]);

  /**
   * Restyle in place when the theme or font size changes.
   *
   * A terminal cannot be re-created for a preference change: its scrollback and
   * the PTY behind it would be lost. Writing the options and re-fitting keeps
   * both, and the re-fit is required because a font change alters the grid.
   */
  useEffect(() => {
    if (!term) return;
    term.options.theme = xtermTheme(theme);
    term.options.fontSize = fontSize;
    try {
      fitRef.current?.fit();
    } catch {
      /* the panel may be hidden; the next reveal re-fits */
    }
  }, [term, theme, fontSize]);

  useTerminalSocket(id, term);

  // refit when this terminal becomes visible again
  useEffect(() => {
    if (active && fitRef.current) {
      requestAnimationFrame(() => {
        try {
          fitRef.current?.fit();
        } catch {
          /* ignore */
        }
      });
    }
  }, [active]);

  return (
    <div
      className="term-host"
      style={{
        position: "absolute",
        inset: 0,
        display: active ? "block" : "none",
      }}
    >
      <div ref={holderRef} className="xterm-holder" />
    </div>
  );
}

export function TerminalPanel() {
  const ids = useTerminalStore((s) => s.ids);
  const activeId = useTerminalStore((s) => s.activeId);
  const setActive = useTerminalStore((s) => s.setActive);
  const error = useTerminalStore((s) => s.error);

  // Re-attach to this project's shells, or open the first one.
  useEffect(() => {
    void restoreTerminals();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="term-wrap">
      <div className="term-tabs">
        {ids.map((id, i) => (
          <div
            key={id}
            className={`term-tab ${activeId === id ? "active" : ""}`}
            onClick={() => setActive(id)}
            title={id}
          >
            <span className="grow">shell {i + 1}</span>
            <span
              className="close"
              onClick={(e) => {
                e.stopPropagation();
                void closeTerminal(id);
              }}
            >
              ✕
            </span>
          </div>
        ))}
        <button
          className="btn subtle"
          style={{ width: "100%", marginTop: 4 }}
          onClick={() => void createTerminal({ reveal: false })}
        >
          + New
        </button>
      </div>
      <div style={{ flex: 1, minWidth: 0, position: "relative" }}>
        {ids.length === 0 && !error && (
          <div className="empty">Starting a terminal…</div>
        )}
        {ids.length === 0 && error && (
          <div className="empty">
            <div>{error}</div>
            <button
              className="btn sm"
              style={{ marginTop: 10 }}
              onClick={() => void createTerminal({ reveal: false })}
            >
              Try again
            </button>
          </div>
        )}
        {ids.map((id) => (
          <XtermView key={id} id={id} active={id === activeId} />
        ))}
      </div>
    </div>
  );
}

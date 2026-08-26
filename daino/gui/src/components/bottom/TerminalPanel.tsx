import { useEffect, useRef, useState } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import { api } from "../../api/client";
import { useTerminalStore } from "../../store/terminalStore";
import { useTerminalSocket } from "../../ws/useTerminalSocket";

const THEME = {
  background: "#05070b",
  foreground: "#e6edf3",
  cursor: "#6aa1ff",
  selectionBackground: "#26314a",
  black: "#0b0e14",
  brightBlack: "#7c8798",
};

function XtermView({ id, active }: { id: string; active: boolean }) {
  const holderRef = useRef<HTMLDivElement | null>(null);
  const fitRef = useRef<FitAddon | null>(null);
  const [term, setTerm] = useState<Terminal | null>(null);

  useEffect(() => {
    const holder = holderRef.current;
    if (!holder) return;
    const t = new Terminal({
      fontFamily: "'SFMono-Regular', 'JetBrains Mono', Menlo, monospace",
      fontSize: 12,
      cursorBlink: true,
      theme: THEME,
      convertEol: true,
    });
    const fit = new FitAddon();
    t.loadAddon(fit);
    t.open(holder);
    fitRef.current = fit;
    setTerm(t);
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
      t.dispose();
    };
  }, []);

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
  const addTerminal = useTerminalStore((s) => s.addTerminal);
  const removeTerminal = useTerminalStore((s) => s.removeTerminal);
  const setActive = useTerminalStore((s) => s.setActive);
  const creatingRef = useRef(false);

  const createTerminal = async () => {
    if (creatingRef.current) return;
    creatingRef.current = true;
    try {
      const res = await api.createTerminal();
      addTerminal(res.id);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.error("createTerminal failed", err);
    } finally {
      creatingRef.current = false;
    }
  };

  const closeTerminal = async (id: string) => {
    removeTerminal(id);
    try {
      await api.deleteTerminal(id);
    } catch {
      /* ignore */
    }
  };

  // auto-create the first terminal
  useEffect(() => {
    if (ids.length === 0) void createTerminal();
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
          onClick={() => void createTerminal()}
        >
          + New
        </button>
      </div>
      <div style={{ flex: 1, minWidth: 0, position: "relative" }}>
        {ids.length === 0 && (
          <div className="empty">Starting a terminal…</div>
        )}
        {ids.map((id) => (
          <XtermView key={id} id={id} active={id === activeId} />
        ))}
      </div>
    </div>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import type {
  ElementInfo,
  FrameMessage,
  HostMessage,
} from "../../lib/visualEditor";

/**
 * The host half of the visual-editor bridge.
 *
 * Messages are only trusted when they come from this exact frame's window — the
 * frame's origin is opaque, so `event.origin` is "null" and identity has to be
 * established by source rather than by origin.
 */
export function useVisualEditor(
  frameRef: React.RefObject<HTMLIFrameElement | null>,
  enabled: boolean,
  onChange: (html: string) => void,
  history?: { onUndo: () => void; onRedo: () => void },
  onInserted?: (node: ElementInfo) => void,
) {
  const [selection, setSelection] = useState<ElementInfo | null>(null);
  const [ready, setReady] = useState(false);
  const changeRef = useRef(onChange);
  changeRef.current = onChange;
  const insertedRef = useRef(onInserted);
  insertedRef.current = onInserted;
  const historyRef = useRef(history);
  historyRef.current = history;
  const selectionRef = useRef<ElementInfo | null>(null);
  selectionRef.current = selection;

  useEffect(() => {
    if (!enabled) {
      setSelection(null);
      setReady(false);
      return;
    }
    const onMessage = (event: MessageEvent) => {
      if (!frameRef.current || event.source !== frameRef.current.contentWindow)
        return;
      const msg = event.data as FrameMessage;
      if (!msg || typeof msg.t !== "string") return;
      if (msg.t === "ready") {
        setReady(true);
        // The frame reloaded (agent update, manual refresh): put the reader
        // back on whatever they had selected instead of dropping them.
        const previous = selectionRef.current;
        if (previous)
          frameRef.current?.contentWindow?.postMessage(
            { t: "select", path: previous.path },
            "*",
          );
      }
      else if (msg.t === "selected") setSelection(msg.node);
      else if (msg.t === "deselected") setSelection(null);
      else if (msg.t === "changed") changeRef.current(msg.html);
      else if (msg.t === "undo") historyRef.current?.onUndo();
      else if (msg.t === "redo") historyRef.current?.onRedo();
      else if (msg.t === "inserted") insertedRef.current?.(msg.node);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [enabled, frameRef]);

  const send = useCallback(
    (message: HostMessage) => {
      frameRef.current?.contentWindow?.postMessage(message, "*");
    },
    [frameRef],
  );

  return { selection, ready, send, setSelection };
}

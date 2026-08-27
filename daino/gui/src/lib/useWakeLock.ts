// Keep the display awake in the browser while a turn runs.
//
// The server already holds an OS sleep inhibitor for the machine (see
// daino/keepawake.py), which is what keeps the work itself alive. This is the
// other half: the Screen Wake Lock keeps the *display* on while the IDE is the
// visible tab, so a long turn can be watched without the screen dimming. The
// browser releases the lock automatically when the tab is hidden, so it is
// re-acquired on visibility changes.
import { useEffect } from "react";
import { useAgentStore } from "../store/agentStore";
import { isRunning } from "./activity";

interface WakeLockSentinel {
  released: boolean;
  release: () => Promise<void>;
}
interface WakeLockNavigator {
  wakeLock?: { request: (type: "screen") => Promise<WakeLockSentinel> };
}

export function useWakeLock(enabled: boolean): void {
  const running = useAgentStore((s) => isRunning(s.activity.state) || s.turnRunning);

  useEffect(() => {
    const api = (navigator as Navigator & WakeLockNavigator).wakeLock;
    if (!enabled || !running || !api) return;

    let sentinel: WakeLockSentinel | null = null;
    let disposed = false;

    const acquire = async () => {
      if (disposed || document.visibilityState !== "visible") return;
      try {
        sentinel = await api.request("screen");
      } catch {
        // Denied, or the tab lost visibility mid-request; the server-side
        // inhibitor still keeps the machine from suspending.
      }
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible" && (!sentinel || sentinel.released)) {
        void acquire();
      }
    };

    void acquire();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onVisibility);
      const held = sentinel;
      sentinel = null;
      if (held && !held.released) void held.release().catch(() => {});
    };
  }, [enabled, running]);
}

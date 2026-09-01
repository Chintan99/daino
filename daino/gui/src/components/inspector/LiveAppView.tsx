import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { usePreviewDetect, usePreviewStatus, qk } from "../../api/hooks";
import { api, ApiError } from "../../api/client";

/**
 * Runs the project and shows it, as the Preview workspace used to.
 *
 * It lives under the Inspector because a running app is the other half of an
 * end-to-end inspection: the Scan view's live probe points at whatever this
 * view started, so "see it working" and "check what it exposes" are the same
 * two clicks instead of two unrelated workspaces.
 */
export function LiveAppView() {
  const qc = useQueryClient();
  const { data: detect } = usePreviewDetect();
  const { data: status } = usePreviewStatus(2000);

  const [command, setCommand] = useState("");
  const [url, setUrl] = useState("");
  const [reloadKey, setReloadKey] = useState(0);
  const [busy, setBusy] = useState(false);

  const commands = detect?.commands ?? [];

  // seed the command/url fields from the first detected candidate
  useEffect(() => {
    if (!command && commands.length > 0) {
      setCommand(commands[0].command);
      setUrl(commands[0].default_url);
    }
  }, [commands, command]);

  // when running, reflect the server's url
  useEffect(() => {
    if (status?.running && status.url) setUrl(status.url);
  }, [status?.running, status?.url]);

  const running = !!status?.running;

  const start = async () => {
    setBusy(true);
    try {
      await api.previewStart(command, url);
      await qc.invalidateQueries({ queryKey: qk.previewStatus });
    } catch (err) {
      if (err instanceof ApiError && err.status === 403)
        window.alert("The command was denied by the backend policy.");
      else
        window.alert(
          `Failed to start the app: ${err instanceof Error ? err.message : String(err)}`,
        );
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    setBusy(true);
    try {
      await api.previewStop();
      await qc.invalidateQueries({ queryKey: qk.previewStatus });
    } finally {
      setBusy(false);
    }
  };

  const onPickCommand = (value: string) => {
    setCommand(value);
    const match = commands.find((c) => c.command === value);
    if (match) setUrl(match.default_url);
  };

  return (
    <div className="preview-workspace">
      <div className="preview-bar">
        <select
          className="model-picker"
          value={command}
          onChange={(e) => onPickCommand(e.target.value)}
          style={{ maxWidth: 260 }}
          disabled={running}
        >
          {commands.length === 0 && (
            <option value="">no commands detected</option>
          )}
          {commands.map((c) => (
            <option key={c.command} value={c.command}>
              {c.label}
            </option>
          ))}
        </select>
        <input
          className="input"
          style={{ maxWidth: 260 }}
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="http://localhost:3000"
        />
        {running ? (
          <button className="btn danger" onClick={() => void stop()} disabled={busy}>
            Stop
          </button>
        ) : (
          <button
            className="btn primary"
            onClick={() => void start()}
            disabled={busy || !command}
          >
            Start
          </button>
        )}
        <button
          className="btn subtle"
          onClick={() => setReloadKey((k) => k + 1)}
          disabled={!running}
          title="Reload"
        >
          ⟳
        </button>
        <button
          className="btn subtle"
          onClick={() => url && window.open(url, "_blank")}
          disabled={!url}
          title="Open in new tab"
        >
          ↗
        </button>
        <span className="grow" />
        {running && (
          <span className="muted" style={{ fontSize: "var(--fs-11)" }}>
            SCAN will probe this URL
          </span>
        )}
        <span className="badge">{running ? "running" : "stopped"}</span>
      </div>

      {running && url ? (
        <iframe
          key={reloadKey}
          className="preview-frame"
          src={url}
          title="Live app"
        />
      ) : (
        <div className="empty" style={{ margin: "auto" }}>
          {commands.length === 0
            ? "No runnable command was detected for this project."
            : "Choose a command and press Start. The running app becomes the Scan view's live target."}
        </div>
      )}

      {status && status.logs.length > 0 && (
        <div className="preview-logs">
          {status.logs.slice(-200).map((line, i) => (
            <div key={i} className="log-line">
              {line}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

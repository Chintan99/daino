import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { usePreviewDetect, usePreviewStatus, qk } from "../../api/hooks";
import { api, ApiError } from "../../api/client";

export function PreviewWorkspace() {
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
        window.alert("Preview command was denied by the backend policy.");
      else
        window.alert(
          `Failed to start preview: ${err instanceof Error ? err.message : String(err)}`,
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
          {commands.length === 0 && <option value="">no commands detected</option>}
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
        <span className="badge">{running ? "running" : "stopped"}</span>
      </div>

      {running && url ? (
        <iframe
          key={reloadKey}
          className="preview-frame"
          src={url}
          title="Preview"
        />
      ) : (
        <div className="empty" style={{ margin: "auto" }}>
          {commands.length === 0
            ? "No runnable commands were detected for this project."
            : "Choose a command and press Start to preview your app."}
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

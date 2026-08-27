import { useEffect, useState } from "react";
import { useDialogStore, type PromptRequest, type InfoRequest } from "../../store/dialogStore";

/** Host for the app's single dialog slot; mounted once by the shell. */
export function Dialogs() {
  const request = useDialogStore((s) => s.request);
  if (!request) return null;
  if (request.kind === "prompt") return <PromptDialog request={request} />;
  return <InfoDialog request={request} />;
}

function PromptDialog({ request }: { request: PromptRequest }) {
  const close = useDialogStore((s) => s.close);
  const [value, setValue] = useState(request.initial);

  // A new request reuses this component; reset to its initial value.
  useEffect(() => setValue(request.initial), [request]);

  const finish = (result: string | null) => {
    request.resolve(result);
    close();
  };

  return (
    <div className="dialog-backdrop" onMouseDown={() => finish(null)}>
      <div className="dialog" onMouseDown={(e) => e.stopPropagation()}>
        <h3>{request.title}</h3>
        {request.hint && <div className="muted">{request.hint}</div>}
        <input
          className="input"
          autoFocus
          value={value}
          placeholder={request.placeholder}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") finish(value);
            if (e.key === "Escape") finish(null);
          }}
        />
        <div className="actions">
          <button className="btn subtle" onClick={() => finish(null)}>
            Cancel
          </button>
          <button className="btn primary" onClick={() => finish(value)}>
            {request.confirmLabel ?? "OK"}
          </button>
        </div>
      </div>
    </div>
  );
}

function InfoDialog({ request }: { request: InfoRequest }) {
  const close = useDialogStore((s) => s.close);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  return (
    <div className="dialog-backdrop" onMouseDown={close}>
      <div
        className="dialog"
        style={{ width: 520, maxHeight: "76vh", overflowY: "auto" }}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <h3>{request.title}</h3>
        {request.hint && <div className="muted">{request.hint}</div>}
        {request.sections.map((section) => (
          <div key={section.heading} className="vstack" style={{ gap: 4 }}>
            <div className="section-title" style={{ padding: "6px 0 2px" }}>
              {section.heading}
            </div>
            {section.rows.map((row) => (
              <div key={row.label} className="row" style={{ gap: 12 }}>
                <span className="grow">{row.label}</span>
                <span className="mono muted">{row.value}</span>
              </div>
            ))}
          </div>
        ))}
        <div className="actions">
          <button className="btn primary" onClick={close}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

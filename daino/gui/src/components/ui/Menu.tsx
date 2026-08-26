import { useEffect, useRef, useState } from "react";

export interface MenuItem {
  label: string;
  hint?: string;
  checked?: boolean;
  disabled?: boolean;
  danger?: boolean;
  onSelect: () => void;
}

/** A small dropdown button — the app's one menu idiom. */
export function Menu({
  label,
  title,
  items,
  align = "right",
}: {
  label: string;
  title?: string;
  items: MenuItem[];
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const hostRef = useRef<HTMLDivElement | null>(null);

  // Close on an outside click or Escape, the way a menu is expected to behave.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!hostRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        setOpen(false);
      }
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [open]);

  return (
    <div className="menu-host" ref={hostRef}>
      <button
        className={`btn sm ${open ? "open" : ""}`}
        title={title}
        onClick={() => setOpen(!open)}
      >
        {label}
        <span className="caret">▾</span>
      </button>
      {open && (
        <div className={`menu ${align}`} role="menu">
          {items.map((item) => (
            <button
              key={item.label}
              className={`menu-item ${item.danger ? "danger" : ""}`}
              disabled={item.disabled}
              role="menuitem"
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
            >
              <span className="tick">{item.checked ? "✓" : ""}</span>
              <span className="grow">
                {item.label}
                {item.hint && <span className="hint">{item.hint}</span>}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

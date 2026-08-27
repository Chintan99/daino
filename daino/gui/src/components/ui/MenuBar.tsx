import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

/**
 * The application menu bar.
 *
 * Menus are data — see `menus/useAppMenus` — so a new command is one entry in a
 * list rather than a new piece of layout. Behaviour follows the desktop
 * convention users already have in their fingers: click to open, slide sideways
 * to switch menus, arrow keys to walk items, Escape to close.
 */
export type MenuNode =
  | { type: "separator" }
  | { type: "label"; text: string }
  | {
      type: "item";
      label: string;
      shortcut?: string;
      hint?: string;
      checked?: boolean;
      disabled?: boolean;
      danger?: boolean;
      onSelect: () => void;
    }
  | {
      type: "submenu";
      label: string;
      /** Rendered on the right, where a shortcut would be: the current value. */
      value?: string;
      hint?: string;
      disabled?: boolean;
      items: MenuNode[];
    };

export interface MenuDefinition {
  id: string;
  label: string;
  items: MenuNode[];
}

export function MenuBar({ menus }: { menus: MenuDefinition[] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  const hostRef = useRef<HTMLDivElement | null>(null);

  const close = useCallback(() => setOpenId(null), []);

  useEffect(() => {
    if (!openId) return;
    const onDown = (e: MouseEvent) => {
      const target = e.target as HTMLElement | null;
      // Submenus render in a portal, so they are outside `hostRef` while still
      // being part of the open menu.
      if (target?.closest?.(".menu-portal")) return;
      if (!hostRef.current?.contains(target)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      close();
    };
    window.addEventListener("mousedown", onDown);
    window.addEventListener("keydown", onKey, true);
    return () => {
      window.removeEventListener("mousedown", onDown);
      window.removeEventListener("keydown", onKey, true);
    };
  }, [openId, close]);

  const step = (delta: number) => {
    const index = menus.findIndex((m) => m.id === openId);
    if (index < 0) return;
    setOpenId(menus[(index + delta + menus.length) % menus.length].id);
  };

  return (
    <div className="menu-roots" ref={hostRef} style={{ display: "flex", gap: 2 }}>
      {menus.map((menu) => {
        const open = openId === menu.id;
        return (
          <div className="menu-root" key={menu.id}>
            <button
              className={`menu-trigger ${open ? "open" : ""}`}
              aria-haspopup="menu"
              aria-expanded={open}
              onClick={() => setOpenId(open ? null : menu.id)}
              // Once a menu is open, moving across the bar switches menus
              // without a second click — the desktop behaviour.
              onMouseEnter={() => openId && setOpenId(menu.id)}
              onKeyDown={(e) => {
                if (e.key === "ArrowLeft") {
                  e.preventDefault();
                  step(-1);
                } else if (e.key === "ArrowRight") {
                  e.preventDefault();
                  step(1);
                } else if (e.key === "ArrowDown" && !open) {
                  e.preventDefault();
                  setOpenId(menu.id);
                }
              }}
            >
              {menu.label}
            </button>
            {open && (
              <MenuPanel items={menu.items} onClose={close} className="menu top" />
            )}
          </div>
        );
      })}
    </div>
  );
}

function MenuPanel({
  items,
  onClose,
  className,
}: {
  items: MenuNode[];
  onClose: () => void;
  className: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [openSub, setOpenSub] = useState<number | null>(null);

  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const buttons = Array.from(
      ref.current?.querySelectorAll<HTMLButtonElement>(
        ":scope > button.menu-item:not(:disabled), :scope > .menu-sub > button.menu-item:not(:disabled)",
      ) ?? [],
    );
    if (!buttons.length) return;
    const current = buttons.indexOf(document.activeElement as HTMLButtonElement);
    const delta = e.key === "ArrowDown" ? 1 : -1;
    const next = (current + delta + buttons.length) % buttons.length;
    buttons[next].focus();
  };

  return (
    <div className={className} role="menu" ref={ref} onKeyDown={onKeyDown}>
      {items.map((node, index) => {
        if (node.type === "separator")
          // eslint-disable-next-line react/no-array-index-key
          return <div className="menu-sep" key={`sep-${index}`} />;
        if (node.type === "label")
          return (
            <div className="menu-label" key={`label-${node.text}`}>
              {node.text}
            </div>
          );
        if (node.type === "submenu")
          return (
            <SubMenu
              key={node.label}
              node={node}
              open={openSub === index}
              onOpen={() => setOpenSub(index)}
              onClose={onClose}
            />
          );
        return (
          <button
            key={node.label}
            className={`menu-item ${node.danger ? "danger" : ""} ${
              node.checked ? "checked" : ""
            }`}
            role="menuitem"
            disabled={node.disabled}
            onMouseEnter={() => setOpenSub(null)}
            onClick={() => {
              onClose();
              node.onSelect();
            }}
          >
            <span className="tick">{node.checked ? "✓" : ""}</span>
            <span className="grow">
              {node.label}
              {node.hint && <span className="hint">{node.hint}</span>}
            </span>
            {node.shortcut && <span className="kbd">{node.shortcut}</span>}
          </button>
        );
      })}
    </div>
  );
}

function SubMenu({
  node,
  open,
  onOpen,
  onClose,
}: {
  node: Extract<MenuNode, { type: "submenu" }>;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
}): ReactNode {
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [at, setAt] = useState<{ left: number; top: number } | null>(null);

  /**
   * Submenus are positioned in a portal rather than nested in the DOM.
   *
   * The parent panel scrolls (the Settings menu is long), and a scroll
   * container clips absolutely-positioned children — a nested submenu simply
   * disappeared. Fixed coordinates also let a submenu flip left near the right
   * edge and lift up near the bottom instead of running off-screen.
   */
  useLayoutEffect(() => {
    if (!open) {
      setAt(null);
      return;
    }
    const trigger = triggerRef.current?.getBoundingClientRect();
    if (!trigger) return;
    const panel = panelRef.current?.getBoundingClientRect();
    const width = panel?.width || 256;
    const height = panel?.height || 0;
    const gap = 2;
    const left =
      trigger.right + gap + width > window.innerWidth - 8
        ? Math.max(8, trigger.left - width - gap)
        : trigger.right + gap;
    const top = Math.max(
      8,
      Math.min(trigger.top - 5, window.innerHeight - height - 8),
    );
    setAt((current) =>
      current && current.left === left && current.top === top
        ? current
        : { left, top },
    );
  }, [open, node.items.length]);

  return (
    <div className="menu-sub" onMouseEnter={onOpen}>
      <button
        ref={triggerRef}
        className="menu-item"
        role="menuitem"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={node.disabled}
        onClick={onOpen}
        onKeyDown={(e) => {
          if (e.key === "ArrowRight" || e.key === "Enter") onOpen();
        }}
      >
        <span className="tick" />
        <span className="grow">
          {node.label}
          {node.hint && <span className="hint">{node.hint}</span>}
        </span>
        {node.value && <span className="kbd">{node.value}</span>}
        <span className="arrow">›</span>
      </button>
      {open &&
        createPortal(
          <div
            className="menu-portal"
            ref={panelRef}
            style={{
              position: "fixed",
              left: at?.left ?? -9999,
              top: at?.top ?? -9999,
              // Hidden for the one frame before it has been measured.
              visibility: at ? "visible" : "hidden",
              zIndex: 90,
            }}
          >
            <MenuPanel items={node.items} onClose={onClose} className="menu floating" />
          </div>,
          document.body,
        )}
    </div>
  );
}

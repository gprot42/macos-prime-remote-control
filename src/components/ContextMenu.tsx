import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

export interface ContextMenuItem {
  label: string;
  icon?: React.ReactNode;
  onClick: () => void;
  destructive?: boolean;
}

interface ContextMenuProps {
  x: number;
  y: number;
  items: ContextMenuItem[];
  onClose: () => void;
}

export default function ContextMenu({ x, y, items, onClose }: ContextMenuProps) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const handleClick = (e: MouseEvent) => {
      // Ignore right/middle button — macOS can emit these around contextmenu.
      if (e.button !== 0) return;
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };

    const handleContextMenu = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };

    const timer = window.setTimeout(() => {
      document.addEventListener("keydown", handleKey);
      document.addEventListener("mousedown", handleClick);
      document.addEventListener("contextmenu", handleContextMenu);
    }, 0);

    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", handleKey);
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("contextmenu", handleContextMenu);
    };
  }, [onClose]);

  const menuWidth = 220;
  const menuHeight = items.length * 36 + 8;
  const left = Math.min(x, window.innerWidth - menuWidth - 8);
  const top = Math.min(y, window.innerHeight - menuHeight - 8);

  return createPortal(
    <div
      ref={ref}
      className="fixed z-[9999] min-w-[220px] py-1 bg-zinc-900 border border-zinc-700
                 rounded-lg shadow-xl shadow-black/50"
      style={{ left, top }}
      onContextMenu={(e) => e.preventDefault()}
    >
      {items.map((item) => (
        <button
          key={item.label}
          onClick={() => {
            item.onClick();
            onClose();
          }}
          className={`w-full flex items-center gap-2.5 px-3 py-2 text-sm text-left
                      transition-colors ${
                        item.destructive
                          ? "text-red-400 hover:bg-red-950/50"
                          : "text-zinc-200 hover:bg-zinc-800"
                      }`}
        >
          {item.icon && <span className="w-4 h-4 shrink-0 opacity-80">{item.icon}</span>}
          {item.label}
        </button>
      ))}
    </div>,
    document.body
  );
}
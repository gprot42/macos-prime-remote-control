import { useState, useEffect, useRef } from "react";

/**
 * Custom horizontal seek bar — pointer events on a plain div, no <input type="range">.
 * Works reliably in Tauri's WKWebView (avoids controlled-range snap-back).
 */
export default function HorizontalSeekBar({
  value,
  max,
  knownDuration,
  disabled = false,
  onChange,
  onCommit,
}: {
  value: number;
  max: number;
  knownDuration: boolean;
  disabled?: boolean;
  onChange: (v: number) => void;
  onCommit: (v: number) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  const [local, setLocal] = useState(value);

  useEffect(() => {
    if (!draggingRef.current) setLocal(value);
  }, [value]);

  const fromPointer = (clientX: number): number => {
    if (!trackRef.current) return local;
    const rect = trackRef.current.getBoundingClientRect();
    if (rect.width <= 0) return local;
    const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return Math.round(pct * max);
  };

  const finishDrag = (clientX: number) => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    const v = fromPointer(clientX);
    setLocal(v);
    onCommit(v);
  };

  const pct = max > 0 ? Math.min(100, (local / max) * 100) : 0;
  const fillColor = knownDuration ? "#10b981" : "#4b5563";

  return (
    <div
      ref={trackRef}
      onPointerDown={(e) => {
        if (disabled) return;
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        draggingRef.current = true;
        const v = fromPointer(e.clientX);
        setLocal(v);
        onChange(v);
      }}
      onPointerMove={(e) => {
        if (!draggingRef.current) return;
        const v = fromPointer(e.clientX);
        setLocal(v);
        onChange(v);
      }}
      onPointerUp={(e) => {
        finishDrag(e.clientX);
        try {
          e.currentTarget.releasePointerCapture(e.pointerId);
        } catch {
          /* already released */
        }
      }}
      onPointerCancel={(e) => {
        finishDrag(e.clientX);
        try {
          e.currentTarget.releasePointerCapture(e.pointerId);
        } catch {
          /* already released */
        }
      }}
      className={`relative h-5 flex items-center select-none ${
        disabled ? "opacity-50 cursor-not-allowed" : "cursor-pointer"
      }`}
      style={{ touchAction: "none" }}
    >
      <div className="absolute inset-x-0 h-2 rounded-full bg-zinc-700 pointer-events-none">
        <div
          className="absolute left-0 inset-y-0 rounded-full"
          style={{ width: `${pct}%`, backgroundColor: fillColor }}
        />
      </div>
      <div
        className="absolute w-4 h-4 rounded-full bg-white shadow-md
                   border-2 border-zinc-900 z-10 -translate-x-1/2
                   hover:scale-110 transition-transform pointer-events-none"
        style={{ left: `${pct}%` }}
      />
    </div>
  );
}
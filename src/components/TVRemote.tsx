/**
 * TVRemote — permanent docked remote bar at the bottom of the screen.
 *
 * Single row layout:
 *
 *  ┌─────────────────────────────────────────────────────────────────────────┐
 *  │ [🖼] Title          0:23 ──────●──────────────── 1:54  ⟳   ⏸  ▶  ⏹  🔊 │
 *  │      ● Playing                                                          │
 *  └─────────────────────────────────────────────────────────────────────────┘
 *
 * The seek bar is a fully custom div-based component — no <input type="range">
 * controlled-component conflicts. Works reliably in Tauri's WKWebView.
 */
import { useState, useEffect, useCallback, useRef, memo } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { PrimeTitle } from "../types";
import { isTvUnreachableMessage, repairTvConnection } from "../playback";

// ─── Types ────────────────────────────────────────────────────────────────────
interface VolumeState { volume: number | null; muted: boolean; }
export type PlaybackState = "playing" | "paused";

interface TVRemoteProps {
  nowPlaying: PrimeTitle | null;
  episode?: number | null;
  playbackState: PlaybackState;
  cachedImageSrc?: string;
  /** Configured default TV volume (0–100), used before the TV reports its level. */
  defaultTvVolume?: number;
  onPlaybackStateChange: (s: PlaybackState) => void;
  onDismissPlaying: () => void;
}

// ─── Transport controls (stable module-level components — must NOT be defined
//     inside TVRemote or they remount every position tick and lose clicks/hover) ─

type TransportAction = "pause" | "play" | "stop";

const TransportButton = memo(function TransportButton({
  action,
  title,
  active,
  busy,
  anyBusy,
  onAction,
  children,
}: {
  action: TransportAction;
  title: string;
  active?: boolean;
  busy: boolean;
  anyBusy: boolean;
  onAction: (action: TransportAction) => void;
  children: React.ReactNode;
}) {
  const disabled = anyBusy;

  return (
    <button
      type="button"
      disabled={disabled}
      title={title}
      onPointerDown={(e) => {
        if (e.button !== 0 || disabled) return;
        e.preventDefault();
        e.stopPropagation();
        onAction(action);
      }}
      className={`w-9 h-9 flex items-center justify-center rounded-xl border shrink-0
                  disabled:opacity-40 ${active
        ? "bg-emerald-600 border-emerald-500 text-white"
        : action === "stop"
          ? "bg-zinc-800/80 border-zinc-700/60 text-zinc-300 hover:bg-red-900/60 hover:border-red-700/60 hover:text-red-300"
          : "bg-zinc-800/80 border-zinc-700/60 text-zinc-300 hover:bg-zinc-700 hover:text-white"
      }`}
    >
      {busy
        ? <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24"><circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/><path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>
        : children}
    </button>
  );
});

const TransportBar = memo(function TransportBar({
  tvOn,
  playbackState,
  pbBusy,
  transportErr,
  onTransport,
}: {
  tvOn: boolean | null;
  playbackState: PlaybackState;
  pbBusy: TransportAction | null;
  transportErr: string | null;
  onTransport: (action: TransportAction) => void;
}) {
  const anyBusy = pbBusy !== null;

  return (
    <div className={`flex flex-col items-center gap-0.5 shrink-0 ${
      tvOn === false ? "opacity-40" : ""
    }`}>
      <div className="flex items-center gap-1.5">
        <TransportButton
          action="pause"
          title="Pause"
          active={playbackState === "paused"}
          busy={pbBusy === "pause"}
          anyBusy={anyBusy}
          onAction={onTransport}
        >
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
            <rect x="6" y="4" width="4" height="16" rx="1"/>
            <rect x="14" y="4" width="4" height="16" rx="1"/>
          </svg>
        </TransportButton>
        <TransportButton
          action="play"
          title="Play / Resume"
          active={playbackState === "playing"}
          busy={pbBusy === "play"}
          anyBusy={anyBusy}
          onAction={onTransport}
        >
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
            <path d="M6.3 2.841A1.5 1.5 0 004 4.11V15.89a1.5 1.5 0 002.3 1.269l9.344-5.89a1.5 1.5 0 000-2.538L6.3 2.84z"/>
          </svg>
        </TransportButton>
        <TransportButton
          action="stop"
          title="Stop"
          busy={pbBusy === "stop"}
          anyBusy={anyBusy}
          onAction={onTransport}
        >
          <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
            <rect x="5" y="5" width="14" height="14" rx="2"/>
          </svg>
        </TransportButton>
      </div>
      {transportErr && (
        <span className="text-[9px] text-red-400 max-w-[120px] text-center leading-tight truncate"
              title={transportErr}>
          {transportErr}
        </span>
      )}
    </div>
  );
});

// Compact "Fix" button shown next to the dock's "TV unreachable" label.
const QuickFixButton = memo(function QuickFixButton({
  fixing,
  onClick,
}: {
  fixing: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={fixing}
      onClick={onClick}
      title="Re-detect the TV and wake it (Wake-on-LAN)"
      className="ml-auto shrink-0 flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-semibold
                 bg-sky-600 hover:bg-sky-500 disabled:opacity-50 text-white transition-colors"
    >
      {fixing ? (
        <svg className="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
          <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
          <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
        </svg>
      ) : null}
      Fix
    </button>
  );
});

// ─── Utilities ────────────────────────────────────────────────────────────────
function formatTime(secs: number): string {
  const s = Math.max(0, Math.floor(secs));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}

// ─── Custom horizontal seek bar ───────────────────────────────────────────────
// Uses pointer events on a plain div — completely avoids the React
// controlled-component snap-back problem of <input type="range">.
function HorizontalSeekBar({
  value, max, knownDuration,
  onChange, onCommit,
}: {
  value: number;         // current playback position in seconds
  max: number;           // total seconds (or large default)
  knownDuration: boolean;// whether max is real runtime or just a fallback
  onChange: (v: number) => void;   // called during drag (preview)
  onCommit: (v: number) => void;   // called on release (actual seek)
}) {
  const trackRef    = useRef<HTMLDivElement>(null);
  const draggingRef = useRef(false);
  // localValue is the source of truth for the visual; syncs from parent when idle
  const [local, setLocal] = useState(value);

  useEffect(() => {
    if (!draggingRef.current) setLocal(value);
  }, [value]);

  const fromPointer = (clientX: number): number => {
    if (!trackRef.current) return local;
    const rect = trackRef.current.getBoundingClientRect();
    const pct  = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return Math.round(pct * max);
  };

  const pct        = max > 0 ? Math.min(100, (local / max) * 100) : 0;
  const fillColor  = knownDuration ? "#10b981" : "#4b5563"; // green or grey

  // Drag via document-level listeners so the thumb keeps following the cursor
  // even when it leaves the thin track (and works reliably inside the Tauri
  // WebView, where setPointerCapture on a div is unreliable).
  const startDrag = (clientX: number) => {
    draggingRef.current = true;
    const v = fromPointer(clientX);
    setLocal(v); onChange(v);

    const move = (ev: PointerEvent) => {
      if (!draggingRef.current) return;
      const nv = fromPointer(ev.clientX);
      setLocal(nv); onChange(nv);
    };
    const up = (ev: PointerEvent) => {
      if (!draggingRef.current) return;
      draggingRef.current = false;
      const nv = fromPointer(ev.clientX);
      setLocal(nv); onCommit(nv);
      document.removeEventListener("pointermove", move);
      document.removeEventListener("pointerup", up);
      document.removeEventListener("pointercancel", up);
    };
    document.addEventListener("pointermove", move);
    document.addEventListener("pointerup", up);
    document.addEventListener("pointercancel", up);
  };

  return (
    <div
      ref={trackRef}
      onPointerDown={e => {
        e.preventDefault();
        startDrag(e.clientX);
      }}
      className="relative h-5 flex items-center cursor-pointer select-none"
      style={{ touchAction: "none" }}
    >
      {/* Track */}
      <div className="absolute inset-x-0 h-2 rounded-full bg-zinc-700">
        {/* Fill */}
        <div
          className="absolute left-0 inset-y-0 rounded-full"
          style={{ width: `${pct}%`, backgroundColor: fillColor }}
        />
      </div>
      {/* Thumb — visible and draggable */}
      <div
        className="absolute w-4 h-4 rounded-full bg-white shadow-md
                   border-2 border-zinc-900 z-10 -translate-x-1/2
                   hover:scale-110 transition-transform"
        style={{ left: `${pct}%` }}
      />
    </div>
  );
}

// ─── Speaker icon ─────────────────────────────────────────────────────────────
function SpeakerIcon({ level, muted, size = 16 }: { level: number | null; muted: boolean; size?: number }) {
  if (muted || level === 0)
    return (
      <svg width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M17.25 9.75L19.5 12m0 0l2.25 2.25M19.5 12l2.25-2.25M19.5 12l-2.25 2.25m-10.5-6l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z" />
      </svg>
    );
  if (level !== null && level < 40)
    return (
      <svg width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" d="M19.114 5.636a9 9 0 010 12.728M11.25 4.5l-4.72 4.72a.75.75 0 01-.53.22H4.51c-.88 0-1.704.507-1.938 1.354A9.01 9.01 0 002.25 12c0 .83.112 1.633.322 2.396.234.847 1.057 1.354 1.938 1.354h1.49a.75.75 0 01.53.22l4.72 4.72a.75.75 0 001.28-.53V5.03a.75.75 0 00-1.28-.53z" />
      </svg>
    );
  return (
    <svg width={size} height={size} fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M19.114 5.636a9 9 0 010 12.728M15.75 5.25S18 7.5 18 12s-2.25 6.75-2.25 6.75M11.25 4.5l-4.72 4.72a.75.75 0 01-.53.22H4.51c-.88 0-1.704.507-1.938 1.354A9.01 9.01 0 002.25 12c0 .83.112 1.633.322 2.396.234.847 1.057 1.354 1.938 1.354h1.49a.75.75 0 01.53.22l4.72 4.72a.75.75 0 001.28-.53V5.03a.75.75 0 00-1.28-.53z" />
    </svg>
  );
}

// ─── Custom vertical volume slider ───────────────────────────────────────────
const SLIDER_H = 104;
const THUMB_SZ = 16;

function VerticalSlider({ value, muted, onChange }: {
  value: number; muted: boolean; onChange: (v: number) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  const fromPointer = useCallback((clientY: number): number => {
    if (!trackRef.current) return value;
    const rect = trackRef.current.getBoundingClientRect();
    return Math.round(Math.max(0, Math.min(1, 1 - (clientY - rect.top) / rect.height)) * 100);
  }, [value]);

  const pct       = Math.max(0, Math.min(100, value));
  const fillColor = muted ? "#374151" : pct > 75 ? "#f97316" : "#10b981";

  return (
    <div
      ref={trackRef}
      onPointerDown={e => { e.preventDefault(); e.currentTarget.setPointerCapture(e.pointerId); dragging.current = true; onChange(fromPointer(e.clientY)); }}
      onPointerMove={e => { if (!dragging.current) return; onChange(fromPointer(e.clientY)); }}
      onPointerUp={() => { dragging.current = false; }}
      onPointerLeave={() => { dragging.current = false; }}
      className="relative select-none cursor-pointer rounded-full bg-zinc-700/80"
      style={{ width: "8px", height: `${SLIDER_H}px`, touchAction: "none" }}
    >
      <div className="absolute bottom-0 left-0 right-0 rounded-full"
           style={{ height: `${pct}%`, backgroundColor: fillColor }} />
      <div className="absolute left-1/2 rounded-full shadow-lg border-2 border-zinc-900"
           style={{ width: `${THUMB_SZ}px`, height: `${THUMB_SZ}px`,
                    bottom: `${(pct / 100) * (SLIDER_H - THUMB_SZ)}px`,
                    transform: "translateX(-50%)", backgroundColor: fillColor, cursor: "grab" }} />
    </div>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────
export default function TVRemote({
  nowPlaying, episode, playbackState, cachedImageSrc,
  defaultTvVolume = 13,
  onPlaybackStateChange, onDismissPlaying,
}: TVRemoteProps) {

  // ── TV power + volume ─────────────────────────────────────────────────────
  const [tvOn, setTvOn]       = useState<boolean | null>(null);
  const tvOnRef               = useRef<boolean | null>(null);
  const [vol, setVol]         = useState<VolumeState>({ volume: null, muted: false });
  const [slider, setSlider]   = useState(defaultTvVolume);
  const [volError, setVE]     = useState(false);
  const volDebounce           = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setTvOnState = useCallback((on: boolean | null) => {
    tvOnRef.current = on;
    setTvOn(on);
  }, []);

  const refreshTvPower = useCallback(async () => {
    try {
      const ps = await invoke<{ on: boolean }>("get_tv_power");
      setTvOnState(ps.on);
      return ps.on;
    } catch {
      setTvOnState(false);
      return false;
    }
  }, [setTvOnState]);

  const fetchVolume = useCallback(async () => {
    if (tvOnRef.current === false) return;
    setVE(false);
    try {
      const s = await invoke<VolumeState>("get_tv_volume");
      setVol(s); setSlider(s.volume ?? defaultTvVolume);
      setVE(false);
    } catch {
      if (tvOnRef.current === true) setVE(true);
      else setVE(false);
    }
  }, [defaultTvVolume]);

  useEffect(() => {
    refreshTvPower().then((on) => { if (on) fetchVolume(); });
  }, [refreshTvPower, fetchVolume]);

  // Keep the power indicator in sync with what playback actually observes:
  // a play attempt that can't reach the TV flips us to "off/unreachable", and a
  // successful launch confirms the TV is back. Avoids a stale green icon when
  // the TV drops mid-session (it's only polled once at startup).
  useEffect(() => {
    let unlisten: (() => void) | null = null;
    let active = true;
    listen<string>("play-progress", (event) => {
      const line = event.payload ?? "";
      if (isTvUnreachableMessage(line)) {
        setTvOnState(false);
      } else if (/(^|\n)\s*done\.?\s*$/i.test(line)) {
        setTvOnState(true);
      }
    }).then((fn) => {
      if (active) unlisten = fn;
      else fn();
    });
    return () => {
      active = false;
      unlisten?.();
    };
  }, [setTvOnState]);

  // Quick, non-disruptive connection fix from the dock: re-discover the TV's IP
  // via mDNS and send Wake-on-LAN. (The disruptive Wi-Fi reset lives in the
  // play dialog's fuller repair UI.)
  const [fixingTv, setFixingTv] = useState(false);
  const handleQuickFix = useCallback(async () => {
    setFixingTv(true);
    try {
      const r = await repairTvConnection(true);
      setTvOnState(r.reachable);
      if (r.reachable) fetchVolume();
    } catch {
      /* leave indicator as-is */
    } finally {
      setFixingTv(false);
    }
  }, [setTvOnState, fetchVolume]);

  const handleVolSlider = (v: number) => {
    if (!tvOnRef.current) return;
    setSlider(v);
    setVol(p => ({ ...p, volume: v, muted: false }));
    if (volDebounce.current) clearTimeout(volDebounce.current);
    volDebounce.current = setTimeout(async () => {
      try {
        const s = await invoke<VolumeState>("set_tv_volume", { level: v });
        setVol(s); setSlider(s.volume ?? v);
        setVE(false);
      } catch { setVE(true); }
    }, 180);
  };

  const handleMute = async () => {
    if (!tvOnRef.current) return;
    const m = !vol.muted;
    setVol(p => ({ ...p, muted: m }));
    try {
      const s = await invoke<VolumeState>("set_tv_mute", { muted: m });
      setVol(s); setSlider(s.volume ?? slider);
      setVE(false);
    } catch {
      setVol(p => ({ ...p, muted: !m }));
      setVE(true);
    }
  };

  // ── Position tracking ───────────────────────────────────────────────────
  // playRef holds the "anchor point": position value + wall-clock time when
  // that value was recorded. The interval adds elapsed time to get current pos.
  //
  // isDraggingRef: lets the interval check drag state WITHOUT being a dep
  // (adding isDragging to tick deps would destroy/recreate the interval on
  //  every drag event, causing the bar to freeze).
  //
  const [pos, setPos]                       = useState(0);
  const playRef                             = useRef<{ pos: number; time: number }>({ pos: 0, time: Date.now() });
  const isDraggingRef                       = useRef(false);
  const [seekPreview, setSeekPreview]       = useState<number | null>(null); // shown during drag

  // Time text-input. `timeMode` decides what committing the value does:
  //   "seek"   → re-launch playback at that position on the TV
  //   "anchor" → only correct the displayed position (no re-launch / no TV call)
  const [editingTime, setEditingTime]   = useState(false);
  const [timeMode, setTimeMode]         = useState<"seek" | "anchor">("seek");
  const [timeInput, setTimeInput]       = useState("");

  const totalSecs      = nowPlaying?.runtime_min ? nowPlaying.runtime_min * 60 : null;
  const sliderMax      = totalSecs ?? 4 * 3600;
  const displayPos     = seekPreview ?? pos;   // show preview during drag
  const knownDuration  = totalSecs !== null;

  // Reset when a new title starts
  useEffect(() => {
    playRef.current = { pos: 0, time: Date.now() };
    setPos(0); setSeekPreview(null);
    isDraggingRef.current = false;
  }, [nowPlaying?.content_id]); // eslint-disable-line

  // NOTE: We intentionally do NOT auto-poll the TV for playback position.
  // Querying position (get_playback_position) opens an SSAP session and issues
  // media getInfo requests, which on webOS interrupts/pauses Prime playback
  // (including ads). Position therefore tracks via the wall-clock simulation
  // below; the user can press "Sync from TV" to pull the real position on demand.

  // Freeze / resume the anchor when pausing / resuming
  useEffect(() => {
    if (playbackState === "paused") {
      const elapsed = (Date.now() - playRef.current.time) / 1000;
      playRef.current = { pos: playRef.current.pos + elapsed, time: Date.now() };
      setPos(playRef.current.pos);
    } else {
      playRef.current = { ...playRef.current, time: Date.now() };
    }
  }, [playbackState]);

  // Tick every 250 ms — isDragging checked via ref, NOT in dep array
  useEffect(() => {
    if (!nowPlaying || playbackState !== "playing") return;
    const id = setInterval(() => {
      if (isDraggingRef.current) return;
      const elapsed = (Date.now() - playRef.current.time) / 1000;
      const next    = playRef.current.pos + elapsed;
      setPos(totalSecs ? Math.min(next, totalSecs) : next);
    }, 250);
    return () => clearInterval(id);
  }, [nowPlaying, playbackState, totalSecs]); // isDragging intentionally absent

  // ── Seek ─────────────────────────────────────────────────────────────────
  const commitSeek = useCallback((seconds: number) => {
    playRef.current = { pos: seconds, time: Date.now() };
    setPos(seconds); setSeekPreview(null);
    invoke("seek_to", {
      seconds,
      contentId: nowPlaying?.content_id ?? null,
      episode: episode ?? null,
    }).catch(() => {});
  }, [nowPlaying?.content_id, episode]); // eslint-disable-line

  const parseTimeStr = (s: string): number | null => {
    const p = s.trim().split(":").map(x => parseFloat(x));
    if (p.some(isNaN)) return null;
    if (p.length === 1) return p[0];
    if (p.length === 2) return p[0] * 60 + p[1];
    if (p.length === 3) return p[0] * 3600 + p[1] * 60 + p[2];
    return null;
  };

  const syncFromTV = async () => {
    try {
      const r = await invoke<{ position: number | null }>("get_playback_position");
      if (r.position != null) { playRef.current = { pos: r.position, time: Date.now() }; setPos(r.position); }
    } catch { /* not supported */ }
  };

  // Re-anchor the displayed position to a value the user typed (e.g. the time
  // Prime is actually showing). Purely local — does NOT touch the TV, so it
  // never pauses or re-launches the stream. The bar then ticks on from here.
  const setDisplayedPosition = (seconds: number) => {
    playRef.current = { pos: seconds, time: Date.now() };
    setPos(seconds);
    setSeekPreview(null);
  };

  // Commit the value in the time input according to the active mode.
  const commitTimeInput = () => {
    const s = parseTimeStr(timeInput);
    if (s !== null) {
      const clamped = Math.max(0, totalSecs ? Math.min(s, totalSecs) : s);
      if (timeMode === "anchor") setDisplayedPosition(clamped);
      else commitSeek(clamped);
    }
    setEditingTime(false); setTimeInput("");
  };

  const openTimeEditor = (mode: "seek" | "anchor") => {
    setTimeMode(mode);
    setTimeInput(formatTime(displayPos));
    setEditingTime(true);
  };

  // ── Power toggle ──────────────────────────────────────────────────────────
  const [powerBusy, setPowerBusy] = useState(false);
  const [powerErr, setPowerErr] = useState<string | null>(null);

  const togglePower = async () => {
    const turningOff = tvOn === true;
    setPowerBusy(true);
    setPowerErr(null);
    try {
      const applied = await invoke<VolumeState | null>("tv_power", { action: turningOff ? "off" : "on" });
      if (turningOff) {
        setTvOnState(false);
        setVE(false);
        onDismissPlaying();
      } else {
        setTvOnState(true);
        // Prefer the volume the backend just applied (avoids a separate read that
        // races a just-woken TV and can latch a stale level). Fall back to a live
        // fetch when the default-volume feature is off.
        if (applied && applied.volume != null) {
          setVol(applied);
          setSlider(applied.volume);
          setVE(false);
        } else {
          await fetchVolume();
        }
      }
    } catch (err) {
      setPowerErr(String(err).replace(/^Error:\s*/, "").slice(0, 60));
      await refreshTvPower();
    } finally {
      setPowerBusy(false);
    }
  };

  // ── Transport ─────────────────────────────────────────────────────────────
  const [pbBusy, setPbBusy] = useState<TransportAction | null>(null);
  const [transportErr, setTransportErr] = useState<string | null>(null);

  const handleTransport = useCallback(async (action: TransportAction) => {
    // Pause acts as a toggle: if already paused, resume instead.
    const effective = action === "pause" && playbackState === "paused" ? "play" : action;
    setPbBusy(effective);
    setTransportErr(null);
    try {
      await invoke("media_control", { action: effective });
      // Command succeeded — TV is clearly reachable; reset any stale unreachable state.
      setTvOnState(true);
      if (effective === "pause") onPlaybackStateChange("paused");
      else if (effective === "play") onPlaybackStateChange("playing");
      else { onPlaybackStateChange("paused"); onDismissPlaying(); }
    } catch (err) {
      const msg = String(err).replace(/^Error:\s*/, "");
      if (isTvUnreachableMessage(msg)) {
        setTvOnState(false);
        setTransportErr("TV unreachable");
      } else {
        setTransportErr(msg.slice(0, 60));
      }
    } finally {
      setPbBusy(null);
    }
  }, [playbackState, onPlaybackStateChange, onDismissPlaying, setTvOnState]);

  const volPct    = vol.muted ? 0 : slider;
  const dispVol   = vol.muted ? 0 : (vol.volume ?? slider);

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="fixed bottom-0 inset-x-0 z-50 overflow-visible
                    bg-[#0d1920]/97 backdrop-blur-md
                    border-t border-zinc-700/50
                    shadow-[0_-4px_24px_rgba(0,0,0,0.5)]">
      <div className="flex items-center gap-3 h-[72px] px-4 max-w-screen-2xl mx-auto">

        {/* ── LEFT: Now Playing ─────────────────────────────────────────── */}
        <div className="flex items-center gap-3 shrink-0 w-64">
          {/* Thumbnail — 16:9, prominent */}
          <div className="shrink-0 w-20 h-[45px] rounded-lg overflow-hidden
                          bg-zinc-800 border border-zinc-700/60 shadow-md">
            {nowPlaying && (cachedImageSrc || nowPlaying.image_url) ? (
              <img
                src={cachedImageSrc || nowPlaying.image_url!.replace(/\._UR\d+,\d+_\./, "._UR320,180_.")}
                alt={nowPlaying.title}
                className="w-full h-full object-cover"
              />
            ) : (
              <div className="w-full h-full flex items-center justify-center">
                <svg className="w-5 h-5 text-zinc-600" fill="currentColor" viewBox="0 0 20 20">
                  <path d="M6.3 2.841A1.5 1.5 0 004 4.11V15.89a1.5 1.5 0 002.3 1.269l9.344-5.89a1.5 1.5 0 000-2.538L6.3 2.84z"/>
                </svg>
              </div>
            )}
          </div>

          {/* Title + meta */}
          <div className="flex-1 min-w-0">
            {nowPlaying ? (
              <>
                <p className="text-white text-sm font-semibold truncate leading-snug">
                  {nowPlaying.title}
                </p>
                <div className="flex items-center gap-1.5 mt-0.5">
                  <span className={`w-2 h-2 rounded-full shrink-0 ${
                    tvOn === false
                      ? "bg-red-500"
                      : playbackState === "playing"
                        ? "bg-emerald-400 animate-pulse"
                        : "bg-zinc-500"
                  }`} />
                  <span className={`text-xs truncate ${tvOn === false ? "text-red-400 font-medium" : "text-zinc-400"}`}>
                    {tvOn === false
                      ? "TV unreachable"
                      : playbackState === "playing" ? "Playing on TV" : "Paused"}
                    {tvOn !== false && episode != null ? ` · Episode ${episode}` : ""}
                    {tvOn !== false && episode == null && nowPlaying.year ? ` · ${nowPlaying.year}` : ""}
                  </span>
                  {tvOn === false && <QuickFixButton fixing={fixingTv} onClick={handleQuickFix} />}
                </div>
              </>
            ) : tvOn === false ? (
              <div className="flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full shrink-0 bg-red-500" />
                <div className="flex flex-col gap-0.5 min-w-0">
                  <p className="text-sm text-red-400 font-semibold leading-tight">TV unreachable</p>
                  <p className="text-xs text-red-400/70">Power it on, or try a quick fix</p>
                </div>
                <QuickFixButton fixing={fixingTv} onClick={handleQuickFix} />
              </div>
            ) : (
              <div className="flex flex-col gap-0.5">
                <p className="text-sm text-zinc-500 font-medium">Nothing playing</p>
                <p className="text-xs text-zinc-700">Select a title to play</p>
              </div>
            )}
          </div>

          {/* Dismiss — only when something is playing */}
          {nowPlaying && (
            <button onClick={onDismissPlaying} title="Dismiss"
              className="shrink-0 p-1.5 text-zinc-600 hover:text-zinc-400
                         hover:bg-zinc-800 rounded-lg transition-colors">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12"/>
              </svg>
            </button>
          )}
        </div>

        <div className="w-px h-9 bg-zinc-700/60 shrink-0" />

        {/* ── CENTRE: Position / seek ───────────────────────────────────── */}
        <div className="flex-1 flex items-center gap-2 min-w-0">

          {/* Elapsed time — click to type a target time (re-launches there) */}
          {editingTime ? (
            <input
              type="text"
              value={timeInput}
              onChange={e => setTimeInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === "Enter") commitTimeInput();
                if (e.key === "Escape") { setEditingTime(false); setTimeInput(""); }
              }}
              onBlur={commitTimeInput}
              placeholder={formatTime(displayPos)}
              title={timeMode === "anchor"
                ? "Set the displayed time to match the TV (no re-launch)"
                : "Jump to this time on the TV (re-launches playback)"}
              className={`shrink-0 w-16 text-xs font-mono text-center bg-zinc-800
                         border rounded px-1 py-0.5 text-white outline-none ${
                timeMode === "anchor" ? "border-sky-500" : "border-emerald-500"}`}
              autoFocus
            />
          ) : (
            <button
              onClick={() => openTimeEditor("seek")}
              title="Click to jump to a specific time on the TV  e.g. 1:23:45"
              className="shrink-0 w-14 text-right text-xs font-mono tabular-nums
                         text-zinc-300 hover:text-emerald-400 hover:underline
                         transition-colors cursor-pointer"
            >
              {formatTime(displayPos)}
            </button>
          )}

          {/* Seek bar */}
          <div className="flex-1 min-w-0">
            <HorizontalSeekBar
              value={displayPos}
              max={sliderMax}
              knownDuration={knownDuration}
              onChange={v => { isDraggingRef.current = true; setSeekPreview(v); }}
              onCommit={v => { isDraggingRef.current = false; commitSeek(v); }}
            />
          </div>

          {/* Total duration */}
          <span className="shrink-0 w-14 text-xs font-mono tabular-nums text-zinc-600">
            {knownDuration ? formatTime(totalSecs!) : "?:??"}
          </span>

          {/* Set displayed position (no re-launch) — fixes the readout when
              Prime auto-resumed at a different point than the bar shows */}
          <button onClick={() => openTimeEditor("anchor")}
            title="Set the displayed time to match the TV (no re-launch)"
            className="shrink-0 p-1 text-zinc-700 hover:text-sky-400 hover:bg-zinc-800/60 rounded-lg transition-colors">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.2} viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="8.25" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 7.5V12l2.75 1.75" />
            </svg>
          </button>

          {/* Sync from TV */}
          <button onClick={syncFromTV} title="Sync position from TV (briefly pauses the stream)"
            className="shrink-0 p-1 text-zinc-700 hover:text-zinc-400 hover:bg-zinc-800/60 rounded-lg transition-colors">
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99"/>
            </svg>
          </button>
        </div>

        <div className="w-px h-9 bg-zinc-700/60 shrink-0" />

        {/* ── TRANSPORT ─────────────────────────────────────────────────── */}
        <TransportBar
          tvOn={tvOn}
          playbackState={playbackState}
          pbBusy={pbBusy}
          transportErr={transportErr}
          onTransport={handleTransport}
        />

        <div className="w-px h-9 bg-zinc-700/60 shrink-0" />

        {/* ── POWER (single toggle: green=on, red=off) ──────────────────── */}
        <div className="flex flex-col items-center shrink-0">
          <button
            onClick={togglePower}
            disabled={powerBusy || tvOn === null}
            title={
              powerErr ?? (
                tvOn === true ? "TV is on — click to power off"
                : tvOn === false ? "TV is off — click to power on"
                : "Checking TV…"
              )
            }
            className={`w-9 h-9 flex items-center justify-center rounded-xl border transition-colors
                        disabled:opacity-40 shrink-0 ${
              tvOn === true
                ? "bg-emerald-900/50 border-emerald-600 text-emerald-300 hover:bg-emerald-800/60"
                : tvOn === false
                  ? "bg-red-900/40 border-red-700/70 text-red-300 hover:bg-red-900/60"
                  : "bg-zinc-800/80 border-zinc-700/60 text-zinc-400"
            }`}
          >
            {powerBusy ? (
              <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
              </svg>
            ) : (
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5.636 5.636a9 9 0 1012.728 0M12 3v9"/>
              </svg>
            )}
          </button>
        </div>

        <div className="w-px h-9 bg-zinc-700/60 shrink-0" />

        {/* ── VOLUME (only active when TV is on) ─────────────────────────── */}
        <div className={`relative flex flex-col items-center justify-center shrink-0 w-10 h-full ${
          tvOn === false ? "opacity-40 pointer-events-none" : ""
        }`}>

          {/* Vertical slider pops above dock */}
          <div className="absolute bottom-full mb-1 flex flex-col items-center gap-1 pb-1.5
                          bg-zinc-900/90 border border-zinc-700/60 rounded-xl px-2 pt-2"
               style={{ width: "36px" }}>
            <VerticalSlider value={volPct} muted={vol.muted} onChange={handleVolSlider} />
            <span className={`text-[10px] font-bold tabular-nums leading-none mt-0.5 ${
              vol.muted ? "text-zinc-600" : volPct > 75 ? "text-orange-400" : "text-emerald-400"
            }`}>
              {volError ? "!" : vol.muted ? "—" : slider}
            </span>
          </div>

          {/* Mute button — click also retries volume sync when TV is on */}
          <button
            onClick={() => { if (volError) fetchVolume(); else handleMute(); }}
            title={
              tvOn === false ? "TV is off"
              : volError ? "Retry volume sync"
              : vol.muted ? "Unmute" : "Mute"
            }
            className={`p-1.5 rounded-lg transition-colors ${
              vol.muted
                ? "text-red-400 bg-red-900/40 hover:bg-red-900/60"
                : volError
                  ? "text-orange-400 bg-orange-900/30 hover:bg-orange-900/50"
                  : "text-zinc-400 hover:text-white hover:bg-zinc-700/60"
            }`}>
            <SpeakerIcon level={dispVol} muted={vol.muted} size={18} />
          </button>
        </div>

      </div>
    </div>
  );
}

import { useState, useEffect, useRef } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { isBookmarked } from "../bookmarks";
import { PrimeTitle, PrimeEpisode, getAccessLabel, accessBadgeStyle, AppConfig } from "../types";
import { ContextMenuItem } from "./ContextMenu";
import { showMediaContextMenu } from "../contextMenuBus";
import { BookmarkIcon, ExternalLinkIcon, LaptopIcon, PlayIcon, TrailerIcon } from "./BookmarkMenuIcons";
import { playOnMac, playOnTv } from "../playback";
import { openTmdbLookup, openTmdbTrailerForTitle, tmdbUrlForEpisode, tmdbUrlForTitle } from "../tmdb";

interface PlayDialogProps {
  item: PrimeTitle;
  config: AppConfig;
  initialEpisode?: number | null;
  /** When set, launch this Prime detail ID instead of item.content_id + episode. */
  launchContentId?: string | null;
  bookmarkedIds?: Set<string>;
  onToggleBookmark?: (
    item: PrimeTitle,
    options?: {
      episode?: PrimeEpisode | null;
      episodeIndex?: number;
      sourceItem?: PrimeTitle;
      playEpisode?: number;
    }
  ) => void;
  onClose: () => void;
  onOpenSettings: () => void;
  onStartPlaying: (item: PrimeTitle, episode: number | null) => void;
  onPlayed: (item: PrimeTitle) => void;
}

type PlayState = "idle" | "playing" | "done" | "error";
type MacPlayState = "idle" | "opening" | "done" | "error";

/** Format whole minutes as "48 min" or "1 h 23 min". */
function formatRuntimeMin(min: number): string {
  const h = Math.floor(min / 60);
  const m = min % 60;
  return h > 0 ? `${h} h ${m} min` : `${m} min`;
}

/** Parse "ss", "mm:ss" or "h:mm:ss" into seconds. Returns null when invalid. */
function parseTimeToSeconds(s: string): number | null {
  const t = s.trim();
  if (!t) return null;
  const parts = t.split(":").map((x) => Number(x));
  if (parts.some((n) => Number.isNaN(n) || n < 0)) return null;
  if (parts.length === 1) return parts[0];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return null;
}

export default function PlayDialog({
  item,
  config,
  initialEpisode = null,
  launchContentId = null,
  bookmarkedIds,
  onToggleBookmark,
  onClose,
  onOpenSettings,
  onStartPlaying,
  onPlayed,
}: PlayDialogProps) {
  // Profile can be quickly overridden per play; IP always comes from settings.
  const [profile, setProfile] = useState(config.profile);
  const [playState, setPlayState] = useState<PlayState>("idle");
  const [macPlayState, setMacPlayState] = useState<MacPlayState>("idle");
  const [macError, setMacError] = useState<string | null>(null);
  const [log, setLog] = useState<string[]>([]);
  const preferMac = config.default_playback_target === "mac";
  const logRef = useRef<HTMLDivElement>(null);
  const unlistenRef = useRef<(() => void) | null>(null);

  // TV series episode selection (movies ignore this)
  const isSeries = item.entity_type === "TV Show";
  const [episode, setEpisode] = useState(initialEpisode ?? 1);

  // Optional start position ("mm:ss" / "h:mm:ss") to begin playback from.
  const [startAt, setStartAt] = useState("");


  // Full episode list (fetched for TV shows). Falls back to the numeric
  // stepper when the list can't be loaded.
  type EpListState = "idle" | "loading" | "done" | "error";
  const [episodes, setEpisodes] = useState<PrimeEpisode[]>([]);
  const [epListState, setEpListState] = useState<EpListState>("idle");

  useEffect(() => {
    if (!isSeries) return;
    let active = true;
    setEpListState("loading");
    setEpisodes([]);
    invoke<string>("list_episodes", { contentId: item.content_id })
      .then((raw) => {
        if (!active) return;
        let list: PrimeEpisode[] = [];
        try {
          list = JSON.parse(raw);
        } catch {
          list = [];
        }
        setEpisodes(Array.isArray(list) ? list : []);
        setEpListState(list.length > 0 ? "done" : "error");
      })
      .catch(() => {
        if (!active) return;
        setEpisodes([]);
        setEpListState("error");
      });
    return () => {
      active = false;
    };
  }, [isSeries, item.content_id]);

  useEffect(() => {
    if (initialEpisode != null) setEpisode(initialEpisode);
  }, [initialEpisode, item.content_id]);

  const openMenu = (e: React.MouseEvent, items: ContextMenuItem[]) => {
    e.preventDefault();
    e.stopPropagation();
    window.getSelection()?.removeAllRanges();
    if (items.length > 0) {
      showMediaContextMenu({ x: e.clientX, y: e.clientY, items });
    }
  };

  const titleBookmarked = bookmarkedIds ? isBookmarked(bookmarkedIds, item) : false;

  const titleMenuItems = (): ContextMenuItem[] => {
    const items: ContextMenuItem[] = [
      {
        label: "Play on TV",
        icon: <PlayIcon />,
        onClick: () => void handlePlayAt(episode),
      },
      {
        label: "Play on Mac",
        icon: <LaptopIcon />,
        onClick: () => void handlePlayOnMacAt(episode),
      },
      {
        label: "Look up on TMDB",
        icon: <ExternalLinkIcon />,
        onClick: () => void openTmdbLookup(tmdbUrlForTitle(item)),
      },
      {
        label: "Show trailer on TMDB",
        icon: <TrailerIcon />,
        onClick: () => void openTmdbTrailerForTitle(item),
      },
    ];
    if (onToggleBookmark) {
      items.push({
        label: titleBookmarked
          ? "Remove bookmark"
          : isSeries
          ? "Bookmark series"
          : "Bookmark",
        icon: <BookmarkIcon filled={titleBookmarked} />,
        onClick: () => onToggleBookmark(item),
        destructive: titleBookmarked,
      });
    }
    return items;
  };

  const episodeMenuItems = (ep: PrimeEpisode, idx: number): ContextMenuItem[] => {
    const epNum = ep.sequence_number ?? idx + 1;
    const bookmarked = bookmarkedIds ? isBookmarked(bookmarkedIds, item, ep) : false;
    const items: ContextMenuItem[] = [
      {
        label: `Play episode ${epNum} on TV`,
        icon: <PlayIcon />,
        onClick: () => {
          setEpisode(idx + 1);
          void handlePlayAt(idx + 1);
        },
      },
      {
        label: `Play episode ${epNum} on Mac`,
        icon: <LaptopIcon />,
        onClick: () => {
          setEpisode(idx + 1);
          void handlePlayOnMacAt(idx + 1);
        },
      },
    ];
    items.push({
      label: "Look up episode on TMDB",
      icon: <ExternalLinkIcon />,
      onClick: () => void openTmdbLookup(tmdbUrlForEpisode(item, ep, idx)),
    });
    items.push({
      label: "Show trailer on TMDB",
      icon: <TrailerIcon />,
      onClick: () => void openTmdbTrailerForTitle(item),
    });
    if (onToggleBookmark) {
      items.push({
        label: bookmarked ? "Remove bookmark" : "Bookmark episode",
        icon: <BookmarkIcon filled={bookmarked} />,
        onClick: () =>
          onToggleBookmark(item, {
            episode: ep,
            episodeIndex: idx,
            sourceItem: item,
            playEpisode: epNum,
          }),
        destructive: bookmarked,
      });
    }
    return items;
  };

  const hasEpisodeList = isSeries && epListState === "done" && episodes.length > 0;
  const selectedEpisode = hasEpisodeList ? episodes[episode - 1] : null;

  // Total length of the media currently selected (the chosen episode for a
  // series, otherwise the movie itself).
  const selectedRuntimeMin = isSeries
    ? selectedEpisode?.runtime_min ?? null
    : item.runtime_min;
  const selectedRuntimeStr =
    selectedRuntimeMin != null
      ? formatRuntimeMin(selectedRuntimeMin)
      : !isSeries
      ? item.runtime_str
      : null;

  const label = getAccessLabel(item);
  const badgeStyle = accessBadgeStyle(label);

  // Subscribe to play-progress events from Rust
  useEffect(() => {
    let mounted = true;
    listen<string>("play-progress", (event) => {
      if (!mounted) return;
      setLog((prev) => [...prev, event.payload]);
      requestAnimationFrame(() => {
        logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
      });
    }).then((unlisten) => {
      unlistenRef.current = unlisten;
    });
    return () => {
      mounted = false;
      unlistenRef.current?.();
    };
  }, []);

  const handlePlayAt = async (epNum: number) => {
    setLog([]);
    setMacError(null);
    setPlayState("playing");
    const useLaunchId = launchContentId?.trim() || null;
    const ep = useLaunchId ? null : isSeries ? epNum : null;
    const epRow = hasEpisodeList ? episodes[epNum - 1] : null;
    const epRuntimeMin = epRow?.runtime_min ?? null;
    const playedItem = epRuntimeMin ? { ...item, runtime_min: epRuntimeMin } : item;
    onStartPlaying(playedItem, useLaunchId ? epNum : ep);
    try {
      await playOnTv(item, { tv_ip: config.tv_ip, profile }, {
        contentId: useLaunchId,
        episode: ep,
        startSeconds: parseTimeToSeconds(startAt),
      });
      setPlayState("done");
      onPlayed(item);
    } catch (err) {
      setLog((prev) => [...prev, `\nError: ${err}`]);
      setPlayState("error");
    }
  };

  const handlePlayOnMacAt = async (epNum: number) => {
    setMacError(null);
    setMacPlayState("opening");
    const useLaunchId = launchContentId?.trim() || null;
    const ep = useLaunchId ? null : isSeries ? epNum : null;
    try {
      await playOnMac(item, {
        contentId: useLaunchId,
        episode: ep,
      });
      setMacPlayState("done");
      onClose();
    } catch (err) {
      setMacError(String(err));
      setMacPlayState("error");
    }
  };

  const isBusy = playState === "playing" || macPlayState === "opening";

  // Keyboard: Escape closes
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape" && playState !== "playing") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [playState, onClose]);

  const imageUrl = item.image_url
    ? item.image_url.replace(/\._UR\d+,\d+_\./, "._UR960,540_.")
    : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget && playState !== "playing") onClose();
      }}
    >
      <div
        data-media-dialog
        className="bg-[#1A242F] rounded-2xl shadow-2xl w-full max-w-lg mx-4 overflow-hidden select-none"
        onContextMenu={(e) => {
          const items = titleMenuItems();
          if (items.length > 0) openMenu(e, items);
        }}
        onMouseDown={(e) => {
          if (e.button === 2) window.getSelection()?.removeAllRanges();
        }}
      >

        {/* ── Hero banner ────────────────────────────────────────────────── */}
        <div
          className="relative h-44"
          onContextMenu={(e) => {
            const items = titleMenuItems();
            if (items.length > 0) openMenu(e, items);
          }}
        >
          {imageUrl ? (
            <img
              src={imageUrl}
              alt={item.title}
              className="w-full h-full object-cover"
            />
          ) : (
            <div className="w-full h-full bg-gradient-to-br from-[#1A242F] to-[#0a0f15]" />
          )}
          {/* dark scrim so text is always readable */}
          <div className="absolute inset-0 bg-gradient-to-t from-[#1A242F] via-black/40 to-transparent" />

          {onToggleBookmark && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                onToggleBookmark(item);
              }}
              title={titleBookmarked ? "Remove bookmark" : "Bookmark series"}
              className={`absolute top-3 left-3 p-1.5 rounded-full transition-colors ${
                titleBookmarked
                  ? "bg-amber-500 text-white"
                  : "bg-black/50 text-zinc-300 hover:text-white hover:bg-black/70"
              }`}
            >
              <BookmarkIcon filled={titleBookmarked} />
            </button>
          )}

          {/* close button */}
          <button
            onClick={onClose}
            className="absolute top-3 right-3 p-1.5 rounded-full bg-black/50 text-zinc-400
                       hover:text-white hover:bg-black/70 transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>

          {/* title / meta overlay */}
          <div className="absolute bottom-3 left-4 right-14">
            <h2 className="text-white font-bold text-xl leading-tight line-clamp-1">
              {item.title}
            </h2>
            <div className="flex flex-wrap items-center gap-2 mt-1">
              {item.entity_type && (
                <span className="text-zinc-400 text-xs">{item.entity_type}</span>
              )}
              {item.year && (
                <span className="text-zinc-400 text-xs">{item.year}</span>
              )}
              {item.runtime_str && (
                <span className="text-zinc-500 text-xs">{item.runtime_str}</span>
              )}
              {label !== "-" && (
                <span className={`text-[11px] px-2 py-0.5 rounded-full font-semibold ${badgeStyle}`}>
                  {label === "Prime" ? "Included with Prime" : label}
                </span>
              )}
            </div>
          </div>
        </div>

        {/* ── Body ───────────────────────────────────────────────────────── */}
        <div className="p-5 space-y-4">

          {/* Synopsis */}
          {item.synopsis && (
            <p className="text-zinc-400 text-sm leading-relaxed line-clamp-3">
              {item.synopsis}
            </p>
          )}

          {/* Availability detail */}
          {item.availability && (
            <p className="text-xs text-zinc-500 italic">{item.availability}</p>
          )}

          {/* Episode selector — TV series only ──────────────────────────── */}
          {isSeries && (
            <div className="bg-emerald-950/40 border border-emerald-800/40 rounded-xl px-4 py-3 space-y-3">
              {/* Header row */}
              <div className="flex items-center gap-3">
                {/* Series icon */}
                <svg className="w-6 h-6 text-emerald-400 shrink-0" fill="none" stroke="currentColor"
                  strokeWidth={1.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round"
                    d="M3.75 3v11.25A2.25 2.25 0 006 16.5h12A2.25 2.25 0 0020.25 14.25V3M3.75 21h16.5M16.5 3v18M7.5 3v18" />
                </svg>
                <div className="flex-1 min-w-0">
                  <p className="text-emerald-300 text-sm font-medium">TV Series</p>
                  <p className="text-emerald-600/80 text-xs">
                    {epListState === "loading"
                      ? "Loading episodes…"
                      : hasEpisodeList
                      ? `${episodes.length} episode${episodes.length === 1 ? "" : "s"} — pick one to play`
                      : "Choose the episode to play"}
                  </p>
                </div>

                {/* Numeric stepper — only when no list is available */}
                {!hasEpisodeList && (
                  <div className="flex items-center gap-1 shrink-0">
                    {epListState === "loading" ? (
                      <svg className="w-4 h-4 text-emerald-400 animate-spin" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                      </svg>
                    ) : (
                      <>
                        <span className="text-xs text-zinc-400 mr-1">Episode</span>
                        <button
                          onClick={() => setEpisode((e) => Math.max(1, e - 1))}
                          disabled={playState === "playing" || episode <= 1}
                          className="w-7 h-7 flex items-center justify-center rounded-lg bg-zinc-700
                                     hover:bg-zinc-600 text-white text-lg leading-none font-bold
                                     disabled:opacity-40 transition-colors"
                        >
                          −
                        </button>
                        <input
                          type="number"
                          min={1}
                          max={999}
                          value={episode}
                          onChange={(e) => setEpisode(Math.max(1, parseInt(e.target.value) || 1))}
                          disabled={playState === "playing"}
                          className="w-14 bg-zinc-700 border border-zinc-600 rounded-lg px-2 py-1 text-sm
                                     text-white text-center focus:outline-none focus:border-emerald-500
                                     transition-colors disabled:opacity-50"
                        />
                        <button
                          onClick={() => setEpisode((e) => e + 1)}
                          disabled={playState === "playing"}
                          className="w-7 h-7 flex items-center justify-center rounded-lg bg-zinc-700
                                     hover:bg-zinc-600 text-white text-lg leading-none font-bold
                                     disabled:opacity-40 transition-colors"
                        >
                          +
                        </button>
                      </>
                    )}
                  </div>
                )}
              </div>

              {/* Scrollable episode list */}
              {hasEpisodeList && (
                <div className="max-h-56 overflow-y-auto rounded-lg border border-emerald-900/50
                                divide-y divide-emerald-900/40 bg-black/20">
                  {episodes.map((ep, idx) => {
                    const num = ep.sequence_number ?? idx + 1;
                    const active = episode === idx + 1;
                    return (
                      <div
                        key={ep.content_id || idx}
                        className={`flex items-center gap-1 pr-1 transition-colors ${
                          active ? "bg-emerald-600/30" : "hover:bg-emerald-900/30"
                        }`}
                      >
                        <button
                          type="button"
                          onClick={() => setEpisode(idx + 1)}
                          onContextMenu={(e) => openMenu(e, episodeMenuItems(ep, idx))}
                          disabled={playState === "playing"}
                          className="flex-1 flex items-center gap-3 px-3 py-2 text-left
                                     disabled:opacity-50 min-w-0"
                        >
                          <span className={`shrink-0 w-7 h-7 flex items-center justify-center rounded-md
                                            text-xs font-bold tabular-nums ${
                            active ? "bg-emerald-500 text-white" : "bg-zinc-700 text-zinc-300"
                          }`}>
                            {num}
                          </span>
                          <span className={`flex-1 min-w-0 text-sm truncate ${
                            active ? "text-white font-medium" : "text-zinc-300"
                          }`}>
                            {ep.title || `Episode ${num}`}
                          </span>
                          {ep.runtime_min != null && (
                            <span className="shrink-0 text-[11px] text-zinc-500 tabular-nums">
                              {formatRuntimeMin(ep.runtime_min)}
                            </span>
                          )}
                          {bookmarkedIds && isBookmarked(bookmarkedIds, item, ep) && (
                            <BookmarkIcon filled />
                          )}
                          {active && (
                            <svg className="w-4 h-4 text-emerald-400 shrink-0" fill="currentColor" viewBox="0 0 20 20">
                              <path d="M6.3 2.841A1.5 1.5 0 004 4.11V15.89a1.5 1.5 0 002.3 1.269l9.344-5.89a1.5 1.5 0 000-2.538L6.3 2.84z" />
                            </svg>
                          )}
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}

              {epListState === "error" && (
                <p className="text-[11px] text-emerald-700/80">
                  Couldn't load the episode list — enter the episode number manually above.
                </p>
              )}
            </div>
          )}

          {/* TV destination + profile ─────────────────────────────────── */}
          <div className="flex items-center gap-3 bg-zinc-800/60 rounded-xl px-4 py-3">
            {/* TV icon */}
            <svg className="w-7 h-7 text-zinc-400 shrink-0" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round"
                d="M6 20.25h12m-7.5-3v3m3-3v3m-10.125-3h17.25c.621 0 1.125-.504 1.125-1.125V4.875C21.75 4.254 21.246 3.75 20.625 3.75H3.375c-.621 0-1.125.504-1.125 1.125v11.25c0 .621.504 1.125 1.125 1.125z" />
            </svg>
            <div className="flex-1 min-w-0">
              <p className="text-white text-sm font-medium truncate">
                {config.tv_ip}
              </p>
              <p className="text-zinc-500 text-xs">LG TV</p>
            </div>

            {/* Profile quick selector */}
            <div className="flex items-center gap-2 shrink-0">
              <label className="text-xs text-zinc-400 whitespace-nowrap">Profile</label>
              <input
                type="number"
                min={0}
                max={9}
                value={profile}
                onChange={(e) => setProfile(Math.max(0, parseInt(e.target.value) || 0))}
                disabled={playState === "playing"}
                className="w-14 bg-zinc-700 border border-zinc-600 rounded-lg px-2 py-1 text-sm text-white
                           text-center focus:outline-none focus:border-emerald-500 transition-colors
                           disabled:opacity-50"
              />
            </div>

            {/* Settings shortcut */}
            <button
              onClick={() => { onClose(); onOpenSettings(); }}
              title="Change TV IP in Settings"
              className="p-1.5 text-zinc-500 hover:text-white hover:bg-zinc-700 rounded-lg transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
                />
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              </svg>
            </button>
          </div>

          {/* Log output */}
          {log.length > 0 && (
            <div
              ref={logRef}
              className="bg-zinc-900 rounded-xl p-3 h-36 overflow-y-auto
                         font-mono text-[11px] leading-relaxed border border-zinc-800"
            >
              {log.map((line, i) => (
                <span
                  key={i}
                  className={line.startsWith("[err]") ? "text-orange-300" : "text-zinc-300"}
                >
                  {line}
                </span>
              ))}
            </div>
          )}

          {/* Status messages */}
          {playState === "done" && (
            <div className="flex items-center gap-2 text-emerald-400 text-sm justify-center">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
              </svg>
              Sent to TV — enjoy!
            </div>
          )}
          {playState === "error" && (
            <p className="text-center text-red-400 text-sm">
              Could not connect. Check the TV is on and paired.
            </p>
          )}
          {macPlayState === "error" && macError && (
            <p className="text-center text-red-400 text-sm">{macError}</p>
          )}
          {macPlayState === "done" && (
            <p className="text-center text-emerald-400 text-sm">
              Opened in Prime Video — sign in if prompted.
            </p>
          )}

          {/* Length + optional start position ─────────────────────────── */}
          <div className="flex items-center gap-3 bg-zinc-800/60 rounded-xl px-4 py-3">
            {/* Clock icon */}
            <svg className="w-6 h-6 text-zinc-400 shrink-0" fill="none" stroke="currentColor"
              strokeWidth={1.5} viewBox="0 0 24 24">
              <circle cx="12" cy="12" r="9" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 7v5l3 2" />
            </svg>
            <div className="flex-1 min-w-0">
              <p className="text-white text-sm font-medium">
                {selectedRuntimeStr ? `Length: ${selectedRuntimeStr}` : "Length unavailable"}
              </p>
              <p className="text-zinc-500 text-xs">Leave blank to start from the beginning</p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <label className="text-xs text-zinc-400 whitespace-nowrap">Start at</label>
              <input
                type="text"
                inputMode="numeric"
                value={startAt}
                onChange={(e) => setStartAt(e.target.value)}
                placeholder="0:00"
                disabled={playState === "playing"}
                title="Start playback from this position, e.g. 12:30 or 1:05:00"
                className="w-20 bg-zinc-700 border border-zinc-600 rounded-lg px-2 py-1 text-sm
                           text-white text-center font-mono focus:outline-none focus:border-emerald-500
                           transition-colors disabled:opacity-50"
              />
            </div>
          </div>

          {/* Action buttons */}
          <div className="flex gap-2">
            <button
              onClick={() => (preferMac ? handlePlayOnMacAt(episode) : handlePlayAt(episode))}
              disabled={isBusy}
              className={`flex-1 disabled:bg-zinc-600 text-white text-sm font-semibold py-2 px-3
                         rounded-md flex items-center justify-center gap-2 transition-colors ${
                preferMac
                  ? "bg-violet-600 hover:bg-violet-500"
                  : "bg-[#00A8E1] hover:bg-[#0090c0]"
              }`}
            >
              {isBusy && (preferMac ? macPlayState === "opening" : playState === "playing") ? (
                <>
                  <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                    <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                    <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
                  </svg>
                  {preferMac ? "Opening…" : "Connecting…"}
                </>
              ) : (
                <>
                  {preferMac ? <LaptopIcon /> : <PlayIcon />}
                  {preferMac ? "Play on Mac" : (
                    playState === "done" ? "Play Again on TV"
                    : playState === "error" ? "Retry on TV"
                    : selectedEpisode ? `Play ${selectedEpisode.title || `Episode ${selectedEpisode.sequence_number ?? episode}`} on TV`
                    : isSeries ? `Play Episode ${episode} on TV`
                    : "Play on TV"
                  )}
                </>
              )}
            </button>

            <button
              onClick={() => (preferMac ? handlePlayAt(episode) : handlePlayOnMacAt(episode))}
              disabled={isBusy}
              className="flex-1 bg-zinc-700 hover:bg-zinc-600 disabled:opacity-40 text-white text-sm
                         font-semibold py-2 px-3 rounded-md flex items-center justify-center gap-2 transition-colors"
            >
              {preferMac ? <PlayIcon /> : <LaptopIcon />}
              {preferMac ? "Play on TV" : "Play on Mac"}
            </button>

            <button
              onClick={onClose}
              disabled={isBusy}
              className="px-4 py-2 bg-zinc-700 hover:bg-zinc-600 disabled:opacity-40
                         text-white rounded-md transition-colors text-sm font-medium"
            >
              {playState === "done" || playState === "error" || macPlayState === "done" ? "Close" : "Cancel"}
            </button>
          </div>
        </div>
      </div>

    </div>
  );
}

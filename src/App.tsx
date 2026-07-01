import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  PrimeTitle,
  PrimeEpisode,
  AppConfig,
  DEFAULT_CONFIG,
  CatalogGroup,
  EntityTypeFilter,
  Bookmark,
  groupTitles,
  formatCacheAge,
  isTitleVisible,
  cachedImageHttpUrl,
} from "./types";
import {
  bookmarkSnapshot,
  findBookmark,
  groupBookmarks,
  isEpisodeBookmark,
  isMovieBookmark,
  isTvBookmark,
  resolveEpisodePlayId,
} from "./bookmarks";
import { playOnMac } from "./playback";
import CatalogGroupRow from "./components/CatalogGroup";
import ContextMenu, { ContextMenuItem } from "./components/ContextMenu";
import PlayDialog from "./components/PlayDialog";
import { MEDIA_CONTEXT_MENU_EVENT, MediaContextMenuDetail } from "./contextMenuBus";
import SettingsDialog from "./components/SettingsDialog";
import TVRemote from "./components/TVRemote";
import type { PlaybackState } from "./components/TVRemote";

// ─── Collection definitions ───────────────────────────────────────────────────
const COLLECTIONS = [
  { slug: "IncludedwithPrime",      label: "Included with Prime", color: "emerald" },
  { slug: "newandupcoming",         label: "New & Upcoming",      color: "sky"     },
  { slug: "TopRated",               label: "Top Rated",           color: "amber"   },
  { slug: "genre/action",           label: "Action",              color: "orange"  },
  { slug: "genre/anime",            label: "Anime",               color: "pink"    },
  { slug: "genre/comedy",           label: "Comedy",              color: "yellow"  },
  { slug: "genre/documentary",      label: "Documentary",         color: "teal"    },
  { slug: "genre/drama",            label: "Drama",               color: "rose"    },
  { slug: "genre/fantasy",          label: "Fantasy",             color: "indigo"  },
  { slug: "genre/historical",       label: "Historical",          color: "stone"   },
  { slug: "genre/horror",           label: "Horror",              color: "red"     },
  { slug: "genre/romance",          label: "Romance",             color: "fuchsia" },
  { slug: "genre/science-fiction",  label: "Sci-Fi",              color: "violet"  },
  { slug: "genre/suspense",         label: "Suspense",            color: "cyan"    },
] as const;

type CollectionSlug = (typeof COLLECTIONS)[number]["slug"];
type ViewMode = "catalog" | "bookmarks";

type LoadState = "idle" | "loading" | "done" | "error";

function safeId(id: string) {
  return id.replace(/[^\w\-]/g, "_");
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function parseResult(raw: string): { data: PrimeTitle[]; stale: boolean; staleReason?: string } {
  if (raw.startsWith("__STALE__")) {
    const rest = raw.slice(9);
    const sep = rest.indexOf("\u0000");
    const reason = sep >= 0 ? rest.slice(0, sep) : undefined;
    const jsonStr = sep >= 0 ? rest.slice(sep + 1) : rest;
    try { return { data: JSON.parse(jsonStr), stale: true, staleReason: reason }; }
    catch { return { data: [], stale: true, staleReason: reason }; }
  }
  try { return { data: JSON.parse(raw), stale: false }; }
  catch { return { data: [], stale: false }; }
}

// ─────────────────────────────────────────────────────────────────────────────
export default function App() {
  const [config, setConfig] = useState<AppConfig>(DEFAULT_CONFIG);

  // Catalog data
  const [allItems, setAllItems]     = useState<PrimeTitle[]>([]);
  const [loadState, setLoadState]   = useState<LoadState>("idle");
  const [error, setError]           = useState<string | null>(null);
  const [isStale, setIsStale]       = useState(false);
  const [staleReason, setStaleReason] = useState<string | null>(null);
  const [cacheAgeSecs, setCacheAge] = useState<number | null>(null);
  const [primeRegion, setPrimeRegion] = useState<string | null>(null);
  const [publicIp, setPublicIp] = useState<{ ip: string; country: string } | null>(null);

  // Collection / bookmarks view
  const [viewMode, setViewMode] = useState<ViewMode>("catalog");
  const [collection, setCollection] = useState<CollectionSlug>("IncludedwithPrime");
  const [bookmarks, setBookmarks] = useState<Bookmark[]>([]);

  // Image server port (0 until the server starts)
  const [imgPort, setImgPort] = useState(0);
  // Set of content_id stems that are cached on disk; URL = http://127.0.0.1:{port}/{safe}.jpg
  const [imageCache, setImageCache] = useState<Set<string>>(new Set());

  // Search
  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching]     = useState(false);
  const searchTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Filters
  const [typeFilter, setTypeFilter] = useState<EntityTypeFilter>("all");

  // Dialogs
  const [selectedItem, setSelectedItem] = useState<PrimeTitle | null>(null);
  const [selectedEpisode, setSelectedEpisode] = useState<number | null>(null);
  const [selectedLaunchContentId, setSelectedLaunchContentId] = useState<string | null>(null);
  const [showSettings, setShowSettings] = useState(false);
  const [globalMenu, setGlobalMenu] = useState<{
    x: number;
    y: number;
    items: ContextMenuItem[];
  } | null>(null);

  // Now-playing (drives the TVRemote bar)
  const [nowPlaying, setNowPlaying]         = useState<PrimeTitle | null>(null);
  const [nowPlayingEpisode, setNPEpisode]   = useState<number | null>(null);
  const [nowPlayingStart, setNPStart]       = useState<number | null>(null);
  const [playbackState, setPlaybackState]   = useState<PlaybackState>("playing");

  const stopTvPlayback = useCallback(async () => {
    try {
      await invoke("media_control", { action: "stop" });
    } catch {
      // Still clear the dock if the TV is unreachable.
    }
    setPlaybackState("paused");
    setNowPlaying(null);
    setNPEpisode(null);
    setNPStart(null);
  }, []);

  // Escape stops TV playback when something is playing (same as the stop button).
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || !nowPlaying) return;
      if (globalMenu || showSettings) return;

      const el = e.target;
      if (
        el instanceof HTMLInputElement ||
        el instanceof HTMLTextAreaElement ||
        (el instanceof HTMLElement && el.isContentEditable)
      ) {
        return;
      }

      e.preventDefault();
      void stopTvPlayback();
    };

    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [nowPlaying, globalMenu, showSettings, stopTvPlayback]);

  // ── Global media context menu (single instance above all UI) ─────────────────
  useEffect(() => {
    const onMenu = (e: Event) => {
      const detail = (e as CustomEvent<MediaContextMenuDetail>).detail;
      setGlobalMenu(detail);
    };
    window.addEventListener(MEDIA_CONTEXT_MENU_EVENT, onMenu);
    return () => window.removeEventListener(MEDIA_CONTEXT_MENU_EVENT, onMenu);
  }, []);

  // Suppress macOS text lookup menus outside search fields.
  useEffect(() => {
    const isTextField = (el: EventTarget | null) => {
      if (!(el instanceof HTMLElement)) return false;
      return (
        el instanceof HTMLInputElement ||
        el instanceof HTMLTextAreaElement ||
        el.isContentEditable
      );
    };

    const onSelectStart = (e: Event) => {
      if (!isTextField(e.target)) e.preventDefault();
    };

    const onMouseDown = (e: MouseEvent) => {
      if (e.button === 2 && !isTextField(e.target)) {
        window.getSelection()?.removeAllRanges();
      }
    };

    const onContextMenu = (e: MouseEvent) => {
      if (isTextField(e.target)) return;
      window.getSelection()?.removeAllRanges();
      // Let media cards/dialogs show the custom menu (Play, TMDB, Bookmark).
      const onMedia = (e.target as HTMLElement).closest(
        "[data-media-card], [data-media-dialog]",
      );
      if (onMedia) return;
      e.preventDefault();
    };

    document.addEventListener("selectstart", onSelectStart, { capture: true });
    document.addEventListener("mousedown", onMouseDown, { capture: true });
    document.addEventListener("contextmenu", onContextMenu, { capture: true });
    return () => {
      document.removeEventListener("selectstart", onSelectStart, { capture: true });
      document.removeEventListener("mousedown", onMouseDown, { capture: true });
      document.removeEventListener("contextmenu", onContextMenu, { capture: true });
    };
  }, []);

  // ── Config load ─────────────────────────────────────────────────────────────
  useEffect(() => {
    invoke<AppConfig>("get_config")
      .then((cfg) => {
        setConfig(cfg);
        return invoke<AppConfig>("discover_tv_mac");
      })
      .then((cfg) => setConfig(cfg))
      .catch(() => {});
  }, []);

  useEffect(() => {
    const unlisten = listen<AppConfig>("config-updated", (event) => {
      setConfig(event.payload);
    });
    return () => {
      unlisten.then((fn) => fn());
    };
  }, []);

  useEffect(() => {
    if (!config.detect_vpn_region) {
      setPrimeRegion(null);
      setPublicIp(null);
      return;
    }
    invoke<string>("get_prime_region")
      .then((region) => setPrimeRegion(region !== "unknown" ? region : null))
      .catch(() => {});
    invoke<{ ip: string | null; country: string | null }>("get_public_ip")
      .then((info) => setPublicIp(info.ip ? { ip: info.ip, country: info.country ?? "" } : null))
      .catch(() => {});
  }, [config.detect_vpn_region]);

  // ── Bookmarks load ──────────────────────────────────────────────────────────
  const reloadBookmarks = useCallback(() => {
    invoke<Bookmark[]>("get_bookmarks")
      .then(setBookmarks)
      .catch(() => {});
  }, []);

  useEffect(() => {
    reloadBookmarks();
  }, [reloadBookmarks]);

  useEffect(() => {
    if (bookmarks.length === 0 || !imgPort) return;
    const missing = bookmarks
      .map((b) => b.item)
      .filter((item) => item.image_url && !imageCache.has(safeId(item.content_id)));
    if (missing.length > 0) {
      invoke("prefetch_images", {
        items: missing.map((i) => ({ content_id: i.content_id, url: i.image_url! })),
      }).catch(() => {});
    }
  }, [bookmarks, imgPort, imageCache]);

  const handleToggleBookmark = useCallback(
    async (
      item: PrimeTitle,
      options?: {
        episode?: PrimeEpisode | null;
        episodeIndex?: number;
        sourceItem?: PrimeTitle;
        playEpisode?: number;
      }
    ) => {
      const snapshot = options?.episode
        ? bookmarkSnapshot(item, options.episode, options.episodeIndex)
        : item;
      const sourceItem = options?.sourceItem ?? (options?.episode ? item : null);
      const playEpisode =
        options?.playEpisode ??
        (options?.episode
          ? options.episode.sequence_number ?? (options.episodeIndex != null ? options.episodeIndex + 1 : null)
          : null);
      const episodeContentId = options?.episode?.content_id ?? null;

      try {
        const added = await invoke<boolean>("toggle_bookmark", {
          item: snapshot,
          sourceItem,
          episodeContentId,
          playEpisode,
        });
        const updated = await invoke<Bookmark[]>("get_bookmarks");
        setBookmarks(updated);
        const imageSource = sourceItem ?? item;
        if (added && imageSource.image_url) {
          const stem = safeId(imageSource.content_id);
          if (!imageCache.has(stem)) {
            invoke("prefetch_images", {
              items: [{ content_id: imageSource.content_id, url: imageSource.image_url }],
            }).catch(() => {});
          }
        }
      } catch {
        /* ignore */
      }
    },
    [imageCache]
  );

  const playEpisodeBookmark = useCallback(
    async (bookmark: Bookmark): Promise<boolean> => {
      const episodeNum = bookmark.play_episode ?? null;
      const seriesId = bookmark.source_item?.content_id ?? null;
      const episodeId = resolveEpisodePlayId(bookmark);

      // Prefer the proven play path: launch the series content_id with an
      // explicit episode number, which the resolver maps to the right episode.
      // Fall back to the episode's own detail ID only when we lack the series
      // reference or episode number.
      let contentId: string;
      let episode: number | null;
      if (seriesId && episodeNum && episodeNum >= 1) {
        contentId = seriesId;
        episode = episodeNum;
      } else if (episodeId) {
        contentId = episodeId;
        episode = null;
      } else {
        return false;
      }

      setSelectedItem(null);
      setSelectedEpisode(null);
      setNowPlaying(bookmark.item);
      setNPEpisode(episodeNum);
      setNPStart(null);
      setPlaybackState("playing");

      try {
        await invoke("play_on_tv", {
          contentId,
          profile: config.profile,
          tvIp: config.tv_ip,
          episode,
        });
        return true;
      } catch (err) {
        setError(String(err));
        setPlaybackState("paused");
        setNowPlaying(null);
        setNPEpisode(null);
        return false;
      }
    },
    [config]
  );

  const handleOpenTitle = useCallback(
    async (item: PrimeTitle) => {
      const bm = findBookmark(bookmarks, item);

      if (bm && isEpisodeBookmark(bm)) {
        const played = await playEpisodeBookmark(bm);
        if (played) return;
        if (bm.source_item && bm.play_episode) {
          setSelectedItem(bm.source_item);
          setSelectedEpisode(bm.play_episode);
          setSelectedLaunchContentId(resolveEpisodePlayId(bm));
          return;
        }
      }

      setSelectedLaunchContentId(null);
      if (bm?.source_item && bm.play_episode) {
        setSelectedItem(bm.source_item);
        setSelectedEpisode(bm.play_episode);
      } else {
        setSelectedItem(item);
        setSelectedEpisode(null);
      }
    },
    [bookmarks, playEpisodeBookmark]
  );

  const handlePlayOnMac = useCallback(async (item: PrimeTitle) => {
    try {
      await playOnMac(item);
    } catch (err) {
      setError(String(err));
    }
  }, []);

  const bookmarkedIds = useMemo(
    () => new Set(bookmarks.map((b) => b.content_id)),
    [bookmarks]
  );

  // ── Fetch the image HTTP server port + pre-existing cache IDs ───────────────
  useEffect(() => {
    invoke<number>("get_image_server_port").then(setImgPort).catch(() => {});
    invoke<string[]>("list_cached_images")
      .then((ids) => setImageCache(new Set(ids)))
      .catch(() => {});
  }, []);

  // ── Listen for newly downloaded image-cached events ─────────────────────────
  useEffect(() => {
    const unlisten = listen<string>("image-cached", (event) => {
      setImageCache((prev) => {
        const next = new Set(prev);
        next.add(event.payload);          // payload is now just the content_id stem
        return next;
      });
    });
    return () => { unlisten.then((fn) => fn()); };
  }, []);

  // ── Load catalog ────────────────────────────────────────────────────────────
  const loadCatalog = useCallback(
    async (forceRefresh = false, slug: CollectionSlug = collection) => {
      setLoadState("loading");
      setError(null);
      setIsStale(false);
      setStaleReason(null);
      setSearchQuery("");
      if (searchTimerRef.current) clearTimeout(searchTimerRef.current);

      if (!forceRefresh) {
        invoke<number | null>("collection_cache_age", { collection: slug })
          .then((age) => setCacheAge(age ?? null))
          .catch(() => setCacheAge(null));
      } else {
        setCacheAge(null);
      }

      try {
        const raw = await invoke<string>("load_catalog", {
          collection: slug,
          forceRefresh,
        });
        const { data, stale, staleReason: reason } = parseResult(raw);
        setAllItems(data);
        setIsStale(stale);
        setStaleReason(stale ? reason ?? null : null);
        setLoadState("done");

        if (config.detect_vpn_region) {
          invoke<string>("get_prime_region")
            .then((region) => setPrimeRegion(region !== "unknown" ? region : null))
            .catch(() => {});
          invoke<{ ip: string | null; country: string | null }>("get_public_ip")
            .then((info) => setPublicIp(info.ip ? { ip: info.ip, country: info.country ?? "" } : null))
            .catch(() => {});
        } else {
          setPrimeRegion(null);
          setPublicIp(null);
        }

        invoke<number | null>("collection_cache_age", { collection: slug })
          .then((age) => setCacheAge(age ?? null))
          .catch(() => {});

        // Prefetch posters; on hard refresh re-download even if a JPEG already exists.
        const toPrefetch = data.filter((item) => {
          if (!item.image_url) return false;
          if (forceRefresh) return true;
          return !imageCache.has(safeId(item.content_id));
        });
        if (toPrefetch.length > 0) {
          invoke("prefetch_images", {
            items: toPrefetch.map((i) => ({
              content_id: i.content_id,
              url: i.image_url!,
            })),
          }).catch(() => {});
        }
      } catch (err) {
        setError(String(err));
        setLoadState("error");
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [collection]
  );

  useEffect(() => {
    if (viewMode !== "catalog") return;
    loadCatalog(false, collection);
    setTypeFilter("all");
  }, [collection, viewMode]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Debounced search ────────────────────────────────────────────────────────
  const handleSearchChange = (q: string) => {
    setSearchQuery(q);
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);

    if (q.trim()) {
      setViewMode("catalog");
    }

    if (!q.trim()) {
      if (viewMode === "catalog") loadCatalog(false, collection);
      return;
    }

    searchTimerRef.current = setTimeout(async () => {
      setSearching(true);
      setError(null);
      setIsStale(false);
      setStaleReason(null);

      invoke<number | null>("search_cache_age", { query: q.trim() })
        .then((age) => setCacheAge(age ?? null))
        .catch(() => setCacheAge(null));

      try {
        const raw = await invoke<string>("search_catalog", {
          query: q.trim(),
          forceRefresh: false,
        });
        const { data, stale, staleReason: reason } = parseResult(raw);
        setAllItems(data);
        setIsStale(stale);
        setStaleReason(stale ? reason ?? null : null);
        setLoadState("done");

        // Prefetch images for search results too
        const missing = data.filter(
          (item) => item.image_url && !imageCache.has(safeId(item.content_id))
        );
        if (missing.length > 0) {
          invoke("prefetch_images", {
            items: missing.map((i) => ({ content_id: i.content_id, url: i.image_url! })),
          }).catch(() => {});
        }
      } catch (err) {
        setError(String(err));
        setLoadState("error");
      } finally {
        setSearching(false);
      }
    }, 600);
  };

  const handleHardRefresh = async () => {
    if (searchQuery.trim()) {
      setSearching(true);
      setError(null);
      setIsStale(false);
      setStaleReason(null);
      setCacheAge(null);
      try {
        const raw = await invoke<string>("search_catalog", {
          query: searchQuery.trim(),
          forceRefresh: true,
        });
        const { data, stale, staleReason: reason } = parseResult(raw);
        setAllItems(data);
        setIsStale(stale);
        setStaleReason(stale ? reason ?? null : null);
        setLoadState("done");
      } catch (err) {
        setError(String(err));
        setLoadState("error");
      } finally {
        setSearching(false);
      }
    } else {
      loadCatalog(true, collection);
    }
  };

  // ── Filtered groups ─────────────────────────────────────────────────────────
  const sourceItems = useMemo<PrimeTitle[]>(
    () => (viewMode === "bookmarks" ? bookmarks.map((b) => b.item) : allItems),
    [viewMode, bookmarks, allItems]
  );

  const filteredGroups = useMemo<CatalogGroup[]>(() => {
    const items = sourceItems.filter((item) => {
      if (typeFilter === "Movie" && item.entity_type !== "Movie") return false;
      if (
        typeFilter === "TV Show"
        && item.entity_type !== "TV Show"
        && item.entity_type !== "TV Episode"
      ) {
        return false;
      }
      return isTitleVisible(item, config);
    });
    if (viewMode === "bookmarks") {
      return groupBookmarks(items);
    }
    return groupTitles(items).filter((g) => g.items.length > 0);
  }, [sourceItems, typeFilter, config, viewMode]);

  const totalFiltered = filteredGroups.reduce((s, g) => s + g.items.length, 0);
  const movieCount    = sourceItems.filter((i) => isMovieBookmark(i)).length;
  const showCount     = sourceItems.filter((i) => isTvBookmark(i)).length;
  const sourceCount   = sourceItems.length;

  const isLoading = viewMode === "catalog" && (loadState === "loading" || searching);
  const activeCollection = COLLECTIONS.find((c) => c.slug === collection)!;
  const showCatalogData = viewMode === "catalog" && loadState === "done";
  const showBookmarksData = viewMode === "bookmarks";

  return (
    <div className="min-h-screen bg-[#0F171E] flex flex-col">

      {/* ── Header ────────────────────────────────────────────────────────── */}
      <header className="sticky top-0 z-40 bg-[#0F171E]/95 backdrop-blur border-b border-zinc-800/60">

        {/* Top row */}
        <div className="flex items-center gap-3 px-6 py-3">
          {/* Logo */}
          <div className="flex items-center gap-2 shrink-0">
            <img
              src="/logo-64.png"
              alt=""
              className="w-8 h-8 rounded-md shadow-lg"
            />
            <div>
              <p className="text-white font-bold text-sm leading-none">Prime Remote Control</p>
              <p className="text-zinc-500 text-[10px] leading-none mt-0.5">LG TV</p>
            </div>
          </div>

          {/* Search */}
          <div className="flex-1 max-w-lg">
            <div className="relative">
              <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-500"
                fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
              </svg>
              <input
                type="search"
                value={searchQuery}
                onChange={(e) => handleSearchChange(e.target.value)}
                placeholder="Search all Prime Video…"
                className="w-full bg-zinc-800/80 border border-zinc-700 rounded-lg pl-9 pr-4 py-2
                           text-sm text-white placeholder-zinc-500 focus:outline-none
                           focus:border-emerald-500 transition-colors"
              />
              {isLoading && (
                <svg className="absolute right-3 top-1/2 -translate-y-1/2 w-4 h-4 text-zinc-400 animate-spin"
                  fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
                </svg>
              )}
            </div>
          </div>

          {/* Right actions */}
          <div className="flex items-center gap-2 ml-auto shrink-0">
            {/* Cache age */}
            {viewMode === "bookmarks" ? (
              <button
                onClick={() => setViewMode("bookmarks")}
                title="Bookmarks"
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium
                           bg-amber-600/20 text-amber-300 border border-amber-700/60 rounded-lg"
              >
                <svg className="w-3.5 h-3.5" fill="currentColor" viewBox="0 0 20 20">
                  <path d="M5 4a2 2 0 012-2h6a2 2 0 012 2v14l-5-2.5L5 18V4z" />
                </svg>
                {bookmarks.length}
              </button>
            ) : (
              <button
                onClick={() => { setViewMode("bookmarks"); setSearchQuery(""); }}
                title="View bookmarks"
                className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium
                           text-zinc-400 hover:text-amber-300 hover:bg-zinc-800/60
                           border border-zinc-700 hover:border-amber-700/50 rounded-lg transition-colors"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round"
                    d="M17.593 3.322c1.1.128 1.907 1.077 1.907 2.185V21L12 17.25 4.5 21V5.507c0-1.108.806-2.057 1.907-2.185a48.507 48.507 0 0111.186 0z" />
                </svg>
                Bookmarks
                {bookmarks.length > 0 && (
                  <span className="text-[10px] bg-amber-600/80 text-white px-1.5 py-0.5 rounded-full min-w-[1.25rem] text-center">
                    {bookmarks.length}
                  </span>
                )}
              </button>
            )}

            {config.detect_vpn_region && primeRegion && showCatalogData && (
              <div
                title={`Prime Video catalog region: ${primeRegion} (based on your current VPN/network exit location). Search results and availability are limited to this region's catalog.`}
                className="hidden sm:flex items-center gap-1.5 text-[11px] px-2.5 py-1
                              rounded-full border bg-zinc-800/60 border-zinc-700 text-zinc-400">
                <svg className="w-3 h-3 text-zinc-500" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round"
                    d="M12 21c4.5-4.5 7-8.25 7-11.5A7 7 0 105 9.5C5 12.75 7.5 16.5 12 21z" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 12a2.5 2.5 0 100-5 2.5 2.5 0 000 5z" />
                </svg>
                <span className="text-zinc-500">Region</span>
                <span className="font-medium text-zinc-300">{primeRegion}</span>
              </div>
            )}

            {config.detect_vpn_region && publicIp && (
              <div
                title={`Outgoing VPN IP address: ${publicIp.ip}${publicIp.country ? ` (${publicIp.country})` : ""}. This is the public address Prime Video sees your connection from.`}
                className="hidden sm:flex items-center gap-1.5 text-[11px] px-2.5 py-1
                              rounded-full border bg-zinc-800/60 border-zinc-700 text-zinc-400">
                <svg className="w-3 h-3 text-zinc-500" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round"
                    d="M9 3.75H6.912a2.25 2.25 0 00-2.15 1.588L2.35 13.177a2.25 2.25 0 00-.1.661V18a2.25 2.25 0 002.25 2.25h15a2.25 2.25 0 002.25-2.25v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 00-2.15-1.588H15M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859" />
                </svg>
                <span className="font-mono text-zinc-300">{publicIp.ip}</span>
                {publicIp.country && (
                  <span className="font-medium text-zinc-400">· {publicIp.country}</span>
                )}
              </div>
            )}

            {cacheAgeSecs !== null && showCatalogData && !searchQuery && (
              <div className={`hidden sm:flex items-center gap-1.5 text-[11px] px-2.5 py-1
                              rounded-full border ${isStale
                ? "bg-orange-950/60 border-orange-700 text-orange-300"
                : "bg-zinc-800/60 border-zinc-700 text-zinc-400"}`}>
                <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round"
                    d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                {isStale ? "Stale · " : "Cached · "}{formatCacheAge(cacheAgeSecs)}
              </div>
            )}

            {/* TV chip (with outgoing VPN IP stacked underneath) */}
            <div className="hidden sm:flex flex-col justify-center gap-0.5 bg-zinc-800/60 rounded-xl px-3 py-1">
              <div title={`LG TV at ${config.tv_ip}`} className="flex items-center gap-1.5">
                <svg className="w-3.5 h-3.5 text-zinc-400" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                  <rect x="2.75" y="4.75" width="18.5" height="12.5" rx="1.75" strokeLinecap="round" strokeLinejoin="round" />
                  <path strokeLinecap="round" strokeLinejoin="round" d="M8.5 20.25h7M12 17.25v3" />
                </svg>
                <div className="w-2 h-2 rounded-full bg-emerald-500" />
                <span className="text-xs text-zinc-400 font-mono">{config.tv_ip}</span>
              </div>
              {config.detect_vpn_region && publicIp && (
                <div
                  title={`Outgoing VPN IP address: ${publicIp.ip}${publicIp.country ? ` (${publicIp.country})` : ""}. This is the public address Prime Video sees your connection from.`}
                  className="flex items-center gap-1.5"
                >
                  <svg className="w-3.5 h-3.5 text-zinc-500" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round"
                      d="M9 3.75H6.912a2.25 2.25 0 00-2.15 1.588L2.35 13.177a2.25 2.25 0 00-.1.661V18a2.25 2.25 0 002.25 2.25h15a2.25 2.25 0 002.25-2.25v-4.162c0-.224-.034-.447-.1-.661L19.24 5.338a2.25 2.25 0 00-2.15-1.588H15M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859" />
                  </svg>
                  <span className="text-xs text-zinc-500 font-mono">{publicIp.ip}</span>
                  {publicIp.country && (
                    <span className="text-xs font-medium text-zinc-500">· {publicIp.country}</span>
                  )}
                </div>
              )}
            </div>

            {/* Soft reload */}
            <button
              onClick={() => viewMode === "bookmarks" ? reloadBookmarks() : loadCatalog(false, collection)}
              disabled={isLoading}
              title={viewMode === "bookmarks" ? "Reload bookmarks" : "Reload from cache"}
              className="p-1.5 text-zinc-500 hover:text-white hover:bg-zinc-700/60 rounded-lg
                         transition-colors disabled:opacity-30">
              <svg className={`w-4 h-4 ${loadState === "loading" && !searching ? "animate-spin" : ""}`}
                fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            </button>

            {/* Hard refresh */}
            <button onClick={handleHardRefresh} disabled={isLoading || viewMode === "bookmarks"}
              title="Re-download from Prime Video"
              className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium
                         text-zinc-300 hover:text-white bg-zinc-800/60 hover:bg-zinc-700
                         border border-zinc-700 hover:border-zinc-500 rounded-lg
                         transition-colors disabled:opacity-30">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
              </svg>
              Refresh
            </button>

            {/* Settings */}
            <button onClick={() => setShowSettings(true)} title="Settings"
              className="p-1.5 text-zinc-400 hover:text-white hover:bg-zinc-700/60 rounded-lg transition-colors">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
                />
                <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              </svg>
            </button>
          </div>
        </div>

        {/* Collection tabs (hidden when searching) */}
        {!searchQuery && (
          <div
            className="flex items-center gap-1 px-6 pt-0.5 pb-2 overflow-x-auto"
            style={{ scrollbarWidth: "thin" }}
          >
            {COLLECTIONS.map((col) => {
              const active = viewMode === "catalog" && col.slug === collection;
              const colorMap: Record<string, string> = {
                emerald: active ? "bg-emerald-600 text-white" : "text-emerald-400/70 hover:text-emerald-300",
                sky:     active ? "bg-sky-600 text-white"     : "text-sky-400/70 hover:text-sky-300",
                amber:   active ? "bg-amber-600 text-white"   : "text-amber-400/70 hover:text-amber-300",
                violet:  active ? "bg-violet-600 text-white" : "text-violet-400/70 hover:text-violet-300",
                rose:    active ? "bg-rose-600 text-white"    : "text-rose-400/70 hover:text-rose-300",
                yellow:  active ? "bg-yellow-600 text-white"  : "text-yellow-400/70 hover:text-yellow-300",
                red:     active ? "bg-red-600 text-white"     : "text-red-400/70 hover:text-red-300",
                orange:  active ? "bg-orange-600 text-white" : "text-orange-400/70 hover:text-orange-300",
                pink:    active ? "bg-pink-600 text-white"    : "text-pink-400/70 hover:text-pink-300",
                teal:    active ? "bg-teal-600 text-white"    : "text-teal-400/70 hover:text-teal-300",
                indigo:  active ? "bg-indigo-600 text-white"  : "text-indigo-400/70 hover:text-indigo-300",
                stone:   active ? "bg-stone-600 text-white"   : "text-stone-400/70 hover:text-stone-300",
                fuchsia: active ? "bg-fuchsia-600 text-white" : "text-fuchsia-400/70 hover:text-fuchsia-300",
                cyan:    active ? "bg-cyan-600 text-white"    : "text-cyan-400/70 hover:text-cyan-300",
              };
              return (
                <button
                  key={col.slug}
                  onClick={() => {
                    setViewMode("catalog");
                    if (col.slug !== collection) {
                      setCollection(col.slug as CollectionSlug);
                    }
                  }}
                  className={`px-3.5 py-1.5 rounded-full text-xs font-semibold transition-colors
                    ${colorMap[col.color]} ${active ? "" : "hover:bg-zinc-800/60"}`}
                >
                  {col.label}
                </button>
              );
            })}
            {showCatalogData && allItems.length > 0 && (
              <span className="ml-2 text-xs text-zinc-600">
                {allItems.length} titles
              </span>
            )}
            {showBookmarksData && (
              <span className="ml-2 text-xs text-zinc-600">
                {bookmarks.length} saved
              </span>
            )}
          </div>
        )}

        {/* Entity-type filter + count row (after data loads) */}
        {((showCatalogData && allItems.length > 0) || (showBookmarksData && bookmarks.length > 0)) && (
          <div className="flex items-center gap-1 px-6 pb-2.5">
            {(
              [
                { key: "all",     label: "All",      count: sourceCount },
                { key: "Movie",   label: "Movies",   count: movieCount },
                { key: "TV Show", label: "TV Shows", count: showCount },
              ] as { key: EntityTypeFilter; label: string; count: number }[]
            ).map(({ key, label, count }) => (
              <button
                key={key}
                onClick={() => setTypeFilter(key)}
                className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                  typeFilter === key
                    ? "bg-zinc-700 text-white"
                    : "text-zinc-500 hover:text-white hover:bg-zinc-800/60"
                }`}
              >
                {label}
                <span className={`ml-1.5 text-[10px] ${typeFilter === key ? "text-zinc-400" : "text-zinc-700"}`}>
                  {count}
                </span>
              </button>
            ))}

            {searchQuery && (
              <span className="ml-2 text-xs text-zinc-500">
                Results for <span className="text-zinc-300 font-medium">"{searchQuery}"</span>
              </span>
            )}

            <span className="ml-auto text-xs text-zinc-600">
              {totalFiltered} shown
            </span>
          </div>
        )}
      </header>

      {/* ── Main content ──────────────────────────────────────────────────── */}
      {/* pb-28 clears the fixed TVRemote bar even when seek bar is open */}
      <main className="flex-1 py-6 pb-28">

        {/* Loading */}
        {loadState === "loading" && !searching && (
          <div className="flex flex-col items-center justify-center h-64 gap-4">
            <svg className="w-10 h-10 text-emerald-500 animate-spin" fill="none" viewBox="0 0 24 24">
              <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"/>
              <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/>
            </svg>
            <p className="text-zinc-400 text-sm">Loading {activeCollection.label}…</p>
            <p className="text-zinc-600 text-xs">
              Fetching titles &amp; resolving availability (~30 s on first load)
            </p>
          </div>
        )}

        {/* Error */}
        {viewMode === "catalog" && loadState === "error" && error && (
          <div className="mx-6 mt-4">
            <div className="bg-red-950/50 border border-red-800 rounded-xl p-5">
              <h3 className="text-red-300 font-semibold mb-2">Failed to load catalog</h3>
              <pre className="text-red-400/80 text-xs font-mono whitespace-pre-wrap break-words
                              max-h-48 overflow-y-auto">{error}</pre>
              <button onClick={() => loadCatalog()}
                className="mt-3 bg-red-800 hover:bg-red-700 text-white text-sm px-4 py-2 rounded-lg transition-colors">
                Retry
              </button>
            </div>
          </div>
        )}

        {/* Stale warning */}
        {viewMode === "catalog" && isStale && loadState === "done" && (
          <div className="mx-6 mb-4">
            <div className="bg-orange-950/40 border border-orange-800/60 rounded-lg px-4 py-2.5
                            flex items-center gap-3">
              <svg className="w-4 h-4 text-orange-400 shrink-0" fill="none" stroke="currentColor"
                strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z"
                />
              </svg>
              <p className="text-orange-300 text-xs">
                Showing stale cached data — live fetch failed
                {staleReason ? <>: <span className="text-orange-200">{staleReason}</span></> : "."}{" "}
                <button onClick={handleHardRefresh} className="underline hover:text-orange-200">
                  Try again
                </button>
              </p>
            </div>
          </div>
        )}

        {/* Empty */}
        {((showCatalogData || showBookmarksData) && filteredGroups.length === 0) && (
          <div className="flex flex-col items-center justify-center h-64 gap-3 text-zinc-500">
            {viewMode === "bookmarks" ? (
              <>
                <svg className="w-12 h-12 opacity-20" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round"
                    d="M17.593 3.322c1.1.128 1.907 1.077 1.907 2.185V21L12 17.25 4.5 21V5.507c0-1.108.806-2.057 1.907-2.185a48.507 48.507 0 0111.186 0z" />
                </svg>
                <p className="text-sm">No bookmarks yet</p>
                <p className="text-xs text-zinc-600 max-w-xs text-center">
                  Right-click any title and choose Bookmark to save it here.
                </p>
              </>
            ) : (
              <>
                <svg className="w-12 h-12 opacity-20" fill="none" stroke="currentColor" strokeWidth={1} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round"
                    d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
                </svg>
                <p className="text-sm">
                  {searchQuery ? `No results for "${searchQuery}"` : "No titles found"}
                </p>
                {typeFilter !== "all" && (
                  <button onClick={() => setTypeFilter("all")}
                    className="text-xs text-emerald-400 hover:underline">
                    Clear type filter
                  </button>
                )}
              </>
            )}
          </div>
        )}

        {/* Catalog groups */}
        {filteredGroups.length > 0 && filteredGroups.map((group) => (
          <CatalogGroupRow
            key={group.label}
            group={group}
            onPlay={handleOpenTitle}
            onPlayOnMac={handlePlayOnMac}
            imageCache={imageCache}
            imgPort={imgPort}
            bookmarkedIds={bookmarkedIds}
            onToggleBookmark={handleToggleBookmark}
          />
        ))}
      </main>

      {/* ── Dialogs ─────────────────────────────────────────────────────── */}
      {selectedItem && (
        <PlayDialog
          item={selectedItem}
          config={config}
          initialEpisode={selectedEpisode}
          launchContentId={selectedLaunchContentId}
          bookmarkedIds={bookmarkedIds}
          onToggleBookmark={handleToggleBookmark}
          onClose={() => {
            setSelectedItem(null);
            setSelectedEpisode(null);
            setSelectedLaunchContentId(null);
          }}
          onOpenSettings={() => {
            setSelectedItem(null);
            setSelectedEpisode(null);
            setSelectedLaunchContentId(null);
            setShowSettings(true);
          }}
          onStartPlaying={(item, episode, startSeconds) => {
            // Show in the dock immediately — before the TV command completes
            setNowPlaying(item);
            setNPEpisode(episode);
            setNPStart(startSeconds != null && startSeconds >= 1 ? startSeconds : null);
            setPlaybackState("playing");
          }}
          onPlayed={() => {
            // Command succeeded — close the dialog (nowPlaying already set)
            setSelectedItem(null);
          }}
        />
      )}

      {showSettings && (
        <SettingsDialog
          config={config}
          onClose={() => setShowSettings(false)}
          onSaved={(cfg) => { setConfig(cfg); setShowSettings(false); }}
        />
      )}

      {globalMenu && (
        <ContextMenu
          x={globalMenu.x}
          y={globalMenu.y}
          items={globalMenu.items}
          onClose={() => setGlobalMenu(null)}
        />
      )}

      {/* ── TV Remote — always visible ───────────────────────────────────── */}
      {/* nowPlaying ?? selectedItem: show the selected title in the dock
          as soon as the user opens a title card, even before pressing Play */}
      <TVRemote
        nowPlaying={nowPlaying ?? selectedItem}
        episode={nowPlaying ? nowPlayingEpisode : null}
        initialPositionSeconds={nowPlaying ? nowPlayingStart : null}
        defaultTvVolume={config.default_tv_volume ?? 13}
        playbackState={nowPlaying ? playbackState : "paused"}
        cachedImageSrc={(() => {
          const item = nowPlaying ?? selectedItem;
          if (!item || !imgPort) return undefined;
          const stem = safeId(item.content_id);
          return imageCache.has(stem)
            ? cachedImageHttpUrl(imgPort, item.content_id, item.image_url)
            : undefined;
        })()}
        onPlaybackStateChange={setPlaybackState}
        onDismissPlaying={() => { setNowPlaying(null); setNPEpisode(null); setNPStart(null); }}
      />
    </div>
  );
}

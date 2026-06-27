import { useState, useEffect, useCallback, useRef, useMemo } from "react";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import {
  PrimeTitle,
  AppConfig,
  DEFAULT_CONFIG,
  CatalogGroup,
  EntityTypeFilter,
  groupTitles,
  formatCacheAge,
  isTitleVisible,
} from "./types";
import CatalogGroupRow from "./components/CatalogGroup";
import PlayDialog from "./components/PlayDialog";
import SettingsDialog from "./components/SettingsDialog";
import TVRemote from "./components/TVRemote";
import type { PlaybackState } from "./components/TVRemote";

// ─── Collection definitions ───────────────────────────────────────────────────
const COLLECTIONS = [
  { slug: "IncludedwithPrime", label: "Included with Prime", color: "emerald" },
  { slug: "newandupcoming",    label: "New & Upcoming",      color: "sky"     },
  { slug: "TopRatedMovies",    label: "Top Rated Movies",    color: "amber"   },
] as const;

type CollectionSlug = (typeof COLLECTIONS)[number]["slug"];

type LoadState = "idle" | "loading" | "done" | "error";

// ─── Helpers ──────────────────────────────────────────────────────────────────
function parseResult(raw: string): { data: PrimeTitle[]; stale: boolean } {
  if (raw.startsWith("__STALE__")) {
    try { return { data: JSON.parse(raw.slice(9)), stale: true }; }
    catch { return { data: [], stale: true }; }
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
  const [cacheAgeSecs, setCacheAge] = useState<number | null>(null);

  // Collection selector
  const [collection, setCollection] = useState<CollectionSlug>("IncludedwithPrime");

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
  const [showSettings, setShowSettings] = useState(false);

  // Now-playing (drives the TVRemote bar)
  const [nowPlaying, setNowPlaying]         = useState<PrimeTitle | null>(null);
  const [nowPlayingEpisode, setNPEpisode]   = useState<number | null>(null);
  const [playbackState, setPlaybackState]   = useState<PlaybackState>("playing");

  // ── Config load ─────────────────────────────────────────────────────────────
  useEffect(() => {
    invoke<AppConfig>("get_config")
      .then((cfg) => setConfig(cfg))
      .catch(() => {});
  }, []);



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
        const { data, stale } = parseResult(raw);
        setAllItems(data);
        setIsStale(stale);
        setLoadState("done");

        invoke<number | null>("collection_cache_age", { collection: slug })
          .then((age) => setCacheAge(age ?? null))
          .catch(() => {});

        // Kick off background image prefetch for any unresolved images
        const missing = data.filter(
          (item) => item.image_url && !imageCache.has(item.content_id)
        );
        if (missing.length > 0) {
          invoke("prefetch_images", {
            items: missing.map((i) => ({ content_id: i.content_id, url: i.image_url! })),
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
    loadCatalog(false, collection);
    // Reset type filter when switching collections
    setTypeFilter("all");
  }, [collection]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Debounced search ────────────────────────────────────────────────────────
  const handleSearchChange = (q: string) => {
    setSearchQuery(q);
    if (searchTimerRef.current) clearTimeout(searchTimerRef.current);

    if (!q.trim()) {
      loadCatalog(false, collection);
      return;
    }

    searchTimerRef.current = setTimeout(async () => {
      setSearching(true);
      setError(null);
      setIsStale(false);

      invoke<number | null>("search_cache_age", { query: q.trim() })
        .then((age) => setCacheAge(age ?? null))
        .catch(() => setCacheAge(null));

      try {
        const raw = await invoke<string>("search_catalog", {
          query: q.trim(),
          forceRefresh: false,
        });
        const { data, stale } = parseResult(raw);
        setAllItems(data);
        setIsStale(stale);
        setLoadState("done");

        // Prefetch images for search results too
        const missing = data.filter(
          (item) => item.image_url && !imageCache.has(item.content_id)
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
      setCacheAge(null);
      try {
        const raw = await invoke<string>("search_catalog", {
          query: searchQuery.trim(),
          forceRefresh: true,
        });
        const { data } = parseResult(raw);
        setAllItems(data);
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
  const filteredGroups = useMemo<CatalogGroup[]>(() => {
    const items = allItems.filter((item) => {
      if (typeFilter === "Movie"   && item.entity_type !== "Movie")   return false;
      if (typeFilter === "TV Show" && item.entity_type !== "TV Show") return false;
      return isTitleVisible(item, config);
    });
    return groupTitles(items).filter((g) => g.items.length > 0);
  }, [allItems, typeFilter, config]);

  const totalFiltered = filteredGroups.reduce((s, g) => s + g.items.length, 0);
  const movieCount    = allItems.filter((i) => i.entity_type === "Movie").length;
  const showCount     = allItems.filter((i) => i.entity_type === "TV Show").length;

  const isLoading = loadState === "loading" || searching;
  const activeCollection = COLLECTIONS.find((c) => c.slug === collection)!;

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
            {cacheAgeSecs !== null && loadState === "done" && !searchQuery && (
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

            {/* TV chip */}
            <div className="hidden sm:flex items-center gap-1.5 bg-zinc-800/60 rounded-full px-3 py-1">
              <div className="w-2 h-2 rounded-full bg-emerald-500" />
              <span className="text-xs text-zinc-400 font-mono">{config.tv_ip}</span>
            </div>

            {/* Soft reload */}
            <button onClick={() => loadCatalog(false, collection)} disabled={isLoading}
              title="Reload from cache"
              className="p-1.5 text-zinc-500 hover:text-white hover:bg-zinc-700/60 rounded-lg
                         transition-colors disabled:opacity-30">
              <svg className={`w-4 h-4 ${loadState === "loading" && !searching ? "animate-spin" : ""}`}
                fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round"
                  d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
              </svg>
            </button>

            {/* Hard refresh */}
            <button onClick={handleHardRefresh} disabled={isLoading}
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
          <div className="flex items-center gap-1 px-6 pt-0.5 pb-2">
            {COLLECTIONS.map((col) => {
              const active = col.slug === collection;
              const colorMap: Record<string, string> = {
                emerald: active ? "bg-emerald-600 text-white" : "text-emerald-400/70 hover:text-emerald-300",
                sky:     active ? "bg-sky-600 text-white"     : "text-sky-400/70 hover:text-sky-300",
                amber:   active ? "bg-amber-600 text-white"   : "text-amber-400/70 hover:text-amber-300",
              };
              return (
                <button
                  key={col.slug}
                  onClick={() => {
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
            {loadState === "done" && allItems.length > 0 && (
              <span className="ml-2 text-xs text-zinc-600">
                {allItems.length} titles
              </span>
            )}
          </div>
        )}

        {/* Entity-type filter + count row (after data loads) */}
        {loadState === "done" && allItems.length > 0 && (
          <div className="flex items-center gap-1 px-6 pb-2.5">
            {(
              [
                { key: "all",     label: "All",      count: allItems.length },
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
        {loadState === "error" && error && (
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
        {isStale && loadState === "done" && (
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
                Showing stale cached data — live fetch failed.{" "}
                <button onClick={handleHardRefresh} className="underline hover:text-orange-200">
                  Try again
                </button>
              </p>
            </div>
          </div>
        )}

        {/* Empty */}
        {loadState === "done" && filteredGroups.length === 0 && (
          <div className="flex flex-col items-center justify-center h-64 gap-3 text-zinc-500">
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
          </div>
        )}

        {/* Catalog groups */}
        {filteredGroups.length > 0 && filteredGroups.map((group) => (
          <CatalogGroupRow
            key={group.label}
            group={group}
            onPlay={setSelectedItem}
            imageCache={imageCache}
            imgPort={imgPort}
          />
        ))}
      </main>

      {/* ── Dialogs ─────────────────────────────────────────────────────── */}
      {selectedItem && (
        <PlayDialog
          item={selectedItem}
          config={config}
          onClose={() => setSelectedItem(null)}
          onOpenSettings={() => { setSelectedItem(null); setShowSettings(true); }}
          onStartPlaying={(item, episode) => {
            // Show in the dock immediately — before the TV command completes
            setNowPlaying(item);
            setNPEpisode(episode);
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

      {/* ── TV Remote — always visible ───────────────────────────────────── */}
      {/* nowPlaying ?? selectedItem: show the selected title in the dock
          as soon as the user opens a title card, even before pressing Play */}
      <TVRemote
        nowPlaying={nowPlaying ?? selectedItem}
        episode={nowPlaying ? nowPlayingEpisode : null}
        playbackState={nowPlaying ? playbackState : "paused"}
        cachedImageSrc={(() => {
          const item = nowPlaying ?? selectedItem;
          if (!item || !imgPort) return undefined;
          const stem = item.content_id.replace(/[^\w\-]/g, "_");
          return imageCache.has(stem)
            ? `http://127.0.0.1:${imgPort}/${stem}.jpg`
            : undefined;
        })()}
        onPlaybackStateChange={setPlaybackState}
        onDismissPlaying={() => { setNowPlaying(null); setNPEpisode(null); }}
      />
    </div>
  );
}

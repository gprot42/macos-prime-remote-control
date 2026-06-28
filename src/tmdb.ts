import { invoke } from "@tauri-apps/api/core";
import type { PrimeEpisode, PrimeTitle } from "./types";

export type TmdbMediaKind = "movie" | "tv" | "multi";

const TMDB_BASE = "https://www.themoviedb.org";

export function tmdbKind(entityType: string | null | undefined): TmdbMediaKind {
  if (entityType === "Movie") return "movie";
  if (entityType === "TV Show" || entityType === "TV Episode") return "tv";
  return "multi";
}

/** Strip Prime season suffixes so TMDB matches the series/film name. */
export function cleanTitleForTmdb(title: string): string {
  let cleaned = title.trim();
  cleaned = cleaned.replace(/\s*:\s*Season\s+\d+.*$/i, "");
  cleaned = cleaned.replace(/\s*-\s*Season\s+\d+.*$/i, "");
  cleaned = cleaned.replace(/\s*\(Season\s+\d+.*?\)\s*$/i, "");
  cleaned = cleaned.replace(/\s+Season\s+\d+.*$/i, "");
  return cleaned.trim();
}

function shouldUseYearFilter(
  kind: TmdbMediaKind,
  year: number | null | undefined,
): boolean {
  if (!year || year < 1900) return false;
  const currentYear = new Date().getFullYear();
  if (year > currentYear) return false;
  // Prime often lists a catalogue refresh year for TV — it breaks TMDB search.
  if (kind === "tv") return false;
  return true;
}

export function buildTmdbQuery(
  title: string,
  options?: {
    year?: number | null;
    kind?: TmdbMediaKind;
    episodeTitle?: string | null;
    episodeNumber?: number | null;
  },
): string {
  const kind = options?.kind ?? "multi";
  const parts = [cleanTitleForTmdb(title)];

  const episodeTitle = options?.episodeTitle?.trim();
  if (episodeTitle && !/^episode\s+\d+$/i.test(episodeTitle)) {
    parts.push(episodeTitle);
  } else if (options?.episodeNumber != null && options.episodeNumber > 0) {
    parts.push(String(options.episodeNumber));
  }

  if (shouldUseYearFilter(kind, options?.year ?? null)) {
    parts.push(`y:${options!.year}`);
  }

  return parts.join(" ").trim();
}

export function tmdbSearchUrl(
  query: string,
  kind: TmdbMediaKind = "multi",
): string {
  const path =
    kind === "movie" ? "search/movie" : kind === "tv" ? "search/tv" : "search";
  const params = new URLSearchParams({
    query: query.trim(),
    language: "en-US",
  });
  return `${TMDB_BASE}/${path}?${params.toString()}`;
}

/** TMDB search URL for a film or series card. */
export function tmdbUrlForTitle(item: PrimeTitle): string {
  const kind = tmdbKind(item.entity_type);
  const query = buildTmdbQuery(item.title, { year: item.year, kind });
  return tmdbSearchUrl(query, kind);
}

/** TMDB search URL for a specific episode (series title + episode label). */
export function tmdbUrlForEpisode(
  series: PrimeTitle,
  episode: PrimeEpisode,
  episodeIndex: number,
): string {
  const epNum = episode.sequence_number ?? episodeIndex + 1;
  const query = buildTmdbQuery(series.title, {
    kind: "tv",
    episodeTitle: episode.title,
    episodeNumber: epNum,
  });
  return tmdbSearchUrl(query, "tv");
}

export async function openExternalUrl(url: string): Promise<void> {
  try {
    await invoke("open_external_url", { url });
  } catch {
    // Dev browser / older app builds without the Rust command.
    window.open(url, "_blank", "noopener,noreferrer");
  }
}

export async function openTmdbLookup(url: string): Promise<void> {
  await openExternalUrl(url);
}
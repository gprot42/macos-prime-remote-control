import { Bookmark, CatalogGroup, PrimeTitle, PrimeEpisode } from "./types";

export function isMovieBookmark(item: PrimeTitle): boolean {
  return item.entity_type === "Movie";
}

export function isTvBookmark(item: PrimeTitle): boolean {
  return item.entity_type === "TV Show" || item.entity_type === "TV Episode";
}

/** Split bookmark items into Movies and TV Series carousel rows. */
export function groupBookmarks(items: PrimeTitle[]): CatalogGroup[] {
  const movies: PrimeTitle[] = [];
  const tv: PrimeTitle[] = [];
  const other: PrimeTitle[] = [];
  for (const item of items) {
    if (isMovieBookmark(item)) movies.push(item);
    else if (isTvBookmark(item)) tv.push(item);
    else other.push(item);
  }
  const groups: CatalogGroup[] = [];
  if (movies.length > 0) groups.push({ label: "Movies", items: movies });
  if (tv.length > 0) groups.push({ label: "TV Series", items: tv });
  if (other.length > 0) groups.push({ label: "Other", items: other });
  return groups;
}

/** Stable bookmark key for a title or a specific episode. */
export function bookmarkId(item: PrimeTitle, episode?: PrimeEpisode | null): string {
  if (episode?.content_id) return `episode:${episode.content_id}`;
  return item.content_id;
}

/** Prime detail ID for a bookmarked episode snapshot (`episode:<id>`), if any. */
export function episodeBookmarkContentId(item: PrimeTitle): string | null {
  const prefix = "episode:";
  if (!item.content_id.startsWith(prefix)) return null;
  const id = item.content_id.slice(prefix.length);
  return id.length > 0 ? id : null;
}

export function findBookmark(
  bookmarks: Bookmark[],
  item: PrimeTitle,
): Bookmark | undefined {
  return bookmarks.find((b) => b.item.content_id === item.content_id);
}

export function isEpisodeBookmark(bookmark: Bookmark): boolean {
  return (
    bookmark.episode_content_id != null
    || episodeBookmarkContentId(bookmark.item) != null
  );
}

export function resolveEpisodePlayId(bookmark: Bookmark): string | null {
  return bookmark.episode_content_id ?? episodeBookmarkContentId(bookmark.item);
}

/** Display snapshot stored in the bookmarks list. */
export function bookmarkSnapshot(
  item: PrimeTitle,
  episode?: PrimeEpisode | null,
  episodeIndex?: number,
): PrimeTitle {
  if (!episode) return item;
  const num = episode.sequence_number ?? (episodeIndex != null ? episodeIndex + 1 : undefined);
  const epName = episode.title || (num != null ? `Episode ${num}` : "Episode");
  const title =
    num != null ? `${item.title} · E${num}: ${epName}` : `${item.title} · ${epName}`;
  return {
    ...item,
    content_id: bookmarkId(item, episode),
    title,
    runtime_min: episode.runtime_min ?? item.runtime_min,
    entity_type: "TV Episode",
  };
}

export function isBookmarked(
  bookmarkedIds: Set<string>,
  item: PrimeTitle,
  episode?: PrimeEpisode | null,
): boolean {
  return bookmarkedIds.has(bookmarkId(item, episode));
}
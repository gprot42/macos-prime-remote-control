import { useEffect, useState } from "react";
import { PrimeTitle, getAccessLabel, accessBadgeStyle } from "../types";
import { ContextMenuItem } from "./ContextMenu";
import { showMediaContextMenu } from "../contextMenuBus";
import { BookmarkIcon, ExternalLinkIcon, LaptopIcon, PlayIcon, TrailerIcon } from "./BookmarkMenuIcons";
import { openTmdbLookup, openTmdbTrailerForTitle, tmdbUrlForTitle } from "../tmdb";

interface TitleCardProps {
  item: PrimeTitle;
  onPlay: (item: PrimeTitle) => void;
  onPlayOnMac?: (item: PrimeTitle) => void;
  cachedImageSrc?: string;
  isBookmarked?: boolean;
  onToggleBookmark?: (item: PrimeTitle) => void;
}

export default function TitleCard({
  item,
  onPlay,
  onPlayOnMac,
  cachedImageSrc,
  isBookmarked = false,
  onToggleBookmark,
}: TitleCardProps) {
  const amazonUrl = item.image_url
    ? item.image_url.replace(/\._UR\d+,\d+_\./, "._UR960,540_.")
    : null;

  const [src, setSrc] = useState<string | null>(cachedImageSrc || amazonUrl);
  const [imgError, setImgError] = useState(false);

  useEffect(() => {
    setImgError(false);
    setSrc(cachedImageSrc || amazonUrl);
  }, [cachedImageSrc, amazonUrl, item.content_id]);

  const label = getAccessLabel(item);
  const badgeStyle = accessBadgeStyle(label);

  const menuItems: ContextMenuItem[] = [
    {
      label: "Play on TV",
      icon: <PlayIcon />,
      onClick: () => onPlay(item),
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
  if (onPlayOnMac) {
    menuItems.splice(1, 0, {
      label: "Play on Mac",
      icon: <LaptopIcon />,
      onClick: () => onPlayOnMac(item),
    });
  }
  if (onToggleBookmark) {
    menuItems.push({
      label: isBookmarked ? "Remove bookmark" : "Bookmark",
      icon: <BookmarkIcon filled={isBookmarked} />,
      onClick: () => onToggleBookmark(item),
      destructive: isBookmarked,
    });
  }

  const openMenu = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    window.getSelection()?.removeAllRanges();
    showMediaContextMenu({ x: e.clientX, y: e.clientY, items: menuItems });
  };

  const handleImageError = () => {
    if (src && src === cachedImageSrc && amazonUrl) {
      setSrc(amazonUrl);
      return;
    }
    setImgError(true);
  };

  return (
    <>
      <div
        data-media-card
        className="relative flex-shrink-0 w-64 cursor-pointer group select-none"
        style={{ aspectRatio: "16/9" }}
        onClick={() => onPlay(item)}
        onContextMenu={openMenu}
        onMouseDown={(e) => {
          if (e.button === 2) window.getSelection()?.removeAllRanges();
        }}
      >
        <div className="absolute inset-0 rounded-lg overflow-hidden bg-[#1A242F]">
          {src && !imgError ? (
            <img
              src={src}
              alt={item.title}
              className="w-full h-full object-cover pointer-events-none"
              onError={handleImageError}
              loading="lazy"
            />
          ) : (
            <div className="w-full h-full flex items-center justify-center bg-gradient-to-br from-[#1A242F] to-[#0F171E]">
              <div className="text-center p-3">
                <div className="text-4xl mb-2 opacity-30">🎬</div>
                <p className="text-xs text-zinc-500 line-clamp-2">{item.title}</p>
              </div>
            </div>
          )}

          <div className="absolute inset-0 bg-gradient-to-t from-black/90 via-black/20 to-transparent pointer-events-none" />

          {onToggleBookmark && (
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onToggleBookmark(item);
              }}
              onContextMenu={openMenu}
              title={isBookmarked ? "Remove bookmark" : "Bookmark"}
              className={`absolute top-2 left-2 z-10 flex items-center justify-center w-7 h-7 rounded-md
                          transition-all shadow ${
                isBookmarked
                  ? "bg-amber-500 text-white opacity-100"
                  : "bg-black/60 text-zinc-200 opacity-0 group-hover:opacity-100 hover:bg-amber-500 hover:text-white"
              }`}
            >
              <BookmarkIcon filled={isBookmarked} />
            </button>
          )}

          <div className="absolute inset-x-0 bottom-0 p-2 pointer-events-none">
            <p className="text-white text-xs font-semibold leading-tight line-clamp-2 mb-1 drop-shadow">
              {item.title}
            </p>
            <div className="flex items-center gap-1 flex-wrap">
              {item.entity_type && (
                <span className="text-[10px] text-zinc-400 bg-zinc-800/80 px-1.5 py-0.5 rounded">
                  {item.entity_type}
                </span>
              )}
              {item.year && (
                <span className="text-[10px] text-zinc-400">{item.year}</span>
              )}
              {item.runtime_str && (
                <span className="text-[10px] text-zinc-500">{item.runtime_str}</span>
              )}
              {label !== "-" && (
                <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ml-auto ${badgeStyle}`}>
                  {label}
                </span>
              )}
            </div>
          </div>

          {(item.included_with_prime || item.prime_catalog) && (
            <div className="absolute top-2 right-2 pointer-events-none">
              <span className="text-[10px] bg-emerald-600/90 text-white px-1.5 py-0.5 rounded font-medium">
                Prime
              </span>
            </div>
          )}
        </div>
      </div>

    </>
  );
}
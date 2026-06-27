import { useState } from "react";
import { PrimeTitle, getAccessLabel, accessBadgeStyle } from "../types";

interface TitleCardProps {
  item: PrimeTitle;
  onPlay: (item: PrimeTitle) => void;
  /** Local asset:// URL for the cached image (takes priority over remote URL). */
  cachedImageSrc?: string;
}

export default function TitleCard({ item, onPlay, cachedImageSrc }: TitleCardProps) {
  const [imgError, setImgError] = useState(false);
  const [hovered, setHovered] = useState(false);
  const label = getAccessLabel(item);
  const badgeStyle = accessBadgeStyle(label);

  // Prefer locally cached image; fall back to remote CDN at card-friendly size.
  const imageUrl =
    cachedImageSrc ||
    (item.image_url
      ? item.image_url.replace(/\._UR\d+,\d+_\./, "._UR960,540_.")
      : null);

  // Title logo: keep smaller
  const logoUrl = item.title_logo_url
    ? item.title_logo_url.replace(/\._UR\d+,\d+_\./, "._UR480,270_.")
    : null;

  return (
    <div
      className="relative flex-shrink-0 w-64 cursor-pointer group"
      style={{ aspectRatio: "16/9" }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onClick={() => onPlay(item)}
    >
      {/* Card background */}
      <div className="absolute inset-0 rounded-lg overflow-hidden bg-[#1A242F]">
        {/* Hero image */}
        {imageUrl && !imgError ? (
          <img
            src={imageUrl}
            alt={item.title}
            className="w-full h-full object-cover"
            onError={() => setImgError(true)}
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

        {/* Gradient overlay - always visible at bottom */}
        <div className="absolute inset-0 bg-gradient-to-t from-black/90 via-black/20 to-transparent" />

        {/* Title logo (shown when not hovered, when available) */}
        {logoUrl && !hovered && (
          <div className="absolute inset-x-0 bottom-8 flex justify-center px-3">
            <img
              src={logoUrl}
              alt={`${item.title} logo`}
              className="max-h-10 max-w-full object-contain drop-shadow-md"
              onError={() => {/* ignore logo error */}}
            />
          </div>
        )}

        {/* Bottom info bar */}
        <div className="absolute inset-x-0 bottom-0 p-2">
          {(!logoUrl || hovered) && (
            <p className="text-white text-xs font-semibold leading-tight line-clamp-2 mb-1 drop-shadow">
              {item.title}
            </p>
          )}
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

        {/* Hover overlay */}
        {hovered && (
          <div className="absolute inset-0 bg-black/70 flex flex-col justify-between p-3 rounded-lg transition-opacity duration-150">
            {/* Synopsis */}
            {item.synopsis && (
              <p className="text-zinc-300 text-[11px] line-clamp-3 leading-relaxed mt-6">
                {item.synopsis}
              </p>
            )}
            {/* Availability detail */}
            {item.availability && (
              <p className="text-[10px] text-zinc-400 line-clamp-2 mt-1">
                {item.availability}
              </p>
            )}
            {/* Play button */}
            <button
              className="mt-2 w-full bg-[#00A8E1] hover:bg-[#0090c0] text-white text-sm font-semibold py-2 px-3 rounded-md flex items-center justify-center gap-2 transition-colors"
              onClick={(e) => {
                e.stopPropagation();
                onPlay(item);
              }}
            >
              <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                <path d="M6.3 2.841A1.5 1.5 0 004 4.11V15.89a1.5 1.5 0 002.3 1.269l9.344-5.89a1.5 1.5 0 000-2.538L6.3 2.84z" />
              </svg>
              Play on TV
            </button>
          </div>
        )}

        {/* Prime badge top-right (green = included with subscription) */}
        {(item.included_with_prime || item.prime_catalog) && (
          <div className="absolute top-2 right-2">
            <span className="text-[10px] bg-emerald-600/90 text-white px-1.5 py-0.5 rounded font-medium">
              Prime
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

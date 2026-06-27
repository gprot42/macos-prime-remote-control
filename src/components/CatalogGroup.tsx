import { useRef } from "react";
import { CatalogGroup as CatalogGroupType, PrimeTitle } from "../types";
import TitleCard from "./TitleCard";

interface CatalogGroupProps {
  group: CatalogGroupType;
  onPlay: (item: PrimeTitle) => void;
  imageCache: Set<string>;
  imgPort: number;
}

function safeId(id: string) { return id.replace(/[^\w\-]/g, "_"); }

export default function CatalogGroupRow({ group, onPlay, imageCache, imgPort }: CatalogGroupProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  const scrollLeft = () => {
    scrollRef.current?.scrollBy({ left: -560, behavior: "smooth" });
  };
  const scrollRight = () => {
    scrollRef.current?.scrollBy({ left: 560, behavior: "smooth" });
  };

  return (
    <div className="mb-8">
      {/* Group header */}
      <h2 className="text-white font-semibold text-base mb-3 px-6 flex items-center gap-2">
        <span className="w-1 h-5 bg-[#00A8E1] rounded-full inline-block" />
        {group.label}
        <span className="text-zinc-600 text-sm font-normal ml-1">
          ({group.items.length})
        </span>
      </h2>

      {/* Scrollable row */}
      <div className="relative group/row">
        {/* Left scroll button */}
        <button
          onClick={scrollLeft}
          className="absolute left-0 top-0 bottom-0 z-10 w-10 flex items-center justify-center
                     bg-gradient-to-r from-[#0F171E] to-transparent opacity-0 group-hover/row:opacity-100
                     transition-opacity duration-200 text-white hover:text-[#00A8E1]"
          aria-label="Scroll left"
        >
          <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
          </svg>
        </button>

        {/* Cards container */}
        <div
          ref={scrollRef}
          className="flex gap-3 overflow-x-auto px-6 pb-2"
          style={{ scrollbarWidth: "thin", scrollbarColor: "#2a3a4a #0f171e" }}
        >
          {group.items.map((item) => {
            const stem = safeId(item.content_id);
            const cachedSrc =
              imgPort && imageCache.has(stem)
                ? `http://127.0.0.1:${imgPort}/${stem}.jpg`
                : undefined;
            return (
              <TitleCard
                key={item.content_id}
                item={item}
                onPlay={onPlay}
                cachedImageSrc={cachedSrc}
              />
            );
          })}
        </div>

        {/* Right scroll button */}
        <button
          onClick={scrollRight}
          className="absolute right-0 top-0 bottom-0 z-10 w-10 flex items-center justify-center
                     bg-gradient-to-l from-[#0F171E] to-transparent opacity-0 group-hover/row:opacity-100
                     transition-opacity duration-200 text-white hover:text-[#00A8E1]"
          aria-label="Scroll right"
        >
          <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M9 5l7 7-7 7" />
          </svg>
        </button>
      </div>
    </div>
  );
}

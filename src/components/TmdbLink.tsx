import { openTmdbLookup } from "../tmdb";
import { ExternalLinkIcon } from "./BookmarkMenuIcons";

interface TmdbLinkProps {
  url: string;
  label?: string;
  className?: string;
  variant?: "link" | "button" | "icon";
}

export default function TmdbLink({
  url,
  label = "TMDB",
  className = "",
  variant = "link",
}: TmdbLinkProps) {
  const onClick = () => void openTmdbLookup(url);

  if (variant === "icon") {
    return (
      <button
        type="button"
        onClick={onClick}
        title="Look up on TMDB"
        className={`flex items-center justify-center w-7 h-7 rounded-md
                    bg-sky-600/90 text-white text-[10px] font-bold shadow
                    hover:bg-sky-500 transition-colors ${className}`}
      >
        T
      </button>
    );
  }

  if (variant === "button") {
    return (
      <button
        type="button"
        onClick={onClick}
        title="Open TMDB search in your browser"
        className={`inline-flex items-center justify-center gap-1.5 px-3 py-2 rounded-md
                    bg-sky-700/80 hover:bg-sky-600 text-white text-sm font-medium
                    transition-colors ${className}`}
      >
        <span>{label}</span>
        <ExternalLinkIcon className="w-3.5 h-3.5 opacity-90" />
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 text-sky-400 hover:text-sky-300
                  text-xs transition-colors ${className}`}
      title="Open TMDB search in your browser"
    >
      <span>{label}</span>
      <ExternalLinkIcon className="w-3.5 h-3.5 opacity-80" />
    </button>
  );
}
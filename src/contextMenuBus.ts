import type { ContextMenuItem } from "./components/ContextMenu";

export const MEDIA_CONTEXT_MENU_EVENT = "media-context-menu";

export interface MediaContextMenuDetail {
  x: number;
  y: number;
  items: ContextMenuItem[];
}

export function showMediaContextMenu(detail: MediaContextMenuDetail) {
  window.dispatchEvent(
    new CustomEvent<MediaContextMenuDetail>(MEDIA_CONTEXT_MENU_EVENT, { detail })
  );
}
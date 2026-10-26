import { createContext } from "react";

/**
 * DOM node of the content top-bar into which a page's PageHeader portals its
 * title + actions. `null` when no slot is mounted, in which case PageHeader
 * falls back to rendering inline. Provided by the app shell (Layout).
 */
export const PageSlotContext = createContext<HTMLElement | null>(null);

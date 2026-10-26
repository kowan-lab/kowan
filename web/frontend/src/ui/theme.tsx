import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { flushSync } from "react-dom";

/**
 * Theming: a palette (Kowan, Graphite, Nord, Dracula, Catppuccin, Gruvbox, Tokyo Night)
 * times a theme (light / dark). Both are attributes on <html> (data-palette,
 * data-theme), so every token in styles/themes.css, and every portal that
 * inherits from <html>, flips at once. The mode is the choice, persisted in
 * localStorage: light, dark, or auto (the default), which follows the OS and
 * switches when the OS does.
 */
export type Theme = "light" | "dark";
export type Mode = Theme | "auto";
export type Palette = "kowan" | "graphite" | "nord" | "dracula" | "catppuccin" | "gruvbox" | "tokyonight";

/** Display data for the picker: name, origin and four swatches per mode. */
export const PALETTES: { id: Palette; label: string; note: string; swatches: Record<Theme, string[]> }[] = [
  { id: "kowan", label: "Kowan", note: "Washi paper, sumi ink, signal orange", swatches: { light: ["#f4f1ea", "#fbfaf6", "#1b1a17", "#fa3600"], dark: ["#121110", "#1a1917", "#ece6da", "#ff5a1f"] } },
  { id: "graphite", label: "Graphite", note: "Neutral graphite, blueprint blue", swatches: { light: ["#f1f1f2", "#ffffff", "#0a0a0a", "#1f5cff"], dark: ["#070707", "#101010", "#ededed", "#5b8cff"] } },
  { id: "nord", label: "Nord", note: "Arctic, north-bluish", swatches: { light: ["#e5e9f0", "#f8f9fb", "#2e3440", "#5e81ac"], dark: ["#242933", "#2e3440", "#eceff4", "#88c0d0"] } },
  { id: "dracula", label: "Dracula", note: "Dark with vivid accents (Alucard in light)", swatches: { light: ["#ecebe2", "#fbfbf6", "#1f1f1f", "#644ac9"], dark: ["#1e1f29", "#282a36", "#f8f8f2", "#bd93f9"] } },
  { id: "catppuccin", label: "Catppuccin", note: "Latte and Mocha, soothing pastels", swatches: { light: ["#e6e9ef", "#f5f6f9", "#4c4f69", "#8839ef"], dark: ["#181825", "#1e1e2e", "#cdd6f4", "#cba6f7"] } },
  { id: "gruvbox", label: "Gruvbox", note: "Retro, warm, high legibility", swatches: { light: ["#f2e5bc", "#fbf1c7", "#3c3836", "#076678"], dark: ["#1d2021", "#282828", "#ebdbb2", "#83a598"] } },
  { id: "tokyonight", label: "Tokyo Night", note: "Night and Day, neon city", swatches: { light: ["#d5d6db", "#e9e9ed", "#343b58", "#2e7de9"], dark: ["#16161e", "#1a1b26", "#c0caf5", "#7aa2f7"] } },
];

const MODE_KEY = "kowan-theme";
const PALETTE_KEY = "kowan-palette";

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
function write(key: string, v: string) {
  try {
    localStorage.setItem(key, v);
  } catch {
    /* private mode */
  }
}

const DARK_QUERY = "(prefers-color-scheme: dark)";

function systemTheme(): Theme {
  return typeof window !== "undefined" && window.matchMedia?.(DARK_QUERY).matches ? "dark" : "light";
}
function initialMode(): Mode {
  const saved = read(MODE_KEY);
  return saved === "light" || saved === "dark" ? saved : "auto";
}
function resolve(mode: Mode, system: Theme): Theme {
  return mode === "auto" ? system : mode;
}
function initialPalette(): Palette {
  const saved = read(PALETTE_KEY) as Palette | null;
  return saved && PALETTES.some((p) => p.id === saved) ? saved : "kowan";
}

function apply(theme: Theme, palette: Palette) {
  const el = document.documentElement;
  el.setAttribute("data-theme", theme);
  el.setAttribute("data-palette", palette);
  // The browser and installed-app chrome take the page's ground colour.
  const ground = getComputedStyle(el).getPropertyValue("--background").trim();
  if (ground) {
    for (const meta of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')) {
      meta.content = ground;
      meta.removeAttribute("media");
    }
  }
}

// Apply before React paints, so there is no flash of the wrong theme.
apply(resolve(initialMode(), systemTheme()), initialPalette());

/** Run a DOM-changing update as a quick cross-fade when the browser supports it. */
function withTransition(update: () => void) {
  const doc = document as Document & { startViewTransition?: (cb: () => void) => unknown };
  const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  if (doc.startViewTransition && !reduce) doc.startViewTransition(() => flushSync(update));
  else update();
}

interface ThemeContextValue {
  /** What is shown: the mode, with auto resolved against the OS. */
  theme: Theme;
  /** What was chosen. */
  mode: Mode;
  palette: Palette;
  /** Switch to the opposite of what is shown (leaving auto). */
  toggle: () => void;
  setMode: (m: Mode) => void;
  setPalette: (p: Palette) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setModeState] = useState<Mode>(initialMode);
  const [system, setSystem] = useState<Theme>(systemTheme);
  const [palette, setPaletteState] = useState<Palette>(initialPalette);
  const theme = resolve(mode, system);

  // Follow the OS while auto is chosen (and keep `system` current for later).
  useEffect(() => {
    const mq = window.matchMedia?.(DARK_QUERY);
    if (!mq) return;
    const onChange = () => withTransition(() => setSystem(mq.matches ? "dark" : "light"));
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    apply(theme, palette);
    write(MODE_KEY, mode);
    write(PALETTE_KEY, palette);
  }, [theme, mode, palette]);

  const setMode = useCallback((m: Mode) => withTransition(() => setModeState(m)), []);
  const setPalette = useCallback((p: Palette) => withTransition(() => setPaletteState(p)), []);
  const toggle = useCallback(() => withTransition(() => setModeState(theme === "dark" ? "light" : "dark")), [theme]);

  const value = useMemo(() => ({ theme, mode, palette, toggle, setMode, setPalette }), [theme, mode, palette, toggle, setMode, setPalette]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within <ThemeProvider>");
  return ctx;
}

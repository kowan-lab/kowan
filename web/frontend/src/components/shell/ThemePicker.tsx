import { Check, Moon, Palette as PaletteIcon, Sun } from "lucide-react";
import { Popover as Pop } from "radix-ui";
import { cn } from "@/lib/utils";
import { Tooltip } from "@/ui/kit";
import { PALETTES, useTheme } from "@/ui/theme";

/** Mode switch plus the palette list. Used in the top bar popover and in Settings. */
export function ThemePanel() {
  const { theme, setTheme, palette, setPalette } = useTheme();
  return (
    <div className="flex flex-col gap-5">
      <div role="radiogroup" aria-label="Mode" className="grid grid-cols-2 bg-[var(--popover)] shadow-[inset_0_0_0_1px_var(--border)]">
        {(["light", "dark"] as const).map((m) => (
          <button
            key={m}
            type="button"
            role="radio"
            aria-checked={theme === m}
            onClick={() => setTheme(m)}
            className={cn(
              "flex h-16 items-center justify-center gap-3 text-[13px] font-semibold transition-colors duration-100 outline-none focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal",
              theme === m ? "bg-foreground text-background" : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            {m === "light" ? <Sun className="size-[12px]" /> : <Moon className="size-[12px]" />}
            {m === "light" ? "Light" : "Dark"}
          </button>
        ))}
      </div>
      <ul role="radiogroup" aria-label="Palette" className="m-0 flex list-none flex-col p-0">
        {PALETTES.map((p) => {
          const on = palette === p.id;
          return (
            <li key={p.id}>
              <button
                type="button"
                role="radio"
                aria-checked={on}
                onClick={() => setPalette(p.id)}
                className={cn(
                  "flex w-full items-center gap-5 px-3 py-3 text-left transition-colors duration-100 outline-none hover:bg-accent focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal",
                  on && "bg-accent",
                )}
              >
                <span aria-hidden className="flex shrink-0 border">
                  {p.swatches[theme].map((c, i) => (
                    <span key={i} className="size-6" style={{ background: c }} />
                  ))}
                </span>
                <span className="flex min-w-0 flex-1 flex-col leading-[16px]">
                  <span className="text-[13px] font-medium">{p.label}</span>
                  <span className="truncate text-[12px] text-muted-foreground">{p.note}</span>
                </span>
                {on && <Check className="size-[12px] shrink-0" />}
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** Top-bar button that opens the theme panel. */
export default function ThemePicker() {
  return (
    <Pop.Root>
      <Tooltip content="Theme">
        <Pop.Trigger
          aria-label="Theme"
          className="flex size-12 items-center justify-center text-muted-foreground outline-none transition-colors duration-100 hover:bg-accent hover:text-foreground focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal data-[state=open]:bg-accent data-[state=open]:text-foreground"
        >
          <PaletteIcon className="size-6" />
        </Pop.Trigger>
      </Tooltip>
      <Pop.Portal>
        <Pop.Content align="end" sideOffset={6} className="anim-pop z-50 w-108 border bg-popover p-5 text-popover-foreground shadow-[0_12px_32px_-8px_rgb(0_0_0/0.35)]">
          <p className="m-0 mb-[8px] text-[12px] font-medium tracking-[0.06em] text-muted-foreground font-mono uppercase">Theme</p>
          <ThemePanel />
        </Pop.Content>
      </Pop.Portal>
    </Pop.Root>
  );
}

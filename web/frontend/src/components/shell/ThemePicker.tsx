import { Check, Monitor, Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";
import { PALETTES, useTheme } from "@/ui/theme";

const MODES = [
  { id: "light", label: "Light", Icon: Sun },
  { id: "dark", label: "Dark", Icon: Moon },
  { id: "auto", label: "Auto", Icon: Monitor },
] as const;

/** Mode switch plus the palette list. Used in the top bar popover and in Settings. */
export function ThemePanel() {
  const { theme, mode, setMode, palette, setPalette } = useTheme();
  return (
    <div className="flex flex-col gap-5">
      <div role="radiogroup" aria-label="Mode" className="grid grid-cols-3 bg-[var(--popover)] shadow-[inset_0_0_0_1px_var(--border)]">
        {MODES.map(({ id, label, Icon }) => (
          <button
            key={id}
            type="button"
            role="radio"
            aria-checked={mode === id}
            title={id === "auto" ? "Follow the system" : undefined}
            onClick={() => setMode(id)}
            className={cn(
              "flex h-16 items-center justify-center gap-3 text-[13px] font-semibold transition-colors duration-100 outline-none focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal",
              mode === id ? "bg-foreground text-background" : "text-muted-foreground hover:bg-accent hover:text-foreground",
            )}
          >
            <Icon className="size-[12px]" />
            {label}
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

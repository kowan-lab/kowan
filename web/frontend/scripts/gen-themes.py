#!/usr/bin/env python3
"""Generate src/styles/themes.css from the palette table below.

Each theme has a light and/or dark variant. A variant sets the surface ramp
(page background < surface < raised), text, hairlines, the signal accent and
the state tones. Run: python3 scripts/gen-themes.py
"""
from pathlib import Path
from urllib.parse import quote

TONES = ["gray", "blue", "green", "amber", "red", "orange", "teal", "purple", "magenta", "cyan", "lime", "rose"]

# name -> mode -> tokens. tones: hue -> (solid, text)
T = {
  # Default. Japanese materials: washi paper, sumi ink, ai (indigo) accent and
  # shu (vermilion) for the seal and for danger.
  "kowan": {
    "label": "Kowan",
    "pattern": "grain",
    "light": dict(bg="#f4f1ea", surface="#fbfaf6", raised="#ffffff", sunken="#ece8de", muted="#efebe2", hover="#e7e2d7",
                  border="#ddd7ca", input="#c9c2b2", fg="#1b1a17", mfg="#5f5a50", signal="#fa3600", primary="#1b1a17",
                  pfg="#fbfaf6", tick="#a39d8f", sidebar="#f8f6f0", seal="#fa3600", grid="#e6e0d3", link="#c42c00", signal_on="#1b1a17",
                  tones=dict(gray=("#7d776b", "#5c574d"), blue=("#264f9e", "#1f4386"), green=("#2f7d4f", "#256640"),
                             amber=("#b7741a", "#8f5a12"), red=("#c8102e", "#a80d26"), orange=("#fa3600", "#c42c00"),
                             teal=("#1f7f86", "#18666c"), purple=("#6b4fa0", "#583f86"), magenta=("#a8306e", "#8a2659"),
                             cyan=("#1d7aa1", "#176385"), lime=("#6a8a24", "#56711c"), rose=("#c2405f", "#a1344e"))),
    "dark": dict(bg="#121110", surface="#1a1917", raised="#22201d", sunken="#0d0c0b", muted="#211f1c", hover="#2a2724",
                 border="#2e2b27", input="#3d3934", fg="#ece6da", mfg="#a8a196", signal="#ff5a1f", primary="#ece6da",
                 pfg="#1a1917", tick="#5a554d", sidebar="#151412", seal="#fa3600", grid="#1f1d1a", link="#ff6a33",
                 tones=dict(gray=("#8f887c", "#c9c2b6"), blue=("#7e9cdb", "#a9bfea"), green=("#5bb07e", "#8fd1a8"),
                            amber=("#e0a24a", "#efc485"), red=("#ff4d5e", "#ff6b79"), orange=("#ff5a1f", "#ff6a33"),
                            teal=("#4fb3b8", "#8ad3d6"), purple=("#a28bd4", "#c5b5e6"), magenta=("#d873a6", "#eaa6c8"),
                            cyan=("#5fb0d4", "#97cfe8"), lime=("#9cc15a", "#c0dc8d"), rose=("#e57690", "#f2a7b8"))),
  },
  "graphite": {
    "label": "Graphite",
    "light": dict(bg="#ffffff", surface="#ffffff", raised="#ffffff", sunken="#f6f6f7", muted="#f3f3f4", hover="#ededef",
                  border="#dfdfe2", input="#cdcdd1", fg="#0a0a0a", mfg="#5c5c62", signal="#1f5cff", primary="#0a0a0a",
                  pfg="#ffffff", tick="#9a9aa0", sidebar="#ffffff",
                  tones=dict(gray=("#737373", "#525252"), blue=("#1f5cff", "#1a4fd6"), green=("#16a34a", "#15803d"),
                             amber=("#d97706", "#a8510a"), red=("#dc2626", "#b91c1c"), orange=("#ea580c", "#c2410c"),
                             teal=("#0d9488", "#0f766e"), purple=("#7c3aed", "#6d28d9"), magenta=("#c026d3", "#a21caf"),
                             cyan=("#0891b2", "#0e7490"), lime=("#65a30d", "#4d7c0f"), rose=("#e11d48", "#be123c"))),
    "dark": dict(bg="#070707", surface="#101010", raised="#161616", sunken="#040404", muted="#181818", hover="#1d1d1d",
                 border="#242424", input="#333333", fg="#ededed", mfg="#a1a1a1", signal="#5b8cff", primary="#ededed",
                 pfg="#0a0a0a", tick="#575757", sidebar="#0a0a0a",
                 tones=dict(gray=("#8a8a8a", "#c2c2c2"), blue=("#5b8cff", "#9db9ff"), green=("#2fbf6a", "#6ee7a0"),
                            amber=("#f2a93b", "#f7c46c"), red=("#f0524d", "#ff8a85"), orange=("#fb8a3c", "#fdba74"),
                            teal=("#2dd4bf", "#7de8d8"), purple=("#a78bfa", "#c4b5fd"), magenta=("#e879f9", "#f0abfc"),
                            cyan=("#22d3ee", "#7ee5f5"), lime=("#a3e635", "#c5f07a"), rose=("#fb7185", "#fda4af"))),
  },
  "nord": {
    "label": "Nord",
    "light": dict(bg="#e5e9f0", surface="#f8f9fb", raised="#ffffff", sunken="#d8dee9", muted="#eceff4", hover="#e1e6ee",
                  border="#d3d9e3", input="#c2c9d6", fg="#2e3440", mfg="#4c566a", signal="#5e81ac", primary="#2e3440",
                  pfg="#eceff4", tick="#a0a9b8", sidebar="#eef1f5",
                  tones=dict(gray=("#7b88a1", "#4c566a"), blue=("#5e81ac", "#4c6e99"), green=("#7f9a65", "#5e7d46"),
                             amber=("#c9a24a", "#8a6d1f"), red=("#bf616a", "#a3454e"), orange=("#d08770", "#a35a40"),
                             teal=("#6fa3a2", "#4d7f7e"), purple=("#b48ead", "#8a5f82"), magenta=("#b48ead", "#8a5f82"),
                             cyan=("#5e9fb3", "#3e7d8c"), lime=("#7f9a65", "#5e7d46"), rose=("#bf616a", "#a3454e"))),
    "dark": dict(bg="#242933", surface="#2e3440", raised="#3b4252", sunken="#20242c", muted="#3b4252", hover="#3f4757",
                 border="#3f4757", input="#4c566a", fg="#eceff4", mfg="#a6b0c1", signal="#88c0d0", primary="#eceff4",
                 pfg="#2e3440", tick="#4c566a", sidebar="#272c36",
                 tones=dict(gray=("#7b88a1", "#c2cad8"), blue=("#81a1c1", "#a9c2dc"), green=("#a3be8c", "#bfd4ad"),
                            amber=("#ebcb8b", "#f1dbae"), red=("#bf616a", "#dc949a"), orange=("#d08770", "#e3ad9b"),
                            teal=("#8fbcbb", "#b2d4d3"), purple=("#b48ead", "#cfb2ca"), magenta=("#b48ead", "#cfb2ca"),
                            cyan=("#88c0d0", "#b1d8e3"), lime=("#a3be8c", "#bfd4ad"), rose=("#bf616a", "#dc949a"))),
  },
  "dracula": {
    "label": "Dracula",
    "light": dict(bg="#ecebe2", surface="#fbfbf6", raised="#ffffff", sunken="#e2e0d4", muted="#f2f1ea", hover="#e8e6dc",
                  border="#dcdacd", input="#c8c5b5", fg="#1f1f1f", mfg="#57527f", signal="#644ac9", primary="#1f1f1f",
                  pfg="#fbfbf6", tick="#a9a6c4", sidebar="#f2f1ea",
                  tones=dict(gray=("#7a7690", "#57527f"), blue=("#3a5ccc", "#2f4cab"), green=("#14710a", "#14710a"),
                             amber=("#846e15", "#846e15"), red=("#cb3a2a", "#a8281a"), orange=("#a34d14", "#a34d14"),
                             teal=("#036a96", "#036a96"), purple=("#644ac9", "#5639b0"), magenta=("#a3144d", "#a3144d"),
                             cyan=("#036a96", "#036a96"), lime=("#14710a", "#14710a"), rose=("#a3144d", "#a3144d"))),
    "dark": dict(bg="#1e1f29", surface="#282a36", raised="#343746", sunken="#191a21", muted="#303241", hover="#383a4a",
                 border="#3a3d4f", input="#44475a", fg="#f8f8f2", mfg="#b0b2ca", signal="#bd93f9", primary="#f8f8f2",
                 pfg="#282a36", tick="#6272a4", sidebar="#21222c",
                 tones=dict(gray=("#6272a4", "#a7b0d6"), blue=("#8aa2ff", "#b3c3ff"), green=("#50fa7b", "#8afba6"),
                            amber=("#f1fa8c", "#f5fbb4"), red=("#ff5555", "#ff8b8b"), orange=("#ffb86c", "#ffcf9c"),
                            teal=("#8be9fd", "#b3f1fe"), purple=("#bd93f9", "#d4b8fb"), magenta=("#ff79c6", "#ffa6d9"),
                            cyan=("#8be9fd", "#b3f1fe"), lime=("#50fa7b", "#8afba6"), rose=("#ff79c6", "#ffa6d9"))),
  },
  "catppuccin": {
    "label": "Catppuccin",
    "light": dict(bg="#e6e9ef", surface="#f5f6f9", raised="#ffffff", sunken="#dce0e8", muted="#eff1f5", hover="#e3e6ec",
                  border="#ccd0da", input="#bcc0cc", fg="#4c4f69", mfg="#5c5f77", signal="#8839ef", primary="#4c4f69",
                  pfg="#eff1f5", tick="#9ca0b0", sidebar="#eceef3",
                  tones=dict(gray=("#7c7f93", "#5c5f77"), blue=("#1e66f5", "#1a55cc"), green=("#40a02b", "#2f7a1f"),
                             amber=("#df8e1d", "#a86b12"), red=("#d20f39", "#b00c30"), orange=("#fe640b", "#c94a06"),
                             teal=("#179299", "#127277"), purple=("#8839ef", "#7027d0"), magenta=("#ea76cb", "#b5459a"),
                             cyan=("#04a5e5", "#0379a8"), lime=("#40a02b", "#2f7a1f"), rose=("#e64553", "#c22f3d"))),
    "dark": dict(bg="#181825", surface="#1e1e2e", raised="#313244", sunken="#11111b", muted="#313244", hover="#2a2b3c",
                 border="#313244", input="#45475a", fg="#cdd6f4", mfg="#a6adc8", signal="#cba6f7", primary="#cdd6f4",
                 pfg="#1e1e2e", tick="#585b70", sidebar="#11111b",
                 tones=dict(gray=("#7f849c", "#bac2de"), blue=("#89b4fa", "#b4cffc"), green=("#a6e3a1", "#c4edc0"),
                            amber=("#f9e2af", "#fbecca"), red=("#f38ba8", "#f7b1c5"), orange=("#fab387", "#fccbac"),
                            teal=("#94e2d5", "#b8ece3"), purple=("#cba6f7", "#dcc4fa"), magenta=("#f5c2e7", "#f9d9f0"),
                            cyan=("#89dceb", "#b2e8f2"), lime=("#a6e3a1", "#c4edc0"), rose=("#eba0ac", "#f2c0c8"))),
  },
  "gruvbox": {
    "label": "Gruvbox",
    "light": dict(bg="#f2e5bc", surface="#fbf1c7", raised="#f9f5d7", sunken="#ebdbb2", muted="#f2e5bc", hover="#ebdbb2",
                  border="#d5c4a1", input="#bdae93", fg="#3c3836", mfg="#665c54", signal="#076678", primary="#3c3836",
                  pfg="#fbf1c7", tick="#a89984", sidebar="#f2e5bc",
                  tones=dict(gray=("#7c6f64", "#665c54"), blue=("#076678", "#076678"), green=("#79740e", "#79740e"),
                             amber=("#b57614", "#9a6411"), red=("#9d0006", "#9d0006"), orange=("#af3a03", "#af3a03"),
                             teal=("#427b58", "#427b58"), purple=("#8f3f71", "#8f3f71"), magenta=("#8f3f71", "#8f3f71"),
                             cyan=("#427b58", "#427b58"), lime=("#79740e", "#79740e"), rose=("#9d0006", "#9d0006"))),
    "dark": dict(bg="#1d2021", surface="#282828", raised="#32302f", sunken="#161819", muted="#32302f", hover="#3c3836",
                 border="#3c3836", input="#504945", fg="#ebdbb2", mfg="#a89984", signal="#83a598", primary="#ebdbb2",
                 pfg="#282828", tick="#665c54", sidebar="#1a1c1d",
                 tones=dict(gray=("#928374", "#bdae93"), blue=("#83a598", "#a8c2b8"), green=("#b8bb26", "#cfd16a"),
                            amber=("#fabd2f", "#fcd37a"), red=("#fb4934", "#fc8577"), orange=("#fe8019", "#feab68"),
                            teal=("#8ec07c", "#b0d4a3"), purple=("#d3869b", "#e2aebd"), magenta=("#d3869b", "#e2aebd"),
                            cyan=("#8ec07c", "#b0d4a3"), lime=("#b8bb26", "#cfd16a"), rose=("#fb4934", "#fc8577"))),
  },
  "tokyonight": {
    "label": "Tokyo Night",
    "light": dict(bg="#d5d6db", surface="#e9e9ed", raised="#f4f4f6", sunken="#c9cacf", muted="#e1e2e7", hover="#dadbe0",
                  border="#c4c8da", input="#a8aecb", fg="#343b58", mfg="#565a6e", signal="#2e7de9", primary="#343b58",
                  pfg="#e9e9ed", tick="#9699a3", sidebar="#e1e2e7",
                  tones=dict(gray=("#6172b0", "#4c5a8c"), blue=("#2e7de9", "#2465c6"), green=("#587539", "#4a6330"),
                             amber=("#8c6c3e", "#7a5c30"), red=("#f52a65", "#c8164d"), orange=("#b15c00", "#9a4f00"),
                             teal=("#118c74", "#0e7562"), purple=("#9854f1", "#7a3bd8"), magenta=("#9854f1", "#7a3bd8"),
                             cyan=("#007197", "#007197"), lime=("#587539", "#4a6330"), rose=("#f52a65", "#c8164d"))),
    "dark": dict(bg="#16161e", surface="#1a1b26", raised="#24283b", sunken="#121219", muted="#1f2335", hover="#292e42",
                 border="#292e42", input="#3b4261", fg="#c0caf5", mfg="#9aa5ce", signal="#7aa2f7", primary="#c0caf5",
                 pfg="#1a1b26", tick="#3b4261", sidebar="#16161e",
                 tones=dict(gray=("#565f89", "#9aa5ce"), blue=("#7aa2f7", "#a6c1fa"), green=("#9ece6a", "#bde09a"),
                            amber=("#e0af68", "#ebc995"), red=("#f7768e", "#faa4b4"), orange=("#ff9e64", "#ffbf98"),
                            teal=("#73daca", "#a2e7dc"), purple=("#bb9af7", "#d3bdfa"), magenta=("#bb9af7", "#d3bdfa"),
                            cyan=("#7dcfff", "#aadfff"), lime=("#9ece6a", "#bde09a"), rose=("#f7768e", "#faa4b4"))),
  },
}


def mix(a: str, b: str, t: float) -> str:
    """Blend hex colour a toward b by t (0..1)."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def canvas_image(pattern: str, color: str, mode: str = "light") -> str:
    """The page-background texture: the paper-grain overlay tile for the mode.

    The tiles are generated by scripts/gen-grain.py into public/ and served
    from the site root; a 248px tile is shown at 124px (2 texels per CSS px).
    """
    return f'url("/grain-{mode}.png")'


def block(sel: str, mode: str, v: dict, pattern: str = "cross") -> str:
    on = "#ffffff" if mode == "light" else v["surface"]
    lines = [f"{sel} {{", f"  color-scheme: {mode};",
             f"  --background: {v['bg']};", f"  --foreground: {v['fg']};",
             f"  --card: {v['surface']};", f"  --card-foreground: {v['fg']};",
             f"  --popover: {v['raised']};", f"  --popover-foreground: {v['fg']};",
             f"  --muted: {v['muted']};", f"  --muted-foreground: {v['mfg']};",
             f"  --accent: {v['hover']};", f"  --accent-foreground: {v['fg']};",
             f"  --border: {v['border']};", f"  --input: {v['input']};",
             f"  --ring: {v['signal']};", f"  --signal: {v['signal']};", f"  --link: {v.get('link', v['signal'])};",
             f"  --primary: {v['primary']};", f"  --primary-foreground: {v['pfg']};",
             f"  --secondary: {v['muted']};", f"  --secondary-foreground: {v['fg']};",
             f"  --destructive: {v['tones']['red'][0]};",
             f"  --tick: {v['tick']};", f"  --seal: {v.get('seal', v['tones']['red'][0])};",
             f"  --grid-line: {v.get('grid') or mix(v['bg'], v['border'], 0.55)};",
             f"  --canvas-image: {canvas_image(pattern, v['fg'], mode)};", f"  --surface-sunken: {v['sunken']};",
             f"  --sidebar: {v['sidebar']};", f"  --sidebar-foreground: {v['fg']};",
             f"  --sidebar-primary: {v['primary']};", f"  --sidebar-primary-foreground: {v['pfg']};",
             f"  --sidebar-accent: {v['hover']};", f"  --sidebar-accent-foreground: {v['fg']};",
             f"  --sidebar-border: {v['border']};", f"  --sidebar-ring: {v['signal']};",
             f"  --tone-soft-mix: 10%;",
             f"  --tone-line-mix: 45%;"]
    for hue in TONES:
        solid, text = v["tones"][hue]
        lines.append(f"  --tone-{hue}-solid: {solid}; --tone-{hue}-text: {text}; --tone-{hue}-on: {on};")
    lines.append(f"  --tone-primary-solid: {v['signal']}; --tone-primary-text: {v.get('link') or (v['tones']['blue'][1] if mode == 'light' else v['signal'])}; --tone-primary-on: {v.get('signal_on', on)};")
    lines.append("}")
    return "\n".join(lines)


out = ["/* GENERATED by scripts/gen-themes.py. Edit the table there, then rerun. */", ""]
for name, t in T.items():
    for mode in ("light", "dark"):
        sel = f'[data-palette="{name}"][data-theme="{mode}"]'
        if name == "kowan" and mode == "light":
            sel = ":root"
        elif name == "kowan":
            sel = '[data-theme="dark"]'
        out.append(block(sel, mode, t[mode], t.get("pattern", "grain")))
        out.append("")
soft = [":root, [data-theme] {"]
for hue in TONES + ["primary"]:
    soft.append(f"  --tone-{hue}-soft: color-mix(in srgb, var(--tone-{hue}-solid) var(--tone-soft-mix), var(--card));")
    soft.append(f"  --tone-{hue}-line: color-mix(in srgb, var(--tone-{hue}-solid) var(--tone-line-mix), var(--card));")
soft.append("}")
out.append("\n".join(soft))
path = Path(__file__).resolve().parent.parent / "src" / "styles" / "themes.css"
path.write_text("\n".join(out) + "\n")
print(f"wrote {path} ({len(T)} themes)")

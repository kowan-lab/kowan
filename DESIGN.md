# Design

Visual system for the Kowan web console.

## Direction

A precise, quiet developer tool built from hairlines rather than boxes: no corner radii, flat buttons, one accent colour, compact density, full use of the viewport and few cards. Paper panels and ink text, bold uppercase titles, mono uppercase labels, registration marks on frames. Reading text is a grotesk (Space Grotesk) for legibility; mono (Geist Mono) stays for labels and data.

Mode: Operate. The console is a working tool. The world contributes type, palette, density and one signature move; navigation and controls stay standard web patterns (sidebar, tables, tabs, dialogs, command palette).

Scene: an operator at a desk, often in the evening or during an incident, with a terminal open next to the browser. Dark is the primary theme; light is fully supported and follows the OS unless the user picks one.

## Signature move

**Full-bleed hairline bands with live state pills.** Work areas are bands that run edge to edge between 1px rules, like sheets of a technical drawing; tables meet the viewport edges. The few remaining boxes (dialogs, grid tiles) are square with small corner ticks where rules meet. Object state is a mono pill with a coloured hairline border and its duration (`RUNNING · 9d`, `EXITED 137 · 3m`), the same pill on every page. Nothing else is decorated.

## Palette

Japanese materials, used with restraint (no cherry blossoms, torii or brush fonts). Light is washi paper (#F4F1EA ground, #FBFAF6 surfaces) with sumi-ink text (#1B1A17); dark is warm sumi black (#121110 ground, #1A1917 surfaces) with paper-coloured text (#ECE6DA). One accent, signal orange (#FA3600 / #FF5A1F dark), does real jobs only: focus rings, the active marker, selection, primary actions, the primary chart series and the seal. Orange used as text is #C42C00 in light (`--link`); text on orange fills is ink (#1B1A17) for contrast. Danger is crimson (#C8102E) so it never reads as the accent; ai indigo (#264F9E) remains the info tone. Solid buttons are monochrome (ink on paper); a solid button takes a colour only when the colour means something.

State colours carry meaning only: green running/healthy, amber restarting/degraded/pending, red exited/failed/critical, gray stopped/unknown. State is never conveyed by colour alone: pills carry text, icons carry shape.

Seven themes, each in light and dark: **Kowan** (default: washi paper, sumi ink, signal orange), **Graphite** (neutral graphite, blueprint blue), **Nord**, **Dracula** (Alucard in light), **Catppuccin** (Latte and Mocha), **Gruvbox**, **Tokyo Night** (Day and Night). Every theme sets a page background, a lighter surface for bands and tables, and a raised surface for popovers, so content always stands off the background. State tones come from each palette's own red, green, yellow and blue; their soft tints are mixed into the card surface, never into transparency. The palette table and the per-palette background pattern live in `web/frontend/scripts/gen-themes.py`, which writes `src/styles/themes.css`; the picker is in the top bar and in Settings.

Tokens live in `web/frontend/src/styles/globals.css` (`--background`, `--card`, `--border`, `--primary`, `--accent-signal`, and the `--tone-<hue>-solid|soft|text|line` ramps used by the UI kit).

## Mark

A hanko seal carrying 港 (*kō*, "harbour"), the first character of 港湾 *kōwan*: a square in `--seal` with a paper-coloured inner hairline frame and the glyph reversed out in paper colour. The glyph is an outline from Shippori Mincho ExtraBold (SIL OFL 1.1), so it renders without Japanese fonts. Source: `docs/assets/logo.svg`; component: `src/components/KowanMark.tsx`. Kanji appear only in the mark, never in navigation or labels. The wordmark is `KOWAN` in Space Grotesk bold uppercase. The seal fill is the palette's `--seal` (signal orange in Kowan).

## Type

- Two faces, both self-hosted via `@fontsource-variable` (no network call at runtime): **Space Grotesk** for everything people read and for titles (variable, weights 300–700; titles use 700), **Geist Mono** for labels, table headers, state pills, numbers, IDs, ports and code.
- Panel-label rule: anything set in UPPERCASE is Geist Mono with small positive tracking (table headers, sidebar groups, stat labels, command-palette groups).
- Section page titles are Space Grotesk bold UPPERCASE at 24px with -0.02em tracking; detail pages show the object's real name as typed (22px semibold). The wordmark is `KOWAN`, bold uppercase.
- Scale (px), 10% up from the first compact pass for legibility: 12 meta and table headers · 13 body and table cells · 14 section titles · 22–24 page titles. Floor 12px.

## Shape and depth

- Radius: 0 everywhere (tokens `--radius-*` are 0). Only status dots are round.
- Buttons and inputs are flat: no shadows, no gradients, hairline borders. Depth comes from hairlines and a one-step lighter surface; popovers and dialogs keep one soft, offset shadow so they separate from the page.
- Background: the page canvas is the theme background colour with a fine paper grain: our own tileable Gaussian-noise texture (`public/grain-{light,dark}.png`, generated by `scripts/gen-grain.py`), 248px shown at 124px, about 1.7 grey levels of variation with soft neighbouring texels, laid over the ground as a transparent overlay so it works on every palette. No geometric pattern. **Every component has a solid surface**: page headers, bands, tables, tab bars, callouts, outline buttons and badges, forms; they stand off the ground by colour and hairlines.

## Density

Compact, on a **2px unit** (`--spacing: 0.125rem`): every size, gap, row and control is an even number of whole pixels, so centring and halving always land on a pixel and edges stay crisp. Rules for staying pixel-perfect:

- Font sizes and line heights are whole pixels (body 13/18).
- Never put a 1px border on an element with a fixed height whose content is centred: draw the rule as an inset shadow (`shadow-[inset_0_-1px_0_var(--border)]`). Tables use the separate border model with inset-shadow row rules for the same reason.
- No half-step utilities (`*-1.5`); use whole steps or explicit even `px` values.
- Inline badges and pills align `top`, not to the text baseline.

The content uses the full viewport width, no max-width container. Lists are full-bleed tables, not card grids; sections are bands, not cards.

## Motion

Fast and cheap: opacity and transform only, no blur. Enter eases out, exit eases in: overlays (dialogs, popovers, menus, selects) pop in at 97% scale in 90ms and out in 70ms; tooltips fade the same way; the sidebar collapses in 120ms ease-in-out. Page content fades in 4px (140ms) on navigation while the shell stays mounted; skeletons shimmer; theme changes cross-fade through the View Transitions API. One authored moment: a state pill that changes briefly pulses its border once. Respect `prefers-reduced-motion`.

Performance: page chunks are preloaded when the browser is idle, navigation runs as a React transition (the old page stays until the new one is ready), charts load lazily, long tables paginate.

## Components

shadcn/ui primitives (Radix) in `src/components/ui`, wrapped by the Kowan UI kit in `src/ui/kit` (Box, Text, Button, Badge, StatePill, Table, Select, Popup…). Pages import from `@/ui/kit` only.

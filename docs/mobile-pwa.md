# Phone and installed app

Every page works on a phone, and the console installs as an app (PWA).

## Layout

- The sidebar is a drawer behind the top-left button; the command palette
  (search) works the same.
- Kit tables become cards when their own box is narrower than 640 px: the
  first column is the card title, the others "LABEL value" lines, actions at
  the end (`.kit-table` container query in `styles/globals.css`, labels from
  `data-label` on each cell). Search and filters stay in the toolbar.
- Pages that put panels side by side stack them (Logs: picker above viewer;
  policy strip one column; secondary table columns hidden).
- `e2e/mobile.e2e.ts` checks a set of pages at 390 × 844 for sideways
  scrolling, the card layout and the drawer.

## Installable

- `public/manifest.webmanifest` (standalone, theme colours, shortcuts to
  Workloads, Logs, Uptime), icons rendered from the favicon by
  `scripts/gen-icons.mjs` (192, 512, maskable 512, Apple touch).
- `public/sw.js`: caches the app shell and the content-hashed bundles only.
  Never the API, webhooks or WebSockets: every value on screen is live.
  Pages are network-first (a new build is picked up at once), assets
  cache-first. Registered in production builds only.
- Offline, the installed app still opens and says so; the session survives
  a start-up without network (only a 401/403 ends it).
- The server serves `sw.js` and the manifest with `no-cache` and the
  manifest as `application/manifest+json`.

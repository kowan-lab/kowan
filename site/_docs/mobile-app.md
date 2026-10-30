---
title: Phone and installed app
description: Use the Kowan console on a phone or tablet and install it as an app on your home screen or desktop, with shortcuts and an offline start screen.
---

The Kowan console works on a phone as well as on a desktop: every page adapts to a small screen, and you can install the console as an app that opens from your home screen in its own window. There is nothing to download from an app store, and nothing to set up on the server.

## On a small screen

On a narrow screen, the console rearranges itself rather than shrinking:

- **The sidebar** becomes a drawer. Open it with the button at the top left; the host picker and Settings are inside it.
- **Search** is the magnifying glass in the top bar. It opens the same search palette as ⌘K on a desktop.
- **Tables become cards** when they are narrower than 640 pixels: the first column is the card's title, the other columns are labeled lines, and the row's actions sit at the end. Search and filters stay above the list.
- **Side-by-side panels stack**, and secondary table columns are hidden.

Every page is usable this way, so you can check a host, restart a [container](containers.md) or read its logs from your phone.

## Install Kowan as an app

Installed, Kowan opens in its own window without the browser's address bar, with its own icon.

- **iPhone and iPad:** open Kowan in Safari, tap **Share**, then **Add to Home Screen**.
- **Android:** open Kowan in Chrome and choose **Install app** or **Add to Home screen** from the menu.
- **Desktop:** in Chrome or Edge, use the install button in the address bar or the browser menu.

Where your system supports app shortcuts, long-press the installed icon (or right-click it on a desktop) to jump straight to **Workloads**, **Logs** or **Uptime**.

!!! note "Use HTTPS"
    Browsers run the part of Kowan that makes it installable and lets it start offline only on a secure address: `https://`, or `localhost` on the same machine. Put Kowan behind a reverse proxy with HTTPS; see [Installation](installation.md).

## What the app keeps on your device

The installed app keeps only the interface itself on your device: the page and its scripts, styles and icons. It never stores data from your servers. Every container, log line and alert on screen comes live from the Kowan server, and API calls, webhooks and live connections never go through the app's cache.

After you [upgrade](upgrading.md) Kowan, the app shows the new version as soon as you reopen or reload it.

## Offline

Without a network connection, the installed app still opens and says **You are offline**. Pages fill in again as soon as the connection is back. You stay signed in: the app signs you out only when the server itself rejects your session, not when it cannot reach the server.

## Related

- [First steps](first-steps.md): find your way around the console.
- [Uptime](uptime.md): checks and a public status page you can open from your phone.
- [Alerts and notifications](alerts-and-notifications.md): get told about problems without opening the app.

---
title: Digest
description: Get one daily or weekly message with pending image updates, vulnerable images, failed backups, expiring certificates and open alerts.
---

The digest is one message a day or a week with what needs attention but did not warrant an alert of its own: pending image updates, vulnerable images, failed backups, expiring certificates and alerts still open. It goes out through your [notification channels](alerts-and-notifications.md#set-up-a-notification-channel).

## Turn on the digest

Only admins set up the digest. You need at least one enabled notification channel.

1. Go to **Settings → Notifications** and find the **Digest** card below the channels.
2. Turn on **Send the digest**.
3. Under **Every**, choose **Day** or **Week**. For a weekly digest, choose the day under **On**.
4. Choose the hour under **At (UTC)**.
5. Under **What it covers**, check the sections you want.
6. Under **Channels**, leave every box clear to send it to every channel that is on, or check the ones that should receive it.
7. Click **Save**.

The digest starts out weekly, on Monday at 08:00 UTC, with every section. Kowan checks every 10 minutes whether it is due, so it goes out within the chosen hour. If the Kowan server is not running during that hour, that digest is skipped.

The digest ignores the channels' minimum level: every channel it goes to receives it.

## What it covers

| Section | Lists |
|---|---|
| Container updates waiting | Containers with a newer image in their registry, checked when the digest is built. Offline hosts are skipped. See [Image updates](image-updates.md). |
| Images with critical or high vulnerabilities | Images whose latest scan found critical or high vulnerabilities, worst first, with how many have a fix. See [Vulnerability scanning](vulnerability-scanning.md). |
| Backups that failed | [Volume backups](volume-backups.md) that failed during the period: the last day or the last week. |
| Certificates expiring within 21 days | Certificates of HTTPS [uptime checks](uptime.md) and of [exposed domains](exposure-and-https.md) that expire within 21 days or have expired. |
| Alerts still open | Every alert not yet resolved, with its level and since when. See [Alerts and notifications](alerts-and-notifications.md). |

Each section lists up to 10 items and counts the rest. Sections with nothing to report are left out. When there is nothing at all, Kowan skips the digest, unless you check **Send it even when there is nothing to report**: then it sends "Kowan digest: all clear".

The title sums up the counts, for example `Kowan digest: 3 updates, 1 failed backup`. The digest is a warning when it reports a failed backup, an expiring certificate or an image with critical vulnerabilities, and info otherwise. Channels that show a level use it, such as the subject of an email or the priority of an ntfy message.

## Preview and send now

**Preview**
:   Shows the title and text the digest would send now, with the saved settings, and the period it covers.

**Send now**
:   Sends the digest right away to its channels, even with nothing to report. A message lists the channels it reached and the ones that failed.

Below the settings, the card shows when the last digest went out and to which channels, or that it was skipped because there was nothing to report.

## Use the API

```text
GET  /api/v1/digest            settings and the last send (operator)
PUT  /api/v1/digest            save the settings (admin)
GET  /api/v1/digest/preview    the text it would send now (operator)
POST /api/v1/digest/send       send it now (admin)
```

The settings, with `weekday` from 0 (Monday) to 6 (Sunday), `hour` in UTC, and an empty `channels` list for every enabled channel:

```json
{
  "enabled": true,
  "every": "weekly",
  "weekday": 0,
  "hour": 8,
  "channels": [],
  "sections": {"updates": true, "cves": true, "backups": true, "certificates": true, "alerts": true},
  "sendEmpty": false
}
```

See [REST API](api.md) for authentication.

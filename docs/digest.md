# The notification digest

One message a day or a week with what needs attention but did not warrant an
alert of its own, sent through the notification channels.

Code: `kowan/digest.py`, `kowan/api/digest.py`; UI: Digest card on the
Notifications page (`components/notifications/DigestCard.tsx`).

## Sections

| section | content |
|---|---|
| updates | containers with a newer image in their registry |
| cves | images with critical or high vulnerabilities (latest scans) |
| backups | volume backups that failed in the period |
| certificates | uptime checks and exposed domains whose certificate expires within 21 days |
| alerts | alerts still open |

Empty sections are left out; with nothing to report the digest is skipped
unless `sendEmpty` is on. It goes to the chosen channels, or to every enabled
channel when none is chosen. Daily or weekly (`weekday`, 0 = Monday) at an
hour (UTC).

```
GET  /api/v1/digest            settings + last send   (operator)
PUT  /api/v1/digest            settings               (admin)
GET  /api/v1/digest/preview    the text it would send now
POST /api/v1/digest/send       send now               (admin)
```

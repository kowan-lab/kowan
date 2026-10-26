# Disk cleanup

What Docker keeps on a host that nothing uses, sized, with safe picks, and the
same cleanup on a schedule.

Code: `kowan/cleanup.py`, `kowan/api/cleanup.py`, the detail lists of
`docker system df` in `kowan/docker/local.py`; UI: `pages/DiskCleanup.tsx`
(`/hosts/<id>/cleanup`, linked from the overview's disk line and the host
page).

## Categories and default picks

| category | picked by default | why not always |
|---|---|---|
| stopped containers | unless a deployment owns them | a deployment stopped on purpose is recreated on its next deploy |
| untagged images | yes | nothing can use them |
| unused tagged images | from 30 days old | recent ones would be pulled again |
| unused volumes | anonymous ones | a named volume may hold data |
| build cache not in use | yes | |

Running a cleanup re-reads the disk usage and skips anything in use again;
images freed by removing their containers in the same run go too. Sizes are
estimates (`docker system df`).

## Schedule

Daily or weekly at an hour (UTC), per host, with the safe categories only:
stopped containers no deployment owns, untagged images, unused images older
than N days (0 = never), anonymous volumes, build cache. Never named volumes,
never a deployment's containers. Each run is recorded and announced in the
notifications bell.

```
GET /api/v1/hosts/{id}/cleanup                  plan + schedule + last run
POST /api/v1/hosts/{id}/cleanup {containers, images, volumes, buildCache}   (operator)
PUT /api/v1/hosts/{id}/cleanup/schedule {…}     (operator)
```

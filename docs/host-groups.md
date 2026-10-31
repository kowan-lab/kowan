# Host groups

User guide: `site/_docs/host-groups.md`.

Tags live in `host_tags (host_id, tag)` (a new table, so existing databases
get it): `Host.tags`, `store.set_host_tags`, `store.hosts_with_tag`. Set with
`PATCH /hosts/{id} {tags}` (admin; `check_tags` in `kowan/api/hosts.py`).

- **Grants**: scope `host_tag` (target = tag). `authorize._expand` turns it into
  one `host` grant per tagged host each time a principal loads.
- **Auto-update**: policy kind `group` (host "", target = tag) in
  `autoupdate_policies`; `GET /autoupdate/groups`, `PUT/DELETE
  /autoupdate/groups/{tag}`. In `process_host` the host's first group policy
  (by name) is the stored policy of containers and deployments that have none,
  except Kowan's own containers (`_kowan_own`).
- **Schedules**: target `group`, actions start/stop/restart over every container
  on the hosts, except Kowan's own and `kowan.schedule.group=off`; start only
  wakes containers with restart policy always/unless-stopped.
- **Runbooks**: a step on `group~<tag>` with `service` = a service or container
  name runs on every matching running container of the group's hosts.

Access to anything on a group needs host-wide access to each of its hosts
(`("host", id, "")` targets); an empty group needs fleet-wide access.

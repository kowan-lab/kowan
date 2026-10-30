---
title: Adding servers
description: Connect more Docker hosts to Kowan with the agent, in edge mode from behind NAT or in direct mode on your network, then manage or remove them.
---

Kowan manages the Docker host it runs on out of the box. To manage another server, you run the Kowan agent on it: a small container that gives Kowan access to that server's Docker, over a connection authenticated with a token unique to the host.

## Choose a connection mode

Every agent connects in one of two modes. You pick it when you add the host.

| | Edge | Agent (direct) |
|---|---|---|
| Who opens the connection | The agent connects out to the Kowan server | The Kowan server connects to the agent |
| Open ports on the new server | None | **9001**, reachable from the Kowan server |
| Kowan server must be reachable | Yes, from the new server | No |
| Use it for | Servers behind NAT or a firewall, home labs, cloud VMs without inbound rules | Servers on the same network as Kowan |

Both modes give the same features. If the connection drops, the side that opened it reconnects on its own, retrying every 1 to 30 seconds.

## Before you start

- The new server needs Docker Engine, on x86-64 (amd64) or ARM64.
- **Edge:** set `KOWAN_EXTERNAL_URL` on the Kowan server to the address agents use to reach it (see [Installation](installation.md)). Without it, the join command uses the address in your browser, which is wrong if you opened Kowan as `localhost`.
- **Direct:** make sure the Kowan server can reach the new server on port 9001, and that both clocks are correct: the connection is refused when they differ by more than 2 minutes.

Only admins can add, rename and remove hosts.

## Add a server

1. Go to **Settings → Hosts & agents** and click **Add host**. You can also pick **Add a host** from the host picker at the top of the sidebar.
2. Choose **Edge** or **Agent** (direct).
3. Enter a **Name** for the host, such as `prod-node-1`. It must be unique.
4. For **Agent** mode, enter the **Address** the Kowan server dials: the new server's IP or hostname and the port, such as `192.168.1.10:9001`.
5. Click **Add host**. Kowan generates a token for the host and shows the **Join command**.
6. Copy the command and run it on the new server.

Once the agent connects, the host shows as online in the host list and in the host picker, and its containers and stacks appear on the **Workloads** page.

## The join command

The join command starts the agent container with the host's token. For an edge host it looks like this:

```bash
docker run -d --name kowan-agent \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e KOWAN_AGENT_MODE=edge \
  -e KOWAN_SERVER_URL=https://kowan.example.com \
  -e KOWAN_AGENT_TOKEN=<token> \
  ghcr.io/kowan-lab/kowan-agent
```

For a direct host, it publishes port 9001 instead of naming the server:

```bash
docker run -d --name kowan-agent \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -p 9001:9001 \
  -e KOWAN_AGENT_MODE=agent \
  -e KOWAN_AGENT_TOKEN=<token> \
  ghcr.io/kowan-lab/kowan-agent
```

To see the command again, click the join command button on the host's row in **Settings → Hosts & agents**.

!!! warning "The token is a password"
    The token in the join command gives control of that server's Docker, which is root access to it. Do not paste it into tickets or chats, and do not reuse it on another server.

!!! tip "Restart the agent with the server"
    The generated command sets no restart policy. Add `--restart unless-stopped` before the image name so the agent comes back after a reboot.

### Run the agent with Docker Compose

The same settings as a Compose file, for an edge agent:

```yaml
services:
  agent:
    image: ghcr.io/kowan-lab/kowan-agent:latest
    environment:
      KOWAN_AGENT_MODE: "edge"
      KOWAN_SERVER_URL: "https://kowan.example.com"
      KOWAN_AGENT_TOKEN: "<token>"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped
```

### Agent settings

| Variable | Default | Purpose |
|---|---|---|
| `KOWAN_AGENT_MODE` | `agent` | `edge` to connect out to the server, `agent` for direct mode. |
| `KOWAN_AGENT_TOKEN` | | The host's token from Kowan. The agent does not start without it, and in direct mode it must be at least 32 characters. |
| `KOWAN_SERVER_URL` | | Edge mode: the Kowan server's address, such as `https://kowan.example.com`. |
| `KOWAN_AGENT_ADDR` | `:9001` | Direct mode: the address and port the agent listens on. |
| `KOWAN_AGENT_ALLOW_CIDRS` | | Direct mode: addresses or CIDRs allowed to connect, comma-separated. Set it to the Kowan server's address. |
| `KOWAN_AGENT_TLS_CERT`, `KOWAN_AGENT_TLS_KEY` | | Direct mode: paths to your own PEM certificate and key. Without them the agent generates a self-signed certificate at every start. |
| `KOWAN_DOCKER_SOCKET` | `/var/run/docker.sock` | The Docker socket, for rootless Docker or Podman. |

On the server side, `KOWAN_EDGE_ALLOW_CIDRS` limits which addresses can connect edge agents. See [Network access](network-access.md).

## How the connection is secured

The rule is the same in both modes: the side that opens the connection proves it knows the host's token, and the side that accepts it checks it.

- **Edge:** the agent presents its token, and the Kowan server checks it before accepting the connection. With an `https://` server URL, the agent verifies the server's certificate, so the server needs a valid one.
- **Direct:** the agent serves an encrypted connection (`wss://`) on port 9001. The token itself never travels: the server and the agent each prove they know it, bound to the agent's certificate, so a machine in the middle with its own certificate is refused by both sides.

See [Architecture](architecture.md) for the full picture.

## Manage a server

Open a host from **Settings → Hosts & agents** to see its name, address, mode, status and when it was last seen. Its **Swarm** tab lists the nodes when the host is a Docker Swarm manager.

- **Rename** a host from its page or from the host list. Only the displayed name changes: deployments, permissions and history stay attached to the host. From the command line: `kowanctl hosts rename <id-or-name> <new-name>`.
- **Update the agent** from the host's page, or all agents at once with **Update agents** on the host list. See [Upgrading](upgrading.md).
- **Clean up disk space** with **Disk cleanup** on the host's page. See [Disk cleanup](disk-cleanup.md).
- **List hosts** from the command line with `kowanctl hosts ls`.

### Remove a server

Click the remove button on the host's row and confirm. This removes the host from Kowan only: its containers keep running and the agent stays on the server. To finish, remove the agent on that server:

```bash
docker rm -f kowan-agent
```

The local host, the one Kowan runs on, cannot be removed.

## Manage Kowan's own server through an agent

By default the Kowan server manages its own host through the Docker socket mounted into it. If you would rather keep the server off the socket, remove the socket mount, add an edge host in Kowan and run its agent next to the server. On the same Compose network, the agent can reach the server by its service name, with `KOWAN_SERVER_URL` set to `http://server:5081`.

Without the socket, the server can no longer [update itself](upgrading.md) from the console.

## Troubleshooting

If a host stays offline, read the agent's log first:

```bash
docker logs kowan-agent
```

- **Edge agent cannot connect:** check that the server URL is reachable from the new server, that a reverse proxy in front of Kowan passes WebSocket connections, and that the address is allowed by `KOWAN_EDGE_ALLOW_CIDRS` if you set it.
- **Edge agent rejects the certificate:** the server URL uses `https://` with a certificate the agent cannot verify. Use a certificate from a public authority, such as Let's Encrypt.
- **Direct host never comes online:** check that port 9001 is reachable from the Kowan server, that the address in Kowan includes the port, that `KOWAN_AGENT_ALLOW_CIDRS` includes the server's address, and that the two clocks agree.
- **Wrong server address in the join command:** set `KOWAN_EXTERNAL_URL` on the server, then show the join command again.

## Next steps

- [Applications](applications.md): see what runs on every host.
- [Upgrading](upgrading.md): keep the server and the agents up to date.
- [Network access](network-access.md): limit who can reach the server and the agents.

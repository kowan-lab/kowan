# Architettura di Kowan


Piattaforma di gestione Docker scritta in **Python**
(asyncio): un control-plane che serve API REST + una SPA React, un agent per host,
e una CLI. Framework: **aiohttp** (REST + WebSocket + static SPA), **pydantic v2**
(modelli), **sqlite3** stdlib (store), **cryptography** (cifratura secret + TLS
self-signed dell'agent), **httpx** (client), **typer** (CLI). Password con
**scrypt**, JWT **HS256** fatto a mano, Docker via **Engine API** su unix-socket —
nessuna dipendenza esterna oltre a quelle in `requirements.txt` (niente
fastapi/docker-py/bcrypt/pyjwt).

Il cuore del trasporto è un **multiplexer di stream scritto in casa** + un
**tunnel bidirezionale server↔agent** con auth a token — perché in Python non
esiste un equivalente leggero e mantenuto di `hashicorp/yamux`.

```
kowan/
  mux/         # multiplexer simmetrico (solo stdlib asyncio): frame/stream/session
  transport/   # il "tubo" a messaggi: memory (test), websocket, aiohttp_ws
  tunnel/      # tunnel host token-based: handshake/endpoint/hub (stoa + atos)
  config.py errors.py events.py backup.py
  models/      # modelli pydantic (alias camelCase = json tag), _serde.drop_empty per omitempty
  store/       # Store sqlite3, schema completo + CRUD (mixin)
  auth/        # scrypt + JWT HS256 + api-key + middleware/require_role
  web/         # httpx helpers + Services (DI) + create_app + middleware + SPA
  docker/      # Ops + LocalClient (Engine API) + RemoteClient (mux) + Provider
  agentproto.py agent_ops_server.py   # RPC docker-over-mux + dispatcher agent
  api/         # moduli REST (register(app,svc)) montati sotto /api/v1
  deployment/ authn/ secrets/ registry/ notify/ workflow/ triggers/ monitor/
  server.py    # control-plane (python -m kowan.server)
  agent.py     # agent edge/direct (python -m kowan.agent)
  cli/         # kowanctl (python -m kowan.cli)
tests/         # test stdlib (mux/tunnel + REST end-to-end + docker-over-tunnel)
examples/      # ws_demo.py (mux su WebSocket reale)
web/           # frontend/ (sorgenti React) + dist/ (SPA buildata, servita dal server)
```

## Avvio

```bash
make deps          # pip install -r requirements.txt
make frontend      # builda la SPA in web/dist
make run           # http://localhost:5081, admin/admin
```

Il server legge `web/dist` da disco a runtime, quindi la UI si aggiorna
rebuildando solo il frontend. Le operazioni Docker reali richiedono un daemon
(`/var/run/docker.sock`); su macOS con OrbStack/Docker Desktop il socket punta al
daemon locale.

## Le due idee chiave

**1. Un mux è un tubo bidirezionale.** Una volta stabilito il byte-stream,
*entrambi* i lati possono aprire stream verso l'altro. Serve solo evitare
collisioni di `stream_id`: il lato "client" usa id **dispari**, il lato
"server" usa id **pari**.

**2. Chi dial-a il socket ≠ chi apre gli stream.** Il **control-plane è
sempre il client-mux** (apre stream per le op sull'host), l'**agent è sempre
il server-mux** (apre stream per push di eventi/metriche) — *a prescindere*
da chi ha aperto la connessione. Questo è ciò che permette una via unica per
i due scenari.

### I due assi (indipendenti)

| Asse | Valori | Deciso da |
|------|--------|-----------|
| Chi dial-a | manda vs verifica il **token** | modalità di connessione |
| Chi è control-plane | client-mux (id dispari) vs server-mux (id pari) | ruolo, non chi dial-a |

### Le due modalità di connessione

| Modo | Chi dial-a | Token | Serve l'IP? | Chi riconnette |
|------|-----------|-------|-------------|----------------|
| **atos** (edge / dietro NAT) | l'**agent** → server | l'agent lo **manda**, il server lo **verifica** | no | l'agent (backoff) |
| **stoa** (direct) | il **server** → agent | il server lo **manda**, l'agent lo **verifica** | **sì** (IP dell'agent) | il server |

> **La regola del token è una sola: chi ha dial-ato il socket manda il
> token, chi ha accettato lo verifica.** L'agent quindi ha *sempre* il token
> configurato (lo manda in atos, lo verifica in stoa) — e la modalità si
> gira con una env. Il controllo lato agent in stoa è una sicurezza in più:
> un server compromesso non può pilotare l'agent senza il token giusto.

In direct mode l'agent serve **wss://** con un certificato self-signed
effimero (P-256) generato all'avvio se non ne fornisci uno con
`KOWAN_AGENT_TLS_CERT` / `KOWAN_AGENT_TLS_KEY`; il control-plane si collega
saltando la verifica.

## Il formato dei frame

Un frame per messaggio del transport (una WebSocket binary message = un
frame → niente framing extra).

```
0        1        2                   6                   10
+--------+--------+-------------------+-------------------+
|  type  | flags  |     stream_id     |      length       |  payload...
+--------+--------+-------------------+-------------------+
```

- `type`: `DATA` | `WINDOW_UPDATE` | `PING` | `GO_AWAY`
- `flags`: `SYN` (apri stream) · `FIN` (half-close) · `RST` (abort)
- **Flow control a credito**: ogni stream parte con una finestra fissa; chi
  scrive non può superare la sua `send_window` e si blocca (backpressure)
  finché chi legge, *consumando* i byte, non gli ridà credito con un
  `WINDOW_UPDATE`. Così uno stream lento (es. un follow dei log) non affama
  gli altri e la memoria resta limitata.

## Provare

```bash
# test (solo stdlib, nessuna installazione)
make test
# oppure: python3 -m unittest discover -s tests -t . -v

# demo end-to-end su WebSocket reale (aiohttp)
PYTHONPATH=. python3 examples/ws_demo.py
```

La demo mostra, su un'unica WebSocket dial-ata dall'agent:
`control-plane → agent` (una host-op) **e** `agent → control-plane` (un push
di evento), entrambe multiplexate.

## Stato

Il control-plane boota e serve l'intera superficie REST su SQLite reale; agent e
CLI partono; il path docker-over-tunnel è verificato end-to-end contro Docker
reale (host + VM). Il progetto è stato portato 1:1 da una versione Go (ora
rimossa) e un audit di parità pre-rimozione ha chiuso 10 divergenze.

**Da rifinire**, soprattutto contro servizi reali: lo streaming WebSocket
(logs/stats/exec) è implementato ma poco testato e2e; alcuni provider esterni
(LDAP live, secret manager cloud) sono implementati ma non testati contro i
servizi reali. Da consolidare incrementalmente.

## Variabili d'ambiente: retention e prestazioni

| Variabile | Default | Effetto |
|---|---|---|
| `KOWAN_AUDIT_RETENTION` | `90d` | Elimina le voci di `audit_log` più vecchie di questa durata. |
| `KOWAN_ALERT_RETENTION` | `30d` | Elimina gli alert **risolti** più vecchi di questa durata (quelli attivi restano). |
| `KOWAN_WORKFLOW_RUNS_KEEP` | `100` | Run conservate per ciascun workflow (le più recenti; una run ancora `running` non viene mai eliminata). |
| `KOWAN_UVLOOP` | (attivo) | `0` disattiva uvloop e usa il loop asyncio standard. |

Le durate accettano il formato Go (`720h`, `1h30m`), i giorni (`90d`) o secondi
interi; `0` disattiva quella pulizia. Il prune gira circa una volta all'ora nel
loop del monitor. `orjson` e `uvloop` sono dipendenze opzionali: se mancano si
usano `json` e `asyncio` della stdlib.

## Licenza

MIT, vedi [LICENSE](../LICENSE). Kowan è software libero: nessuna dipendenza privata, nessun account richiesto.

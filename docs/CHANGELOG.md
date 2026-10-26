# Kowan — Changelog

## Non rilasciato

- **Rinomina degli host**, compreso il local: dalla pagina dell'host, dalla lista Hosts (solo admin) o con `kowanctl hosts rename <id-o-nome> <nuovo-nome>`; API `PATCH /api/v1/hosts/{id}`. Cambia solo il nome mostrato: id, deployment, permessi e storico restano gli stessi. Un nome non può ripetere il nome o l'id di un altro host (i file GitOps li usano entrambi), vale anche alla creazione.
- **Pulsanti "copia" su HTTP**: aprendo Kowan come `http://<indirizzo>` (non HTTPS né localhost) il browser non offre l'API degli appunti, e i pulsanti di copia non facevano nulla (il comando di join di un host, chiavi API, secret, URL). Ora ripiegano sulla copia classica e, se anche quella non riesce, lo dicono.
- **Import da Portainer, variabili sensibili cifrate**: password, token, chiavi e URL con credenziali non vengono più scritti nel compose. Finiscono in un secret Kowan cifrato `<stack>-env` (un campo per variabile) e il compose li richiama come `[[<stack>-env.VAR]]`; le altre variabili (tag delle immagini, porte, fuso orario) restano nel compose. Riservato agli admin, come ogni secret.
- **Stack su due livelli** in Workloads: i container non gestiti si raggruppano per stack anche quando vengono da uno stack Swarm (`com.docker.stack.namespace`), non solo da un progetto compose; aprendo uno stack compaiono i servizi, e ogni servizio mostra i propri container (le repliche, per Swarm) con uno stato riassuntivo. Log conservati, storico risorse e pulizia del disco usano la stessa regola.
- CI: `coverage` 7.16.2 su Python 3.10 (la 7.17 richiede 3.11) e test end-to-end con Docker allineato alla scheda Overview.

## 0.1.0 — 2026-10-26

Primo rilascio pubblico. Immagini `ghcr.io/kowan-lab/kowan` e `ghcr.io/kowan-lab/kowan-agent` (amd64 e arm64), CLI con `pip install git+https://github.com/kowan-lab/kowan`. Licenza MIT.

**Cosa contiene**
- **Server, agent e CLI**: un control plane Python con SQLite e nessun database esterno; agent collegati da un tunnel autenticato, in modalità edge (l'agent chiama il server, anche dietro NAT) o diretta (il server chiama l'agent); `kowanctl` per il terminale.
- **Risorse Docker** su tutti gli host: container, immagini, volumi, reti e stack, con log live, console web e file browser.
- **Deployment** da editor, template o repository git, con validazione del compose, revisioni con diff e rollback, anteprime per branch e pull request, promozione da staging a produzione e import da Portainer o da cartelle compose.
- **Aggiornamenti sicuri**: scansione delle vulnerabilità con Trivy, auto-update con finestre di manutenzione e rollback automatico se il controllo di salute fallisce; aggiornamento di server e agent con rollback.
- **Esposizione e HTTPS** tramite Traefik, mappa della rete, backup dei volumi con restic (disco locale, S3, SFTP), start/stop programmati, GitOps, workflow e webhook.
- **Osservabilità**: uptime con pagina di stato pubblica opzionale, log conservati e ricercabili, storico delle risorse con avviso di crescita della memoria, alert su email e chat, digest giornaliero o settimanale, pulizia guidata del disco.
- **Accesso**: ruoli admin/operator/viewer, team con permessi per risorsa, SSO OpenID Connect, chiavi API, audit log, allowlist di rete, server MCP per gli assistenti AI.
- **Interfaccia** React con sei temi chiari e scuri, usabile da telefono e installabile come app.

Le sezioni che seguono raccontano lo sviluppo che ha portato alla 0.1.0.

## 2026-10-26 — Dieci funzioni nuove, telefono e app installabile

**Funzioni** (ognuna con test backend, test della UI ed end-to-end; dettagli in `docs/`)
1. **Cronologia dei deployment**: ogni salvataggio che cambia ciò che gira è una revisione numerata, con autore e motivo (modifica, push git, GitOps, rollback, promozione, import…). Diff YAML tra due revisioni qualsiasi e rollback con un clic; un rollback di un deployment git resta fissato al commit di allora. Scheda History del workload, `kowanctl deployment history | diff | rollback`. → `docs/deployment-history.md`
2. **Uptime**: controlli HTTP(S) e TCP con codici attesi, parola chiave, tempi di risposta, incidenti, alert sui canali, scadenza dei certificati; collegabili a un workload. Pagina di stato pubblica opzionale su `/status` (senza indirizzi né errori). → `docs/uptime.md`
3. **Log conservati e ricercabili**: un workload "tiene i log" per N giorni; il server li segue su tutti gli host e li indicizza (SQLite FTS5, file separato `logs.db`). Logs → History cerca per parole, frasi ed esclusioni su più host e intervalli, con paginazione; i team vedono solo i propri container. → `docs/retained-logs.md`
4. **Storico delle risorse** per workload (CPU, memoria e rete a 5 minuti, 30 giorni) nella panoramica, e **avviso di crescita anomala della memoria**: una salita costante senza riavvii diventa un alert con il ritmo e, se c'è un limite, le ore che mancano. → `docs/resource-history.md`
5. **Ambienti di anteprima** per branch e pull/merge request dai deployment git: creati e aggiornati dal webhook del deployment (GitHub, Gitea, GitLab) o a mano, rimossi alla chiusura della PR, alla cancellazione del branch o dopo N ore senza push. Porte e nomi fissi tolti dal compose perché girino accanto al genitore; variabili `KOWAN_PREVIEW_*` per i domini. `kowanctl deployment previews | preview`. → `docs/previews-and-promotion.md`
6. **Promozione** staging → produzione: copia l'artefatto (immagine, immagini dei servizi compose modificate sul posto, o il commit git esatto), mai l'ambiente; anteprima delle modifiche con diff prima di applicarle, come nuova revisione del target. `kowanctl deployment promote [--dry-run]`.
7. **Import** da Portainer (token o login, usati solo per la lettura) e da cartelle di stack compose scelte nel browser. Gli stack diventano deployment creati e non avviati; con lo stesso nome di progetto adottano i container già in esecuzione. → `docs/import.md`
8. **Pulizia guidata del disco** con stima per categoria e scelte prudenti (i container fermati di un deployment, i volumi con nome e le immagini recenti restano esclusi), e **pulizia programmata** giornaliera o settimanale con le sole categorie sicure. → `docs/disk-cleanup.md`
9. **Digest periodico**: un messaggio al giorno o alla settimana con aggiornamenti in attesa, immagini con CVE critiche o alte, backup falliti, certificati in scadenza e alert aperti; anteprima del testo e invio immediato. → `docs/digest.md`
10. **Telefono e PWA**: tutte le pagine usabili da telefono (le tabelle diventano schede, i pannelli affiancati si impilano), console installabile come app con icone, scorciatoie e avvio offline che lo dice chiaramente; il service worker conserva solo l'interfaccia, mai i dati. → `docs/mobile-pwa.md`

**Test**
- Backend: 1165 test, coverage 90,0% (righe + rami), soglia in `.coveragerc` alzata a 89. Nuovi test anche su percorsi d'errore del servizio deployment, protocollo dell'agent, auto-update, self-update, GitOps e backup.
- Frontend: 465 test, 87% delle righe; soglie di `vitest.config.ts` alzate.
- End-to-end: 51 scenari Playwright (più quello Docker, opzionale), compresi cronologia, uptime, log, anteprime, promozione, import, digest, layout da telefono e PWA offline.

**Correzioni**
- CLI: `kowanctl` perdeva la sorgente git creando o modificando un deployment: un deployment git modificato da riga di comando diventava inline.
- Panoramica: "Reclaim" eliminava con un clic tutti i container fermi (anche quelli di un deployment fermato apposta) e tutti i volumi inutilizzati; ora apre la pulizia guidata.
- Avvio dell'app: senza rete, o con il server in riavvio, la sessione veniva chiusa e si finiva sulla pagina di login; ora solo un 401/403 la chiude.
- Ripristino di un backup: un archivio con un link simbolico sotto `stacks/` falliva con un errore incomprensibile invece di saltare il link.
- Le chiamate di creazione di un deployment controllano i conflitti di porte escludendo i container dello stesso progetto compose (necessario per adottare stack esistenti).

## 2026-10-26 — Test su tutto, e i bug che hanno trovato

**Test**
- Backend: 1033 test, coverage 90% delle righe (88% righe + rami). Nuovi test per agent edge e diretto end-to-end, client Docker attraverso il tunnel, CLI `kowanctl`, workflow e scheduler, GitOps, backup dei volumi, secret esterni (Vault, Infisical, Phase), notifiche (SMTP finto), SSO, scan, permessi per team, errori dello store e del daemon.
- Frontend: da zero a 377 test di pagine e componenti (Vitest + Testing Library), 86% delle righe. Ogni pagina è provata da utente con il backend finto: creazione, modifica, errori, permessi per ruolo.
- End-to-end: Playwright contro un server reale (primo avvio, tutte le pagine senza errori, utenti e ruoli, secret, workflow, allarmi, deploy di un container vero).
- `make coverage`, `make test-ui`, `make coverage-ui`, `make e2e`, `make test-all`; soglie minime di coverage in `.coveragerc` e `vitest.config.ts`; la CI esegue tutto. Vedi `docs/testing.md`.

**Correzioni, backend**
- Leggere un file di un volume attraverso il tunnel restituiva un contenuto vuoto.
- Gli errori 4xx dell'agent arrivano come 400/401/403/404/409 invece di un generico 502.
- La risoluzione dei secret esterni (Vault, Infisical, Phase) bloccava il loop asyncio del server durante deploy, workflow, GitOps e anteprime.
- Un volume CIFS (`//server/share`) era scambiato per un percorso dell'host.
- Creare un utente con un nome già usato dava 500 invece di 409.
- Riesporre un container o un deployment perdeva l'etichetta della rete a cui Kowan l'aveva collegato.
- Stats ed exec di un container inesistente davano 502 invece di 404.
- Il ripristino di un backup estrae in una cartella temporanea e sostituisce quella dei dati solo dopo la verifica.
- `KOWAN_ADDR` con il solo host (senza porta) non era accettato.
- Il webhook GitOps controlla "sync disabilitato" prima delle esclusioni per percorso.
- Corpi JSON non validi su reti, plugin e volumi davano 500 invece di 400.
- CLI: compatibilità con typer 0.27 (che include click).

**Correzioni, interfaccia**
- Un errore di rendering in una pagina svuotava tutta l'app: ora la pagina mostra l'errore e un "Try again", la shell resta.
- Se `GET /hosts` falliva, le pagine in stato di errore entravano in un loop di richieste (migliaia al secondo) e il dettaglio host restava su uno spinner.
- Secret da una connessione: il form chiedeva di nuovo, obbligatori, i campi che la connessione ha già (per esempio l'indirizzo di Vault).
- Provider SSO: cliccare sul client secret in modifica e salvare cancellava il secret salvato.
- Comandi con argomenti tra virgolette (`nginx -g 'daemon off;'`): il form dei container li spezzava sugli spazi e l'editor compose li rompeva a ogni modifica di un altro campo.
- Campi numerici: svuotare un campo con un valore di default lo riportava al default e la cifra digitata si accodava (6 → "612" ore nell'intervallo dell'auto-update).
- Il campo cron mandava in errore la pagina se l'anteprima non restituiva date.
- Hosts: il pulsante "Update" dell'agent nella riga apriva il dettaglio dell'host invece della conferma.
- Workloads: i viewer vedevano "New workload" e le azioni di prune.
- Nuovo deployment: l'host scelto nella shell non era preselezionato.
- Nella panoramica di un deployment ora compaiono stato di salute, sorgente git e webhook (i componenti esistevano ma non erano collegati).
- Etichetta accessibile sul protocollo delle porte nel form dei container.

## 2026-10-25 — Software libero, nuova UI, prestazioni

**Licenza e distribuzione**
- Licenza **MIT**. Build e immagini usano solo pacchetti pubblici.
- Porta di default del server **5081** al posto di 8080 (in goroawase 5-0-8-1 si legge *ko-o-wa-n*). `KOWAN_ADDR` la cambia come prima.
- Immagini Docker su Alpine multi-stage, `FROM scratch` finale: server da 138 a 59 MB compressi, agent da 102 a 57 MB. HEALTHCHECK ed etichetta `org.opencontainers.image.version`. Binari docker/compose verificati via checksum in build.

**Sicurezza** (audit del 25 ottobre, voci P0)
- Setup iniziale protetto da un token monouso (log del server o `setup-token` nel volume dati; `KOWAN_SETUP_TOKEN` per il provisioning).
- Sessioni verificate sul database a ogni richiesta: logout che revoca il token, cambio password che chiude le altre sessioni, utente cancellato bloccato subito, ruolo letto dal database e non dal token.
- Step "script" dei workflow: ambiente minimo (mai quello del server), niente export di `PATH`/`BASH_ENV`/`LD_*`, solo admin, disattivabili con `KOWAN_SERVER_SCRIPTS=off`.
- Step HTTP: i valori del payload dei webhook entrano per ultimi e alla lettera, non possono più risolvere `[[segreti]]`.
- Ai viewer non arrivano più token dei webhook, segreti dei push git, output delle esecuzioni dei workflow e valori delle variabili d'ambiente (container, spec, compose).
- Tunnel: token verificato prima dell'upgrade WebSocket, timeout sull'handshake, messaggi limitati a 64 KB; l'agent non parte senza un token di almeno 32 caratteri.
- Allowlist di rete `KOWAN_UI_ALLOW_CIDRS` ed `KOWAN_EDGE_ALLOW_CIDRS` sul server, `KOWAN_AGENT_ALLOW_CIDRS` sull'agent; l'indirizzo del client arriva da un solo header (`KOWAN_PROXY_HEADER`) e solo dai `KOWAN_TRUSTED_PROXIES`.
- Limite ai tentativi di login (per indirizzo e per utente) e all'hashing concorrente; password di almeno 8 caratteri ovunque.
- Header di sicurezza: CSP stretta, `frame-ancestors 'none'`, nosniff, `Referrer-Policy`, HSTS su HTTPS.

**Sicurezza, seconda passata** (revisione delle correzioni e delle dipendenze)
- Dipendenze Python a versioni esatte verificate su OSV (`requirements.txt`); in `pyproject.toml` minimi senza advisory noti (aiohttp ≥ 3.14.4, cryptography ≥ 49, orjson ≥ 3.11.6). Rimossa `websockets`, mai usata.
- react-router-dom 6.30.6; redirect dopo il login solo verso percorsi locali.
- Regola degli step script condivisa tra REST e MCP (via MCP un operator poteva ancora eseguirli).
- Vista del compose per chi non può operare costruita da una allowlist (niente ancore, campi `x-`, build args, segreti inline, comandi); anche via MCP. Credenziali tolte dagli URL git, argomenti extra e header HTTP dei workflow mascherati. La decisione usa i grant per risorsa, non solo il ruolo globale.
- YAML: limite di 100.000 nodi dopo l'espansione degli alias (bomba "billion laughs" respinta in 11 ms).
- Build da git come i deployment: URL e ref validati, `--`, protocolli limitati, contesto che non esce dalla cartella temporanea. Comandi docker/git con timeout e output limitato.
- Risposte HTTP in uscita lette fino al limite (RSS, git, step HTTP, notifiche, token dei registry).
- Token dei webhook mascherati nei log.
- `X-Forwarded-For` con porte e valori illeggibili gestito senza ripiegare sull'indirizzo del proxy; un solo header (`KOWAN_PROXY_HEADER`). L'allowlist del server per gli edge si chiama ora `KOWAN_EDGE_ALLOW_CIDRS`.
- Limite ai login che non permette di bloccare l'admin: per indirizzo+utente, per indirizzo (IPv6 per /64), per utente solo oltre 100 errori e mai dagli indirizzi già usati con successo.
- Cambio di ruolo che chiude le sessioni; timeout anche sull'handshake edge dell'agent; corretto `is_admin()` che dava 500 agli utenti con scope.

**Sicurezza, modalità diretta (stoa)**
- Il token non viaggia più: server e agent si provano a vicenda di conoscerlo con un challenge-response HMAC legato all'impronta del certificato TLS dell'agent. Un intercettatore con il proprio certificato viene rifiutato da entrambi i lati, e il server non manda nulla a un agent falso. Nessuna configurazione richiesta, anche con il certificato rigenerato a ogni avvio.
- Prima dell'upgrade l'agent accetta solo un HMAC del token legato all'ora (±2 minuti), non il token.

**Impostazioni > Network access**
- Le allowlist per UI e agent edge si gestiscono anche dalla UI (solo admin): restringono quelle dell'ambiente, non le allargano; il salvataggio chiede la password e viene rifiutato se escluderebbe l'indirizzo da cui ti stai collegando. Proxy fidati e header restano solo nell'ambiente, visibili in sola lettura.
- Cambiare la propria password non chiude più la sessione della scheda in uso (il server restituisce un token nuovo); le altre sessioni sì.

**Creazione dei deployment**
- Nuova sezione "After creating" (e "More options" nel dialog dei template): deploy subito, auto-update con finestra, backup dei volumi, orari di start/stop, dominio tramite il reverse proxy, webhook di redeploy e protezione dalla cancellazione si scelgono già alla creazione e vengono applicati appena il deployment è salvato. Se qualcosa non va a buon fine il deployment resta creato e un avviso dice cosa completare.
- Le immagini includono `tzdata`: senza, gli schedule con un fuso diverso da UTC fallivano (le immagini partono `FROM scratch` e non hanno `/usr/share/zoneinfo`).

**Exposure**
- Rimosso il supporto a caddy-docker-proxy: Kowan riconosce, installa e gestisce solo Traefik (rilevamento, etichette, certificati, scelta della rete).

**Aggiornamenti delle dipendenze**
- Frontend: React 19.3, react-router 8 (da 6: nessun advisory aperto, `npm audit` a zero), Vite 8 con il bundler Rolldown (build da ~6 s a ~0,6 s) e plugin-react 6, TypeScript 7 (il compilatore nativo, typecheck in ~1 s), xterm 6, react-query, codemirror e yaml all'ultima versione. `@types/node` resta su 22 come il Node del build.
- La prima pagina scarica 224 KB compressi invece di 388: l'editor YAML (CodeMirror) finiva nel caricamento iniziale per come erano raggruppati i chunk; ora si carica solo dove serve.
- Immagini: Python 3.14.9 (3.15 non ha ancora wheel musl per pyyaml e frozenlist) e Node 24.22.1 LTS, fissate per digest; docker CLI 29.9.2 e compose 5.6.1. Verificato con un deploy compose e un agent edge reali dall'immagine nuova.
- Nuova pagina "Page not found" per gli indirizzi sconosciuti (prima la pagina restava vuota).

**Interfaccia**
- Migrazione a **shadcn/ui + Tailwind v4** con un kit interno (`src/ui/kit`). Direzione visiva da strumento tecnico: niente raggi, bottoni piatti, un solo font (Geist Mono), viewport piena, fasce invece di card, densità compatta. Vedi `DESIGN.md`.
- **6 temi** (Kowan, Nord, Dracula, Catppuccin, Gruvbox, Tokyo Night), chiaro e scuro, generati da `scripts/gen-themes.py`.
- Shell nuova: selettore dell'host in alto, palette comandi ⌘K, navigazione per scopo; la shell resta montata tra le pagine.
- Dashboard con tabella della flotta, container da guardare (codici di uscita), allarmi aperti, grafici SVG leggeri.
- Nuove pagine: **Templates** (22 app pronte), **Activity** (eventi Docker live), **AI assistants**.

**Funzioni**
- **Aggiornamenti immagini** dei container: badge, controllo, pull e ricreazione.
- **Deployment da Git**: repo, branch, percorso compose, webhook firmati (GitHub/Gitea/GitLab), "Pull & redeploy".
- **Catalogo template** con input e segreti generati.
- **Stream eventi Docker** su tutti gli host (REST + WebSocket).
- **Retention** di audit (90 g), allarmi risolti (30 g), run dei workflow (ultimi 100), configurabile.
- **Aggiornamento di server e agent con rollback automatico** (`kowan/updater`, `docs/updates-design.md`): pull, verifica architettura, smoke test della nuova immagine su una copia del database, snapshot SQLite, swap transazionale eseguito da un helper lanciato dall'immagine attuale, verifica di salute (healthcheck, restart stabili) e ripristino automatico di container e database in caso di errore. Versione precedente conservata per il rollback manuale, cronologia completa, aggiornamento a rotazione degli agent con stop al primo errore. Verificato su Docker reale in tutti gli scenari di errore.
- Spegnimento immediato di server e agent anche con tunnel e WebSocket aperti (prima 20 s e SIGKILL ad ogni aggiornamento).
- **Server MCP integrato** (`POST /mcp`, Streamable HTTP, senza dipendenze nuove): 29 tool filtrati per ruolo, conferma esplicita per le azioni distruttive, audit di ogni azione, risorse e prompt. Ponte stdio `kowanctl mcp` per Claude Desktop. Vedi `docs/mcp.md` e la pagina "AI assistants".

**Prestazioni**
- Primo caricamento da ~2,3 MB a ~650 KB trasferiti; asset precompressi gzip/brotli e cache immutabile; risposte JSON compresse.
- Rimossi axios ed ECharts; editor YAML e terminale caricati solo quando servono; precaricamento al passaggio del mouse; tabelle e log virtualizzati.
- Aggiornamenti delle liste in **push** dagli eventi Docker (polling ridotto a rete di sicurezza).
- Monitor parallelo e statistiche one-shot (ciclo da ~2 s a ~70 ms), cache della lista immagini, log a blocchi, orjson + uvloop, indici SQLite e `synchronous=NORMAL`.

**Sicurezza e correttezza**
- Corretti path traversal verso il Docker Engine e montaggio del filesystem host tramite il browse dei volumi; login con scrypt fuori dall'event loop e tempi costanti; race del primo setup.
- Heartbeat WebSocket su tunnel e stream; chiusura degli stream lato agent alla disconnessione del client.
- Gli errori tipizzati (404, 400, 409) attraversano API e tunnel degli agent invece di diventare tutti 502; le pagine di dettaglio smettono di interrogare oggetti rimossi.
- Una quarantina di bug di UI e backend.

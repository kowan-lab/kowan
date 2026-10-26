# Test

Kowan ha tre livelli di test. Tutti girano in locale con un comando e in CI
(`.github/workflows/ci.yml`) a ogni push e pull request.

| Livello | Cosa prova | Comando | Dove |
|---|---|---|---|
| Backend | API REST, store, auth, mux/tunnel, agent, Docker (Engine finto), CLI, workflow, GitOps, notifiche | `make test` / `make coverage` | `tests/` |
| Frontend | ogni pagina e componente della SPA in jsdom, contro un backend finto | `make test-ui` / `make coverage-ui` | `web/frontend/src/**/__tests__/` |
| End-to-end | la SPA buildata contro un server reale, nel browser | `make e2e` | `web/frontend/e2e/` |

`make test-all` li esegue tutti e tre. `make deps-dev` installa `coverage` e il
browser di Playwright.

## Backend

`unittest` della libreria standard, nessun plugin. Le classi di base:

- `tests/apicase.py::ServerCase` monta l'app completa (middleware, moduli,
  store SQLite in una cartella temporanea) e offre `call(method, path, role=…)`
  e `roles()` per provare ogni endpoint con ogni ruolo.
- `tests/fakeengine.py::FakeEngine` è un Docker Engine in memoria servito su un
  socket unix: container, immagini, volumi, reti, exec, log, stats, eventi,
  pull con digest (`registry`), errori iniettati (`fail`, `fail_once`) e
  container che terminano con un codice (`runners`).
- Finti anche registry, server di secret (Vault, Infisical, Phase), SMTP e
  ricevitori HTTP; `httpx` è sostituito con `MockTransport`.
- L'agent gira davvero: `test_agent_e2e.py` lo collega al server in modalità
  edge e diretta, `test_remote_client.py` ripete la suite del client locale
  attraverso il tunnel.

`make coverage` scrive `coverage.json` e `htmlcov/` e fallisce sotto la soglia
`fail_under` di `.coveragerc` (righe + rami). Sono esclusi solo gli stub
`raise NotImplementedError`, la guardia `if __name__ == "__main__":` e gli
import `if TYPE_CHECKING:`.

## Frontend

Vitest + Testing Library in jsdom. I test montano l'app vera (router, auth,
host, react-query, tema) con `renderApp(path, { role, api })` e la usano come
un utente: click, digitazione, select, dialog, toast.

- `src/test/api.ts` sostituisce `fetch`, `WebSocket` ed `EventSource`. Le rotte
  sono `"METHOD /percorso"` relative a `/api/v1` (`:param`, `*`); un handler è il
  corpo da restituire o una funzione della richiesta, `reply(status, body,
  headers)` per errori e header. Ogni richiesta finisce in `calls`;
  `requests(method, pattern)` le filtra. `socket(part)` restituisce il WebSocket
  aperto dalla pagina per mandargli messaggi.
- `src/test/render.tsx::baseApi` dà a ogni test un backend plausibile
  (`fixtures.ts`, più i cataloghi reali del server in `catalog.json`: tipi di
  credenziali, plugin dei volumi, trigger, regole di allarme, template).
- `src/test/ui.ts` ha gli aiuti per il kit: `dialog`, `choose` (Select),
  `press`, `toast`.
- Dopo ogni test il setup aspetta che le query e le mutation in volo finiscano
  (un toast in ritardo non deve finire nel test successivo) e fallisce se una
  stessa richiesta è partita più di 150 volte: è il segno di un loop di refetch.

`make coverage-ui` fallisce sotto le soglie di `vitest.config.ts`.
`npm run typecheck:test` controlla i tipi dei test e degli spec e2e
(`tsconfig.test.json`; il build dell'app li esclude).

## End-to-end

Playwright (`web/frontend/playwright.config.ts`) avvia `kowan.server` su
`127.0.0.1:18777` con una cartella dati nuova (`e2e/.data`, cancellata a ogni
avvio) e un token di setup fisso, e apre la SPA buildata in `web/dist`
(`make e2e` la builda prima).

- `setup.e2e.ts` fa il primo avvio dalla UI (token sbagliato, poi giusto),
  crea l'admin e salva la sessione per gli altri spec.
- `smoke.e2e.ts` apre ogni pagina e fallisce su errori JavaScript o di console.
- `flows.e2e.ts`: un viewer creato dall'admin vede la console in sola lettura,
  un secret cifrato si rivela e risolve mascherato, un workflow esegue uno
  script e ne mostra l'output, una regola di allarme si crea, disattiva e
  cancella.
- Uno spec per funzione: `history` (revisioni e rollback), `uptime` (controllo
  sul server stesso e pagina di stato pubblica), `logs` (retention e ricerca),
  `previews` (anteprime su un repository `.invalid`, quindi nessuna rete),
  `promotion` (staging → produzione senza deploy), `import` (una cartella
  compose letta dal browser), `digest` (pianificazione e anteprima, senza la
  sezione aggiornamenti che interrogherebbe i registry).
- `mobile.e2e.ts`: a 390 × 844 nessuna pagina scorre di lato, le tabelle
  diventano schede, il menu è un drawer; manifest, icone e service worker,
  e l'avvio offline della console installata.
- `docker.e2e.ts` (solo con `KOWAN_E2E_DOCKER=1`, attivo in CI) deploya un
  container `alpine` chiamato `kowan-e2e-*`, controlla pannello risorse e
  "Keep logs", lo ferma e lo rimuove.

Il server usa il Docker locale: gli spec lo leggono e basta, tranne
`docker.e2e.ts`, che tocca solo il container che crea. Nessuno spec fa prune:
la pagina di pulizia del disco è solo aperta, mai eseguita, e nessuno spec
attiva la pulizia programmata.

## Scrivere un test nuovo

- Un endpoint: un metodo in un `ServerCase`, con `roles()` se cambia per ruolo.
- Una pagina: `renderApp("/percorso", { api: { "GET /…": dati } })`, poi
  interazioni con `user` e asserzioni su `requests(...)` e sul testo visibile.
  Le rotte passate in `api` vincono su quelle di default.
- Un bug: prima il test che lo riproduce, poi la correzione.

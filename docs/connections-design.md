# Connections: typed credentials as capabilities

> Architecture analysis + phased plan for evolving Kowan's freshly-merged
> `credentials` table into a **connection** model — a *typed credential that
> implements one or more capability interfaces*, which per-service configs
> **reference** instead of re-embedding.

---

## 0. The insight, stated precisely

We just merged three near-identical mixins (`registries.py`, `references.py`,
`channels.py`) into one `credentials` table with one encrypt/decrypt/mask path
(`kowan/store/credentials.py`). That merge was correct at the *storage* layer but
it kept a **wrong top-level axis**: the discriminator is
`kind ∈ {registry, secret, channel}` (`kowan/models/credential.py:29-31`), and
sensitivity is resolved per `(kind, type)` pair. In that model a "registry" *is*
a credential; a "channel" *is* a credential.

The deeper observation: the same underlying secret material — one AWS
account, one Docker Hub login, one GitHub PAT, one SMTP server — is **reused
across many services**. In the current model you'd create it three times (as a
registry, as a secret backend, as… nothing, because there's no "AWS" kind at
all — it only exists *inside* an `ecr` registry row, see `kowan/registry/ecr.py:44-55`).

The fix is to make the **connection** the noun, and let features *point at it*:

```
CONNECTION TYPE   ── implements ──▶  CAPABILITY INTERFACE(S)
      (aws)                              (dockerRegistryAuth, secretFetch,
                                          messageSend, objectStore)
                                                   ▲
                                                   │ references (by capability)
SERVICE CONFIG  ───────────────────────────────────┘
 (image-registry) needs dockerRegistryAuth  →  connectionRef: <aws-prod>
                                               + registryUrl / region (on the config)
```

The `kind` axis (`registry`/`secret`/`channel`) is **the thing to delete**. It's
not a property of the credential; it's a property of *how one particular service
consumes it*. AWS is not a registry OR a secret store OR a messenger — it's all
of them at once, decided by which capability the caller asks for.

---

## 1. Core model

### 1.1 Three nouns

| Noun | What it is | Where it lives | Cardinality |
|---|---|---|---|
| **Connection** (a.k.a. typed credential) | The reusable secret material + its type. Owns auth. Encrypted `data`, non-sensitive `meta`. | `connections` table (evolved `credentials` table) | Few. One per real-world account. |
| **Capability** | An interface a connection *implements* — a verb the platform can perform *with* it (`docker_auth()`, `fetch(name)`, `send(msg)`, `put(blob)`). | Code: dispatch registry keyed by capability. Not a row. | Fixed set (~10). |
| **Service config** | A per-feature record that *needs a capability* and *references a connection* by id, plus service-local, non-secret params (region, ARN, repo path, channel id). | Each feature's own table (`registry_targets`, `notify_targets`, `secret_refs`, …) | Many. Thin. |

### 1.2 Why capabilities, not hardcoded type-pairs

Today's `(kind, type)` = `(registry, ecr)` is a **2-tuple that conflates two
independent facts**: *"this is AWS"* and *"I'm using it as a registry"*. That's
why one AWS account can't back both ECR pulls and S3 backups without being
entered twice.

A capability interface splits those apart:

- **Connection type** answers *"what is this credential and how does it prove
  identity?"* → `aws` = SigV4 over an IAM key pair. Decided once, at creation.
- **Capability** answers *"what can I do with a proven identity?"* → `aws`
  *implements* `dockerRegistryAuth` (mint an ECR token), `secretFetch` (read
  Secrets Manager), `messageSend` (SES), `objectStore` (S3). Chosen per call
  site.

The dossier's taxonomy already models AWS this way:
`aws.implements = [cloudIdentity, dockerRegistryAuth, secretFetch, messageSend, objectStore]`.
Hardcoded type-pairs would need `aws-as-registry`, `aws-as-secret-store`,
`aws-as-messenger`, `aws-as-object-store` — four rows, four copies of the same
key pair, four places to rotate. Capabilities give you **one row, four verbs**.

### 1.3 The `cloudIdentity` base capability (the load-bearing subtlety)

Cloud connections (`aws`/`gcp`/`azure`) don't implement the leaf capabilities
directly with static creds — they implement `cloudIdentity` (mint a short-lived
provider session) and *derive* the leaf capabilities from it. This matches the
real code: `ECRProvider` doesn't hold a registry password, it holds an IAM key
pair and calls `GetAuthorizationToken` to *mint* a docker token
(`kowan/registry/ecr.py:57-60`). So:

> A cloud connection implements `cloudIdentity`; the registry/secret/message/
> object capabilities are **facets it can project** from that identity. A
> non-cloud connection (smtp, slack-webhook) implements its leaf capability
> directly.

Dispatch is therefore: *"give me a `dockerRegistryAuth` from connection X"* →
if X implements it directly (docker-login), use it; if X only has
`cloudIdentity`, derive it. The caller never branches on connection type.

---

## 2. Capability interfaces

Each capability is a small async interface. A connection type is registered as
implementing a set of them.

| Capability | One-line contract (the method it exposes) | Consumed by (services in Kowan) |
|---|---|---|
| **`dockerRegistryAuth`** | `docker_auth(registryUrl) -> X-Registry-Auth token` (basic cred or OAuth2/token exchange) | deployment image-pull (`deployment/service.py:280-285`), registry browse (`registry/browse.py`), image build-push (`api/images.py:127-154`), docker-image trigger (`triggers/dockerimage.py:147`) |
| **`secretFetch`** | `fetch(path, key) -> value` (read a named secret/version from an external store) | secret refs / `[[name]]` interpolation (`store/credentials.py:423`), deployment env injection, workflow steps |
| **`messageSend`** | `send(title, body, level) -> None` (deliver a message over a transport) | notify pipeline (`notify/*`), monitor alerts (via event bus), workflow notify-step (`workflow/engine.py:400-421`) |
| **`gitFetch`** | `git_auth(repoUrl) -> creds/transport` (clone/fetch over HTTPS or SSH) | deployment source pull, git-commit trigger (`triggers/gitcommit.py:70-71`), workflow |
| **`gitApi`** | `api_call(...)` (provider REST/GraphQL: webhooks, commit status, releases) | triggers (register webhooks), deployment (report status), workflow |
| **`objectStore`** | `put/get/list(bucket, key, blob)` (blob storage) | backup targets (`backup.py`), deployment artifacts, workflow — **future** |
| **`httpBearer`** | `auth_headers() -> {header: value}` (attach bearer/API token or basic auth to arbitrary outbound HTTP) | workflow http-step (`workflow/engine.py:470-526`), webhook notifier (`notify/webhook.py`), ntfy, generic integrations |
| **`sshAccess`** | `ssh_session(host) -> connection` (key/password SSH for exec, tunnel, git-over-ssh) | sshfs volume plugin (`models/volumeplugin.py:76-87`), host reach, `gitFetch` transport — **future** |
| **`tlsClientAuth`** | `client_cert() -> (cert, key, ca)` (present X.509 client cert for mTLS) | TLS Docker daemon over TCP, mTLS registries/APIs — **future** |
| **`cloudIdentity`** | `session() -> short-lived provider session` (STS/SA/service-principal) | *base only* — not consumed by a feature directly; the derivation source for the four cloud-facet capabilities above |

**Design rule:** `httpBearer` is the escape hatch. Any connection whose auth
reduces to "put this header on the request" implements it, so a workflow HTTP
step can reference *any* token-bearing connection without a dedicated capability.

---

## 3. Connection / credential type catalog

Sensitive fields marked **🔒**. "Impl" = capabilities the type implements.

| Type | Family | Implements | Fields | Concrete Kowan use today |
|---|---|---|---|---|
| **aws** | cloud | cloudIdentity → dockerRegistryAuth, secretFetch, messageSend, objectStore | region, accessKeyId **🔒**, secretAccessKey **🔒**, sessionToken **🔒**, roleArn, externalId | ECR pulls (`registry/ecr.py`); S3 volume plugin (`models/volumeplugin.py:68-74`); *future* S3 backup, Secrets Manager, SES |
| **gcp** | cloud | cloudIdentity → dockerRegistryAuth, secretFetch, messageSend, objectStore | serviceAccountJson **🔒**, projectId, region | Artifact/GCR pulls (`registry/gcp.py`); *future* GCS backup, Secret Manager |
| **azure** | cloud | cloudIdentity → dockerRegistryAuth, secretFetch, messageSend, objectStore | tenantId, clientId, clientSecret **🔒**, subscriptionId | ACR pulls (`registry/acr.py`); *future* Key Vault, Blob backup |
| **docker-registry-login** | registry | dockerRegistryAuth, httpBearer | registryUrl, username, password **🔒** | Generic v2 registry basic auth (`registry/basic.py:112-180`) — Harbor, Quay, self-hosted |
| **dockerhub** | registry | dockerRegistryAuth | username, accessToken **🔒** | Docker Hub private repos / rate-limit lift |
| **ghcr-token** | registry | dockerRegistryAuth, httpBearer | username, githubToken **🔒** | ghcr.io (`registry/basic.py` ghcr type) |
| **github** | git | gitFetch, gitApi, httpBearer, **dockerRegistryAuth** | token **🔒**, appId, appPrivateKey **🔒**, apiBaseUrl | git-commit trigger (`triggers/gitcommit.py`) **and** ghcr.io with the *same* PAT |
| **gitlab** | git | gitFetch, gitApi, httpBearer, dockerRegistryAuth | token **🔒**, apiBaseUrl | git clone + GitLab Container Registry |
| **bitbucket** | git | gitFetch, gitApi, httpBearer | username, appPassword **🔒**, apiBaseUrl | git clone + build status |
| **git-basic** | git | gitFetch, httpBearer | repoUrl/hostGlob, username, password **🔒** | generic git-over-HTTPS with no provider API |
| **ssh-key** | git | sshAccess, gitFetch | privateKey **🔒**, passphrase **🔒**, knownHosts, user | git-over-ssh deploy key; sshfs mount (`models/volumeplugin.py:76`) |
| **vault** | secret-store | secretFetch, httpBearer | address, authMethod, token **🔒** (or roleId+secretId **🔒**), namespace, mount | `secrets/vault.py` KV v2 |
| **infisical** | secret-store | secretFetch, httpBearer | apiUrl, clientId, clientSecret **🔒**, projectId, environment | `secrets/infisical.py` |
| **phase** | secret-store | secretFetch, httpBearer | host, appToken **🔒**, appId, environment | `secrets/phase.py` |
| **smtp** | messaging | messageSend | host, port, username, password **🔒**, fromAddress, tls | `notify/smtp.py` |
| **slack-webhook** | messaging | messageSend | webhookUrl **🔒**, defaultChannel | `notify/slack.py` |
| **discord-webhook** | messaging | messageSend | webhookUrl **🔒**, username, avatarUrl | `notify/discord.py` |
| **telegram** | messaging | messageSend | botToken **🔒**, chatId | `notify/telegram.py` |
| **ntfy** | messaging | messageSend, httpBearer | serverUrl, topic, token **🔒** | `notify/ntfy.py` |
| **api-token** | generic | httpBearer | baseUrl/hostGlob, token **🔒**, header, scheme | workflow http-step, custom notifier |
| **basic-auth** | generic | httpBearer, dockerRegistryAuth | baseUrl/hostGlob, username, password **🔒** | HTTP-basic endpoint; registry that takes plain basic |
| **tls-client-cert** | transport | tlsClientAuth | clientCert **🔒**, clientKey **🔒**, caCert, hostGlob | mTLS Docker daemon / registry — **future** |

**Two multi-capability stars** — the whole argument in two rows: `aws`
(4 leaf capabilities from one key pair) and `github` (gitFetch + gitApi +
dockerRegistryAuth from one PAT — the dossier notes "often the same PAT used for
GHCR", `providers.git-commit.shareable`).

---

## 4. Service catalog — who needs which capability

| Service (Kowan) | Capability required | Site | Non-secret config it keeps locally |
|---|---|---|---|
| Image registry (pull) | `dockerRegistryAuth` | `deployment/service.py:280-285` | registryUrl, region (ECR), insecure flag |
| Registry browse | `dockerRegistryAuth` | `registry/browse.py:58-148` | registryUrl |
| Image build-push | `dockerRegistryAuth` | `api/images.py:127-154` | push target ref |
| Docker-image trigger | `dockerRegistryAuth` | `triggers/dockerimage.py:61-172` | image ref, tag glob, poll interval |
| Secret reference `[[name]]` | `secretFetch` | `store/credentials.py:423` | **mount/path/key** (moves off the connection!) |
| Deploy env injection | `secretFetch` | `deployment/*`, `workflow/poller.py:130` | which env var → which secret path |
| Notification channel | `messageSend` | `notify/*` | minLevel, enabled, target chatId/channel |
| Workflow notify-step | `messageSend` | `workflow/engine.py:400-421` | message template |
| Git-commit trigger | `gitFetch` (+ `gitApi` later) | `triggers/gitcommit.py:38-75` | repo, branch, poll interval |
| Git source pull (deploy) | `gitFetch` | deployment | repo, ref, sub-path |
| Workflow HTTP step | `httpBearer` | `workflow/engine.py:470-526` | url, method, body template |
| Webhook notifier | `httpBearer` + `messageSend` | `notify/webhook.py:43-50` | url, static headers shape |
| ntfy notifier | `messageSend` (+`httpBearer`) | `notify/ntfy.py:33-49` | server, topic |
| Backup target | `objectStore` **(future)** | `backup.py` | bucket, prefix |
| sshfs volume mount | `sshAccess` **(future)** | `models/volumeplugin.py:76-87` | remote path, port |
| s3fs volume mount | `objectStore`/`cloudIdentity` **(future)** | `models/volumeplugin.py:68-74` | bucket, endpoint, region |
| TLS Docker daemon | `tlsClientAuth` **(future)** | host connect | daemon TCP addr |

---

## 5. The compatibility matrix

Rows = services. Columns = connection types. **✓** = the pairing works because
they share a capability (named in the cell). **—** = no shared capability.

| Service ↓ / Type → | aws | gcp | azure | docker-login | ghcr-token | dockerhub | github | gitlab | bitbucket | git-basic | ssh-key | smtp | vault | infisical | phase | api-token | basic-auth | slack/discord webhook |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| **image-registry (pull)** | ✓ ECR | ✓ AR | ✓ ACR | ✓ basic | ✓ ghcr | ✓ hub | ✓ ghcr | ✓ GLCR | — | — | — | — | — | — | — | — | ✓ basic | — |
| **secret-reference** | ✓ SM/SSM | ✓ SM | ✓ KV | — | — | — | — | — | — | — | — | — | ✓ | ✓ | ✓ | — | — | — |
| **notification-channel** | ✓ SES/SNS | ✓ PubSub | ✓ SB | — | — | — | — | — | — | — | — | ✓ | — | — | — | — | — | ✓ |
| **git-source / trigger** | — | — | — | — | — | — | ✓ | ✓ | ✓ | ✓ | ✓ ssh | — | — | — | — | — | — | — |
| **workflow-http** | ✓* | ✓* | ✓* | ✓ hdr | ✓ hdr | — | ✓ hdr | ✓ hdr | ✓ hdr | ✓ hdr | — | — | ✓ hdr | ✓ hdr | ✓ hdr | ✓ | ✓ | — |
| **backup-target** *(future)* | ✓ S3 | ✓ GCS | ✓ Blob | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |
| **messaging** *(future)* | ✓ | ✓ | ✓ | — | — | — | — | — | — | — | — | ✓ | — | — | — | — | — | ✓ |
| **object-store** *(future)* | ✓ | ✓ | ✓ | — | — | — | — | — | — | — | — | — | — | — | — | — | — | — |

Shared capability per column-band:
- **cloud (aws/gcp/azure):** everything flows from `cloudIdentity` — the ✓s in
  registry / secret / notify / backup rows are all *derived facets*, one credential.
- **registry types:** `dockerRegistryAuth` (+ `httpBearer` → workflow-http via a
  bearer header).
- **git types:** `gitFetch`/`gitApi`; `github`/`gitlab` also carry
  `dockerRegistryAuth` (ghcr/GLCR ✓ in the registry row) — the cross-family win.
- **secret-store types:** `secretFetch` (+ `httpBearer`).
- **generic (api-token/basic-auth):** `httpBearer` puts a ✓ in workflow-http for
  *any* token; `basic-auth` also does `dockerRegistryAuth`.

`✓*` (cloud × workflow-http) = works only if the connection surfaces a signed
request / token via `httpBearer`; static IAM keys aren't a bearer header, so
treat cloud→arbitrary-HTTP as a weak pairing, not a first-class one.

**The three cross-family cells are the payoff:** `github`→registry (one PAT for
clone + ghcr), `aws`→{registry, secret, notify, backup} (one key pair, four
services), `basic-auth`→registry (generic basic login reused as an HTTP creds).
None of these are expressible in today's `(kind, type)` model.

---

## 6. Relationship to the just-built merge

The merge did real, keepable work. Be honest about what survives and what moves.

### 6.1 Carries over unchanged (the merge was worth it)

| Asset | Where | Fate |
|---|---|---|
| One `credentials` table, one row shape `(id, type, name, meta, data, …)` | `store/credentials.py:8` | Becomes the **connections** table. Drop the `kind` column (§6.2). |
| AES-256-GCM sealed `data`, "only field NAMES leak" | `models/credential.py:13-16`, `store/credentials.py` | **Kept verbatim.** This is the crown jewel — connections need exactly this. |
| Mask-on-read / preserve-blank-on-update / merge_config | `store/_common.py` (`mask_config`, `merge_config`, `config_keys`) | Kept. Now keyed by connection `type` alone, not `(kind, type)`. |
| Injected sensitivity resolver (avoids import cycle store→providers) | `store/credentials.py:14-17` (`set_credential_sensitivity`) | Kept, re-keyed to `type` → capability field-specs. |
| The single "Credentials" screen | frontend | Becomes the **Connections** screen (list + create/edit + validate). |
| Reveal / validation-status plumbing | `update_credential_status` | Kept per-connection. |

### 6.2 Gets reshaped

| Today | Becomes |
|---|---|
| `kind ∈ {registry, secret, channel}` top-level discriminator (`models/credential.py:29-31`) | **Deleted.** Replaced by *capabilities the type implements*. A connection is no longer "a registry" — it *has* `dockerRegistryAuth`. |
| Sensitivity keyed by `(kind, type)` | Keyed by `type` → its field-spec (capabilities don't change which fields are secret). |
| **A registry row IS the credential** (`registry_view` reconstructs a `Registry` from the credential, `store/credentials.py:339-345`) | A registry *target* is a thin config `{registryUrl, region, connectionRef}`; `registry_view` becomes `registry_view(target)` = load target + resolve `connectionRef` → `dockerRegistryAuth`. |
| **`VaultConfig` bundles connection AND reference** — `address`+`token` (connection) *plus* `mount`+`path`+`key` (which secret) in one encrypted map (`secrets/vault.py:29-39`) | **Split.** `address`+`token` → a `vault` connection. `mount`/`path`/`key` → the secret-ref config that carries `connectionRef` + `secretFetch`. One Vault connection, N references. |
| `credential_config(id)` feeds notify builder with a flat dict | Notify target config `{minLevel, enabled, chatId, connectionRef}`; builder resolves `connectionRef` → `messageSend`. |

### 6.3 Reusable connections vs genuinely single-use secrets — **keep them separate**

Not everything in the merged table is a reusable connection. The dossier flags a
distinct family: **interpolation secrets** — the `[[name]]` / `[[name.field]]`
static values resolved by `store.resolve_secrets` (`store/credentials.py:423`,
`_SECRET_TOKEN_RE` at line 85), used in trigger config
(`workflow/poller.py:130-146`), workflow script env
(`workflow/engine.py:304-309`), and HTTP headers (`workflow/engine.py:493-504`).

A static `[[db-password]]` = `"hunter2"` is **not a connection** — it implements
no capability, it's just an encrypted string with a name. Recommendation:

> **Keep interpolation-secrets as their own first-class concept**, not folded
> into connections. Two clean primitives:
> - **Connection** — typed, capability-bearing, referenced by service configs.
> - **Secret** — a named encrypted value (or a `secretFetch` *pointer* into a
>   secret-store connection), consumed by `[[name]]` interpolation.

The bridge: an *external* secret (vault/infisical/phase) is a **secret-ref that
delegates to a `secretFetch` connection** — the ref holds `{path, key,
connectionRef}`, the connection holds `{address, token}`. A *static* secret holds
its own value inline. Same `[[name]]` surface, two backing shapes. This preserves
today's behaviour (external backends resolved live via the injected resolver,
`store/credentials.py:26-28`) while unbundling the connection.

Likewise genuinely single-use items stay single-use, **not** connections:
one-off notify webhook URLs where the URL *is* the whole secret (slack/discord —
though a reused webhook *can* be promoted to a `slack-webhook` connection),
workflow public run tokens (`api/workflows.py:208`), API keys (`auth/apikey.py`),
and pure infra keys (`KOWAN_SECRET_KEY`, `KOWAN_JWT_SECRET`, TLS certs — dossier
"internal infra"). These never leave their home; don't drag them into the model.

---

## 7. Edge cases & decisions

**Inline vs reference.** Support both, default to reference. Every service form
gets a connection-picker plus an "inline" escape hatch that mints a hidden,
single-use connection behind the scenes (so the storage path is uniform). Rule:
*if you name it, it's reusable; if you inline it, it's private to this config.*
This keeps the one-off webhook ergonomic without a second code path.

**AWS assume-role vs static keys.** Both are the same `aws` connection type;
`roleArn`+`externalId` are optional fields (dossier `aws.fields`). `cloudIdentity`
resolution branches internally: keys present + no role → sign directly (today's
`ecr.py` path); role present → `sts:AssumeRole` first, then sign with the
session. Callers still just ask for `dockerRegistryAuth`. Never a new type.

**One credential, many capabilities.** This is the *normal* case, not an edge
case — it's the whole point. Dispatch = `connection.capability(X)`: look up
whether `type` implements X directly or via `cloudIdentity` derivation. A `github`
connection answers both `gitFetch` (trigger) and `dockerRegistryAuth` (ghcr pull)
from one `token`. Validation is **per-capability** ("this AWS key can reach ECR
✓ but S3 ✗") — store a status map, not a single `verified` bool.

**Region / ARN / mount / path live on the service config, NOT the connection.**
Hard rule. The connection is *"who am I"*; the config is *"what am I touching"*.
- ECR: `region` and the account registry URL → registry target. (Today `region`
  wrongly sits inside the ecr credential, `registry/ecr.py:45` — this moves.)
- Vault: `mount`/`path`/`key` → secret ref (§6.2). `address`/`token` →
  connection.
- Telegram: `chatId` → notify target; `botToken` → connection.

  Exception: a field that is *intrinsic to the identity* stays on the connection
  (GCP `projectId` scopes the SA; Azure `tenantId` scopes the principal).

**Migration from embedded model.** Mechanical and per-kind (§8 phase 6). Each
existing `credentials` row splits into (1) a `connection` (the auth fields) + (2)
a thin service config pointing at it (the reference/target fields). Dedup on
identical `data` so three ECR registries sharing one key pair collapse to one
`aws` connection + three targets.

**Backward-compat.** Keep the `[[name]]` surface byte-for-byte (regex, sources,
preview statuses at `store/credentials.py:76-85`) — user compose files and
workflows must not break. Keep `registry_view`/`credential_config` as **shim
adapters** over the new store during transition so consumers (`deployment`,
`notify`, `browse`) compile unchanged until Phase 3 rewires them.

---

## 8. Phased implementation plan

Incremental, each phase independently shippable and reversible. No big-bang.

### Phase 0 — Capability contracts (no storage change)
Define the ~10 capability interfaces (§2) as ABCs/Protocols in `kowan/capabilities/`.
Register `type → {capability: impl}`. Refactor **existing** providers to declare
what they implement (ECR/GCP/ACR/basic already *are* `dockerRegistryAuth` impls;
vault/infisical/phase *are* `secretFetch`; notify *are* `messageSend`). Pure
adapter layer over today's code — zero behaviour change, fully testable.

### Phase 1 — `connections` store (parallel to `credentials`)
Add a `connections` table = `credentials` shape **minus `kind`**, plus a
per-`type` capability manifest and a per-capability status map. Reuse the AES-GCM
seal / mask / merge path verbatim (§6.1). Inject the sensitivity resolver keyed
by `type`. Ship the store + tests; nothing consumes it yet.

### Phase 2 — Capability dispatch
Implement `resolve(connectionRef, capability) -> impl`, including
`cloudIdentity`→facet derivation for `aws`/`gcp`/`azure`. Per-capability
`validate()`. This is where "one AWS cred → 4 verbs" becomes real. Unit-test the
derivation matrix (§5) directly.

### Phase 3 — Rewire consumers to references (one at a time, behind shims)
For each service, add an optional `connectionRef` to its config and resolve the
capability through Phase 2, keeping the old embedded path as fallback:
1. **image-registry** — target = `{registryUrl, region, connectionRef}`;
   `registry_view` resolves `dockerRegistryAuth`. (`deployment/service.py:280`,
   `api/images.py:127`, `triggers/dockerimage.py:147`)
2. **secret-reference** — ref = `{path, key, connectionRef}` → `secretFetch`;
   unbundle `VaultConfig` (§6.2).
3. **notification-channel** — target = `{minLevel, enabled, target, connectionRef}`
   → `messageSend`. (`notify/*`, `workflow/engine.py:400`)
4. **git-trigger / source** — config = `{repo, branch, connectionRef}` →
   `gitFetch`. (`triggers/gitcommit.py:70`)
5. **workflow-http** — optional `connectionRef` → `httpBearer` header injection,
   coexisting with `[[secret]]` interpolation. (`workflow/engine.py:493`)

Each sub-step is its own PR; the shim keeps everything green.

### Phase 4 — UI: Connections screen + pickers
Rename the merged "Credentials" screen to **Connections**; list shows type,
implemented-capabilities badges, and per-capability validation status. Add a
**connection-picker** control (filtered *by the capability the form needs*) to
each service form — the registry form only offers `dockerRegistryAuth`
connections, the notify form only `messageSend`, etc. Include the inline escape
hatch (§7).

### Phase 5 — New capabilities (unlock the future rows)
Now that dispatch exists, land the features the matrix promised: `objectStore`
→ backup targets (`backup.py`) + s3fs plugin; `sshAccess` → sshfs plugin +
host reach; `tlsClientAuth` → TCP+TLS Docker daemon; `gitApi` →
webhook registration / commit status. Each is "add a `type`, implement one
capability" — no core changes.

### Phase 6 — Migrate & retire the old path
Backfill: split each `credentials` row into connection + thin config, dedup
identical `data` (§7). Flip consumers off the shim. Drop the `kind` column and
the `registry_view`/`credential_config` adapters. Keep static interpolation
secrets in their own store (§6.3) — they were never connections.

---

### One-paragraph summary for the commit message

> The credentials merge unified *storage*; this unifies *identity*. We stop
> modeling `registry`/`secret`/`channel` as kinds of credential and start
> modeling **connections** — typed credentials that implement capability
> interfaces (`dockerRegistryAuth`, `secretFetch`, `messageSend`, `gitFetch`,
> `objectStore`, `httpBearer`, `sshAccess`, `tlsClientAuth`) derived, for cloud
> types, from a `cloudIdentity` base. Services stop *being* credentials and start
> *referencing* one by capability, keeping only their non-secret params
> (region, ARN, mount, chatId) locally. One AWS key pair backs ECR, Secrets
> Manager, SES and S3; one GitHub PAT backs clone and ghcr — expressible for the
> first time. The AES-GCM seal, masking and single admin screen from the merge
> carry over intact; static `[[name]]` interpolation secrets stay their own
> concept.

---

## 9. Realization notes (what was actually built)

This section records where the implementation diverged from the plan above and
why — kept honest so the doc matches the code.

### 9.1 `kind` was **kept**, not deleted (pragmatic vs §6.2)

The plan called for dropping the `kind` column and splitting into per-target
tables. The implementation instead **reused the just-built `credentials` table**
and added `connection` as a fourth `kind` value, with services carrying an
optional `meta.connectionRef`. This delivers the same user-facing value (one AWS
credential backing ECR + Secrets Manager + …, proven end-to-end) while reusing the
entire store/mask/merge/API/UI surface — far less churn and risk than a
table-split for an identical outcome. The capability layer (`kowan/capabilities.py`)
is the real load-bearing abstraction; whether a row's discriminator is a `kind`
column or a table name is incidental to it.

### 9.2 Capability dispatch — done (Phases 0/2)

`kowan/capabilities.py` (manifest: type → capabilities, with `cloudIdentity`
derivation) and `kowan/connections.py` (`build_capability` / `resolve` /
`resolve_service`) implement the caller-asks-for-a-verb dispatch. Builders exist
for `dockerRegistryAuth`, `secretFetch`, `messageSend`, `gitFetch`, `httpBearer`,
reusing the registry/secrets/notify providers. `objectStore` / `sshAccess` /
`tlsClientAuth` / `gitApi` are **declared capability names with no builder yet** —
honest slots, not fakes: `build_capability` raises `CapabilityError` for them, and
no type advertises a capability it can't satisfy (e.g. the git types deliberately
do *not* claim `gitApi`). Wiring those needs real external integrations (S3/SSH
transport, an mTLS client, git-provider REST APIs).

### 9.3 Consumers rewired via `resolve_service` (Phase 3)

- **Registry** pull auth *and* validation (`kowan/registry/resolve.py`,
  `_registry_validate_and_persist`) resolve through the capability layer, so a
  registry delegating to a shared connection uses the connection's creds.
- **Notify** (`notifier.py`, workflow notify step) sends through
  `resolve_service(..., messageSend)`.
- **External secrets** (`vault`/`infisical`/`phase`): a secret can now delegate its
  backend to a shared connection via `meta.connectionRef`. The store's
  `_load_resolved_secret` merges the connection's reusable backend (address/token)
  under the secret's own selectors (mount/path/key) and builds the provider from
  the connection's type — **many secret refs → one Vault**. The standalone inline
  path is unchanged.
- **Git-commit trigger**: reaches connections *transparently* — the poller already
  resolves `[[secret]]` references in a trigger's config before evaluating
  (`workflow/poller.py`), so a `token = [[gh]]` where `gh` is a secret delegating to
  a git connection authenticates the clone with no trigger-specific plumbing.

### 9.4 Native connection types (Phase 5, realizable slice)

`git-http` / `github` / `gitlab` / `bitbucket` (gitFetch + httpBearer),
`api-token` and `basic-auth` (httpBearer) are first-class connection types with
their own field schemas in `kowan/connection_types.py` (the git hosts have no
pre-existing provider family, so their schema/sensitivity lives here). `dockerhub`
needs no new type — it is a `generic` registry connection.

### 9.5 Phase 6 — stance: additive, no forced migration; dedup out of scope

The connection model is **additive and backward-compatible**: every existing
embedded credential keeps resolving through its inline path (covered by tests),
and adoption is opt-in (create a connection, point services at it via
`connectionRef`). There is therefore **no forced migration** to run. **Automatic
dedup** (scanning for credentials with identical secret material and collapsing
them into a shared connection) is a **deliberate non-goal**: it mutates user data
on a heuristic (same password ≠ same identity) and is exactly the kind of
irreversible guesswork to avoid. If ever wanted, it should be a read-only
*suggestion* surfaced in the UI, never an automatic rewrite.

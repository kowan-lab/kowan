# Single sign-on

Kowan signs users in with **OpenID Connect** (Keycloak, Authentik, Authelia,
Microsoft Entra ID, Google, Dex, …) and with **GitHub** (OAuth2 + the user API,
since GitHub is not an OIDC provider). No third-party auth libraries: discovery,
JWKS, PKCE and ID-token validation are implemented on aiohttp's client (`kowan/httpcap.py`) + `cryptography`
in `kowan/authn/`.

## Set-up

1. Set `KOWAN_EXTERNAL_URL` to the URL users open Kowan at, e.g.
   `https://kowan.example.com`. The redirect URI is
   `<KOWAN_EXTERNAL_URL>/api/v1/auth/sso/<provider id>/callback` (shown, with a
   copy button, in *Admin → Single sign-on*).
2. Register a confidential client at the IdP with that redirect URI
   (authorization code flow, PKCE S256 allowed).
3. Add the provider in *Admin → Single sign-on* (issuer URL, client id/secret,
   scopes, button label, role/team mappings) and press **Test**.

The client secret is stored encrypted with the server's secrets cipher
(AES-256-GCM) and never returned by the API (`********`).

### Provider notes

| provider | issuer | notes |
|---|---|---|
| Keycloak | `https://kc/realms/<realm>` | add a *groups* mapper; realm roles via claim `realm_access.roles` |
| Authentik | `https://ak/application/o/<slug>/` | groups in `groups` |
| Authelia | `https://auth.example.com` | `require_pkce: true`; scope `groups` |
| Entra ID | `https://login.microsoftonline.com/<tenant>/v2.0` | the multi-tenant `{tenantid}` issuer template is supported; enable the groups claim |
| Google | `https://accounts.google.com` | no groups: use `allowedDomains` |
| GitHub | – | type `github`; groups are org logins and `org/team-slug` (scope `read:org`); GHE via `config.baseUrl` / `config.apiUrl` |
| Dex | `http://localhost:5556/dex` | `http://` issuers are only accepted for localhost |

## Flow and security

```
browser -> GET /api/v1/auth/sso/{id}/start?returnTo=/path
           302 to IdP (state, nonce, PKCE S256 challenge)
           Set-Cookie kowan_sso_state=<AES-GCM sealed {state, nonce, verifier, returnTo, exp}>
                      HttpOnly; SameSite=Lax; Path=/api/v1/auth/sso; Max-Age=600 (+Secure on https)
IdP     -> GET /api/v1/auth/sso/{id}/callback?code&state
           state compared in constant time with the cookie; code exchanged with
           the verifier (client_secret_basic, or _post when the IdP requires it);
           ID token validated; user provisioned/linked
           302 /login/sso + Set-Cookie kowan_sso_ticket=<one-time, 60 s, HttpOnly>
SPA     -> POST /api/v1/auth/sso/exchange  (cookie) -> {token, user, returnTo}
```

* No token ever appears in a URL; only the OAuth `code`/`state` do.
* ID token: asymmetric algorithms only (RS/PS/ES 256-512, EdDSA; `none` and
  HS* are rejected), signature via the JWKS, `iss` (equal to the discovery
  issuer, itself equal to the configured one), `aud` contains the client id
  (`azp` checked when present / several audiences), `exp`/`iat`/`nbf` with 60 s
  of clock skew, `nonce` (constant time).
* JWKS is cached for an hour and refreshed on an unknown `kid` (key rotation),
  at most once per 60 s.
* `returnTo` is restricted to same-origin absolute paths.
* Errors redirect to `/login?sso_error=<code>` with a short reason code.

## Users

* **Just-in-time provisioning** (`allowSignup`, default on): a new user gets the
  mapped role (`defaultRole` + `roleMappings`), no password, `authSource =
  sso:<provider name>`.
* **Linking** (`linkByEmail`, default on): an existing user whose email equals
  the IdP's **verified** email is linked. Unverified emails are never stored or
  linked. `trustEmail` treats the email as verified for IdPs that omit
  `email_verified`.
* `allowedDomains`: only verified emails in these domains may sign in.
* On every sign-in SSO-provisioned users get their role re-synced from
  `roleMappings` (linked local accounts keep their role) and team memberships
  are re-synced from `teamMappings` (manually added memberships are untouched).

Mapping rules: `{"claim": "groups", "value": "kowan-admins", "role": "admin"}`
and `{"claim": "groups", "value": "web", "team": "<team id or name>"}`; `claim`
is a dot path (`realm_access.roles`); the highest matching role wins.

## SSO-only mode

*Admin → Single sign-on → SSO only* disables password sign-in for everyone but
one **break-glass** local admin (default `KOWAN_ADMIN_USER`). The login page then
shows only the provider buttons, with a small link to the break-glass form. It
can only be enabled when an enabled provider and a valid break-glass account
exist.

## Environment bootstrap

```bash
KOWAN_EXTERNAL_URL=https://kowan.example.com
KOWAN_SSO_PROVIDERS='[{"name":"kc","type":"oidc","label":"Keycloak",
  "issuer":"https://kc.example.com/realms/main","clientId":"kowan",
  "clientSecretEnv":"KC_SECRET","scopes":"openid profile email",
  "config":{"defaultRole":"viewer",
            "roleMappings":[{"claim":"groups","value":"kowan-admins","role":"admin"}]}}]'
KC_SECRET=...
KOWAN_SSO_ONLY=false
KOWAN_SSO_BREAK_GLASS_USER=admin
```

Providers are upserted by `name` at startup and marked `managed: env`.

## REST

| method | path | |
|---|---|---|
| GET | `/api/v1/auth/sso/providers` | public: enabled providers + `ssoOnly` |
| GET | `/api/v1/auth/sso/{id}/start` | public |
| GET | `/api/v1/auth/sso/{id}/callback` | public |
| POST | `/api/v1/auth/sso/exchange` | public (ticket cookie) |
| GET/POST | `/api/v1/sso/providers` | admin |
| GET/PUT/DELETE | `/api/v1/sso/providers/{id}` | admin |
| POST | `/api/v1/sso/providers/{id}/test` | admin: discovery + JWKS |
| GET/PUT | `/api/v1/sso/settings` | admin: `{ssoOnly, breakGlassUser}` |

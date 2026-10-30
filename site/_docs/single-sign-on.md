---
title: Single sign-on
description: Let people sign in to Kowan with OpenID Connect (Keycloak, Authentik, Entra ID, Google, Dex) or GitHub, map groups to roles and teams, and require SSO.
---

Single sign-on lets people sign in to Kowan with your identity provider instead of a Kowan password. Kowan works with any OpenID Connect provider and with GitHub, creates accounts at first sign-in, and maps groups to [roles and teams](users-and-roles.md).

## Before you start

Set `KOWAN_EXTERNAL_URL` to the address people open Kowan at, for example `https://kowan.example.com`, and restart the server. Kowan builds the redirect URI from it:

```text
<KOWAN_EXTERNAL_URL>/api/v1/auth/sso/<provider id>/callback
```

Serve Kowan over HTTPS. When the external URL starts with `https://`, the sign-in cookies are marked secure. The issuer URL of an OpenID Connect provider must use HTTPS too (plain HTTP is accepted only for `localhost`).

## Add a provider

1. At your identity provider, create a confidential client (an OAuth application) that uses the authorization code flow. Note its client ID and client secret.
2. In Kowan, open **Settings → Single sign-on** and click **New provider**.
3. Pick your **Provider**. Kowan fills in an example issuer URL and shows setup hints for it.
4. Fill in the **Button label** (shown as "Continue with …"), the **Name** (a short id), the **Issuer URL**, the **Client ID** and the **Client secret**.
5. Choose the **Default role**, add [mappings](#map-groups-to-roles-and-teams) if you need them, leave **Enabled** on and click **Save**.
6. Click the provider's row to open it again, copy the **Redirect URI** and add it to the client at your identity provider.
7. Click **Test** on the provider's row. For OpenID Connect, Kowan reads the provider's discovery document and signing keys and reports what it found.
8. Sign out: the sign-in page now shows a **Continue with …** button.

The client secret is stored encrypted and never returned by the API.

### Provider notes

| Provider | Issuer URL | Notes |
|---|---|---|
| Keycloak | `https://keycloak.example.com/realms/<realm>` | Turn client authentication on. Add a `groups` mapper for group claims; realm roles are in the claim `realm_access.roles`. |
| Authentik | `https://authentik.example.com/application/o/<slug>/` | Use a confidential OAuth2/OpenID provider. Groups arrive in the `groups` claim. |
| Authelia | `https://auth.example.com` | Set `require_pkce: true` on the client and add the `groups` scope. |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant-id>/v2.0` | Use the web platform redirect URI and enable the groups claim in **Token configuration**. |
| Google | `https://accounts.google.com` | Create an OAuth client of type Web application. Google sends no groups: restrict sign-in with **Allowed email domains**. |
| GitHub | None | Create an OAuth App, not a GitHub App. Groups are your organizations and `org/team-slug` (needs the `read:org` scope). |
| Dex or other | Your issuer | Any provider that publishes `/.well-known/openid-configuration`. |

For GitHub Enterprise Server, set `baseUrl` and `apiUrl` in the provider's `config` through the API or [the environment](#configure-providers-from-the-environment).

## Provider settings

| Setting | Default | What it does |
|---|---|---|
| **Scopes** | `openid profile email` (GitHub: `read:user user:email read:org`) | Space separated. Leave empty for the default. |
| **Default role** | Viewer | Role of new users when no role mapping matches. |
| **Role mappings** | None | Give a role to users whose claim contains a value. |
| **Team mappings** | None | Add users whose claim contains a value to a team. |
| **Allowed email domains** | None | Only verified emails in these domains may sign in. |
| **Create accounts on first sign-in** | On | Create a Kowan user for a new identity. |
| **Link existing accounts by verified email** | On | Link a new identity to the Kowan user with the same email. |
| **Treat email as verified** | Off | For OpenID Connect providers that verify emails but omit `email_verified`. |
| **Always call userinfo** | Off | Merge claims from the provider's userinfo endpoint, for groups missing from the ID token. |

## Map groups to roles and teams

Each mapping has a **Claim** (`groups` by default), a **Value** and the role or team it grants. The claim is a dot path, so `realm_access.roles` reads Keycloak realm roles.

- **Role mappings:** when several match, the highest role wins.
- **Team mappings:** every matching mapping applies. Kowan re-syncs these memberships at every sign-in; teams you add by hand are never removed.

For users that single sign-on created, Kowan re-applies the role mappings at every sign-in, so a change in your identity provider takes effect at the next sign-in. Existing local accounts linked by email keep the role you gave them, so the provider can never demote your break-glass admin.

## How accounts are matched

When someone signs in, Kowan looks for their account in this order:

1. An account already linked to this identity.
2. A user whose **Email** matches the provider's verified email, when **Link existing accounts by verified email** is on.
3. A new account, when **Create accounts on first sign-in** is on. It gets a username from the provider, the mapped role and no password. Its **Source** in **Settings → Users** is `sso:<provider>`.

If none applies, the sign-in is refused. Kowan never stores or links an email the provider has not verified.

Deleting a provider removes its identity links. Users it created keep their accounts but can no longer sign in through it.

## Require single sign-on

The **Password sign-in** card in **Settings → Single sign-on** turns off passwords for everyone except one account.

**Break-glass account**
:   A local admin with a password who can always sign in with it, for example when the identity provider is down. The default is the `KOWAN_ADMIN_USER` account (`admin`).

**SSO only**
:   Hides the password form on the sign-in page and refuses password sign-in for every other account. The sign-in page keeps a small link to the break-glass form.

You can turn on **SSO only** once at least one provider is enabled and the break-glass account is a local admin with a password.

## Configure providers from the environment

To set up single sign-on without the console, for example in a provisioning script:

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

| Variable | What it does |
|---|---|
| `KOWAN_SSO_PROVIDERS` | A JSON array of providers. `clientSecretEnv` names another variable that holds the secret. `type` is `oidc` or `github`. |
| `KOWAN_SSO_ONLY` | `true` or `false`: sets **SSO only** at startup. |
| `KOWAN_SSO_BREAK_GLASS_USER` | The break-glass username. |

Kowan applies these at every start, matching providers by `name`, and marks them **env** in the console. Change them in the environment, since the values there win at the next restart.

Besides the settings above, a provider's `config` accepts `extraAuthParams` (extra parameters for the authorization request) and `tokenAuthMethod` (`client_secret_basic` or `client_secret_post`; by default Kowan picks the one the provider supports).

## How sign-in is protected

Kowan uses the authorization code flow with PKCE (S256), a `state` and a `nonce`. It accepts an ID token only if it is signed with an asymmetric algorithm and a key the provider publishes, and if its issuer, audience, expiry and nonce match, allowing 60 seconds of clock difference. No token ever appears in a URL, and a sign-in must finish within 10 minutes.

Successful and failed sign-ins are recorded in the [audit log](audit-log.md) as `login_sso` and `login_failed`.

## Troubleshooting

| Message on the sign-in page | What to check |
|---|---|
| Single sign-on is not configured | Set `KOWAN_EXTERNAL_URL` and restart. |
| The sign-in took too long or was started in another browser | Start again within 10 minutes, in the same browser. |
| The identity provider's response could not be verified | The issuer URL, the client ID and the server's clock. |
| The identity provider refused the sign-in code | The client secret and the redirect URI registered at the provider. |
| The identity provider could not be reached | The issuer URL, and that the Kowan server can reach the provider. |
| No Kowan account is linked to this identity | Create the user, or turn on **Create accounts on first sign-in**. |
| Your email domain is not allowed to sign in here | **Allowed email domains**, and that the provider marks the email as verified. |

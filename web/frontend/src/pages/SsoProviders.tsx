import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Badge,
  Box,
  Button,
  Callout,
  Code,
  CopyButton,
  InputToggle,
  Popup,
  Select,
  Table,
  TagsInput,
  Text,
  TextInput,
  type Column,
} from "@/ui/kit";
import {
  createSsoProvider,
  deleteSsoProvider,
  listSsoProviders,
  listTeams,
  listUsers,
  putSsoSettings,
  testSsoProvider,
  updateSsoProvider,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import ConfirmModal from "../components/ConfirmModal";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import SectionCard from "../components/SectionCard";
import type { Role, SsoMapping, SsoProvider, SsoProviderConfig, SsoProviderType } from "../types";
import { errorMessage } from "../utils";
import { notify } from "../ui/toast";

const MASK = "********";

interface Preset {
  value: string;
  label: string;
  type: SsoProviderType;
  issuer: string;
  hint: string;
  scopes?: string;
  config?: SsoProviderConfig;
}

/** Starting points for common identity providers (all editable). */
const PRESETS: Preset[] = [
  { value: "keycloak", label: "Keycloak", type: "oidc", issuer: "https://keycloak.example.com/realms/<realm>", hint: "Client authentication on, standard flow, PKCE S256. Add a 'groups' mapper for group claims." },
  { value: "authentik", label: "Authentik", type: "oidc", issuer: "https://authentik.example.com/application/o/<slug>/", hint: "OAuth2/OpenID provider, confidential client. Groups arrive in the 'groups' claim." },
  { value: "authelia", label: "Authelia", type: "oidc", issuer: "https://auth.example.com", hint: "Register a client with redirect_uris set to the redirect URI below and require_pkce: true.", scopes: "openid profile email groups" },
  { value: "entra", label: "Microsoft Entra ID", type: "oidc", issuer: "https://login.microsoftonline.com/<tenant-id>/v2.0", hint: "App registration, web platform redirect URI. Enable the groups claim in Token configuration.", config: { trustEmail: false } },
  { value: "google", label: "Google", type: "oidc", issuer: "https://accounts.google.com", hint: "OAuth client of type Web application. Google sends no groups: map roles by email domain instead." },
  { value: "github", label: "GitHub", type: "github", issuer: "", hint: "OAuth App (not a GitHub App). Groups are your orgs and org/team slugs (needs read:org)." },
  { value: "oidc", label: "Other OpenID Connect", type: "oidc", issuer: "https://idp.example.com", hint: "Any provider that publishes /.well-known/openid-configuration." },
];

const ROLE_OPTIONS = [
  { value: "viewer", label: "Viewer" },
  { value: "operator", label: "Operator" },
  { value: "admin", label: "Admin" },
];

interface FormState {
  id?: string;
  preset: string;
  name: string;
  type: SsoProviderType;
  label: string;
  icon: string;
  issuer: string;
  clientId: string;
  clientSecret: string;
  /** A secret is stored: an emptied field falls back to keeping it. */
  hadSecret: boolean;
  scopes: string;
  enabled: boolean;
  defaultRole: Role;
  roleMappings: SsoMapping[];
  teamMappings: SsoMapping[];
  allowSignup: boolean;
  linkByEmail: boolean;
  trustEmail: boolean;
  useUserinfo: boolean;
  allowedDomains: string[];
}

function emptyForm(preset = PRESETS[0]): FormState {
  return {
    preset: preset.value,
    name: preset.value,
    type: preset.type,
    label: preset.label,
    icon: "",
    issuer: preset.issuer,
    clientId: "",
    clientSecret: "",
    hadSecret: false,
    scopes: preset.scopes ?? "",
    enabled: true,
    defaultRole: "viewer",
    roleMappings: [],
    teamMappings: [],
    allowSignup: true,
    linkByEmail: true,
    trustEmail: preset.config?.trustEmail ?? false,
    useUserinfo: false,
    allowedDomains: [],
  };
}

function fromProvider(p: SsoProvider): FormState {
  const c = p.config ?? {};
  return {
    id: p.id,
    preset: p.type === "github" ? "github" : "oidc",
    name: p.name,
    type: p.type,
    label: p.label,
    icon: p.icon,
    issuer: p.issuer,
    clientId: p.clientId,
    clientSecret: p.clientSecret ? MASK : "",
    hadSecret: !!p.clientSecret,
    scopes: p.scopes,
    enabled: p.enabled,
    defaultRole: c.defaultRole ?? "viewer",
    roleMappings: c.roleMappings ?? [],
    teamMappings: c.teamMappings ?? [],
    allowSignup: c.allowSignup ?? true,
    linkByEmail: c.linkByEmail ?? true,
    trustEmail: c.trustEmail ?? false,
    useUserinfo: c.useUserinfo ?? false,
    allowedDomains: c.allowedDomains ?? [],
  };
}

function toBody(f: FormState): Partial<SsoProvider> {
  const body: Partial<SsoProvider> = {
    name: f.name.trim(),
    type: f.type,
    label: f.label.trim(),
    icon: f.icon,
    issuer: f.type === "github" ? "" : f.issuer.trim(),
    clientId: f.clientId.trim(),
    scopes: f.scopes.trim(),
    enabled: f.enabled,
    config: {
      defaultRole: f.defaultRole,
      roleMappings: f.roleMappings.filter((m) => m.value && m.role),
      teamMappings: f.teamMappings.filter((m) => m.value && m.team),
      allowSignup: f.allowSignup,
      linkByEmail: f.linkByEmail,
      trustEmail: f.trustEmail,
      useUserinfo: f.useUserinfo,
      allowedDomains: f.allowedDomains,
    },
  };
  if (f.clientSecret !== MASK) body.clientSecret = f.clientSecret;
  return body;
}

function MappingRows({
  rows,
  onChange,
  target,
  targetOptions,
}: {
  rows: SsoMapping[];
  onChange: (r: SsoMapping[]) => void;
  target: "role" | "team";
  targetOptions: { value: string; label: string }[];
}) {
  const set = (i: number, patch: Partial<SsoMapping>) => onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <Box direction="column" gap={2}>
      {rows.map((r, i) => (
        <Box key={i} direction="row" gap={2} align="end">
          <TextInput aria-label="Claim" placeholder="groups" value={r.claim} onChange={(e) => set(i, { claim: e.target.value })} />
          <TextInput aria-label="Value" placeholder={target === "role" ? "kowan-admins" : "web-team"} value={r.value} onChange={(e) => set(i, { value: e.target.value })} />
          <Select
            aria-label={target === "role" ? "Role" : "Team"}
            width={160}
            options={targetOptions}
            value={(target === "role" ? r.role : r.team) ?? null}
            onChange={(v) => set(i, target === "role" ? { role: v as Role } : { team: v })}
          />
          <Button variant="clear" color="danger" iconLeft="delete-outline-rounded" aria-label="Remove mapping" onClick={() => onChange(rows.filter((_, j) => j !== i))} />
        </Box>
      ))}
      <Box direction="row">
        <Button size="sm" variant="outline" iconLeft="add-rounded" onClick={() => onChange([...rows, { claim: "groups", value: "" }])}>
          Add {target} mapping
        </Button>
      </Box>
    </Box>
  );
}

export default function SsoProviders() {
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["sso-providers"], queryFn: listSsoProviders, enabled: isAdmin });
  const teamsQ = useQuery({ queryKey: ["teams"], queryFn: listTeams, enabled: isAdmin });
  const usersQ = useQuery({ queryKey: ["users"], queryFn: listUsers, enabled: isAdmin });
  const [form, setForm] = useState<FormState | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<SsoProvider | null>(null);
  const providers = useMemo(() => q.data?.providers ?? [], [q.data]);
  const settings = q.data?.settings;
  const externalUrl = q.data?.externalUrl ?? "";
  const refresh = () => qc.invalidateQueries({ queryKey: ["sso-providers"] });

  const save = useMutation({
    mutationFn: (f: FormState) => (f.id ? updateSsoProvider(f.id, toBody(f)) : createSsoProvider(toBody(f))),
    onSuccess: (p) => {
      setForm(null);
      void refresh();
      notify({ color: "green", title: "Provider saved", message: `${p.label || p.name} is ${p.enabled ? "enabled" : "disabled"}` });
    },
    onError: (err) => setFormError(errorMessage(err, "Could not save provider")),
  });
  const remove = useMutation({
    mutationFn: (id: string) => deleteSsoProvider(id),
    onSuccess: () => {
      setDeleting(null);
      void refresh();
    },
    onError: (err) => notify({ color: "red", title: "Could not delete provider", message: errorMessage(err, "") }),
  });
  const test = useMutation({
    mutationFn: (id: string) => testSsoProvider(id),
    onSuccess: (r) => notify({ color: r.ok ? "green" : "red", title: r.ok ? "Provider reachable" : "Provider check failed", message: r.detail }),
    onError: (err) => notify({ color: "red", title: "Provider check failed", message: errorMessage(err, "") }),
  });
  const saveSettings = useMutation({
    mutationFn: putSsoSettings,
    onSuccess: () => void refresh(),
    onError: (err) => notify({ color: "red", title: "Could not update SSO settings", message: errorMessage(err, "") }),
  });

  if (!isAdmin) {
    return (
      <Box direction="column" gap={5}>
        <PageHeader title="Single sign-on" />
        <Callout color="danger" title="Access denied">You need administrator privileges to manage single sign-on.</Callout>
      </Box>
    );
  }

  const columns: Column<SsoProvider>[] = [
    { key: "label", header: "Provider", accessor: (p) => p.label, cell: (p) => <Text weight="medium">{p.label || p.name}</Text> },
    { key: "type", header: "Type", accessor: (p) => p.type, cell: (p) => <Code>{p.type}</Code> },
    { key: "issuer", header: "Issuer", grow: true, accessor: (p) => p.issuer, cell: (p) => <Text color="muted" truncate>{p.type === "github" ? "github.com" : p.issuer}</Text> },
    {
      key: "state",
      header: "State",
      accessor: (p) => (p.enabled ? "enabled" : "disabled"),
      cell: (p) => (
        <Box direction="row" gap={2}>
          <Badge color={p.enabled ? "green" : "gray"} variant="outline">{p.enabled ? "enabled" : "disabled"}</Badge>
          {p.managed === "env" && <Badge variant="outline">env</Badge>}
        </Box>
      ),
    },
    {
      key: "actions",
      header: "",
      align: "right",
      cell: (p) => (
        <Box direction="row" gap={1} justify="end" onClick={(e) => e.stopPropagation()}>
          <Button size="sm" variant="outline" loading={test.isPending && test.variables === p.id} onClick={() => test.mutate(p.id)}>Test</Button>
          <Button size="sm" variant="clear" iconLeft="edit-outline-rounded" aria-label={`Edit ${p.name}`} onClick={() => { setFormError(null); setForm(fromProvider(p)); }} />
          <Button size="sm" variant="clear" color="danger" iconLeft="delete-outline-rounded" aria-label={`Delete ${p.name}`} onClick={() => setDeleting(p)} />
        </Box>
      ),
    },
  ];

  const localAdmins = (usersQ.data ?? []).filter((u) => u.role === "admin" && (u.authSource || "local") === "local");
  const teamOptions = (teamsQ.data ?? []).map((t) => ({ value: t.id, label: t.name }));
  const preset = PRESETS.find((p) => p.value === form?.preset);
  const redirectFor = (id?: string) => (externalUrl ? `${externalUrl.replace(/\/$/, "")}/api/v1/auth/sso/${id ?? "<provider id>"}/callback` : "");
  const set = (patch: Partial<FormState>) => setForm((f) => (f ? { ...f, ...patch } : f));

  return (
    <Box direction="column" gap={5}>
      <PageHeader
        title="Single sign-on"
        description="Sign in with OpenID Connect (Keycloak, Authentik, Authelia, Entra ID, Google, Dex…) or GitHub."
        actions={<Button iconLeft="add-rounded" onClick={() => { setFormError(null); setForm(emptyForm()); }}>New provider</Button>}
      />

      {!externalUrl && (
        <Callout color="warning" title="KOWAN_EXTERNAL_URL is not set">
          The redirect URI is derived from it. Set it to the URL users open Kowan at (e.g. https://kowan.example.com) and restart.
        </Callout>
      )}

      <SectionCard plain noBodyPadding>
        <Table<SsoProvider>
          data={providers}
          columns={columns}
          rowKey={(p) => p.id}
          loading={q.isLoading}
          onRowClick={(p) => { setFormError(null); setForm(fromProvider(p)); }}
          hoverable
          emptyState={<EmptyState title="No identity providers" description="Users sign in with local passwords until you add one." />}
        />
      </SectionCard>

      <SectionCard title="Password sign-in" description="SSO-only mode turns off password sign-in for everyone except one break-glass local admin.">
        <Box direction="column" gap={4} style={{ maxWidth: 560 }}>
          <Select
            label="Break-glass account"
            description="A local admin that can always sign in with a password, e.g. when the identity provider is down."
            options={localAdmins.map((u) => ({ value: u.username, label: u.username }))}
            value={settings?.breakGlassUser ?? null}
            onChange={(v) => saveSettings.mutate({ breakGlassUser: v })}
          />
          <InputToggle
            label="SSO only"
            description="Hide the password form on the sign-in page and reject password sign-in for other accounts."
            checked={settings?.ssoOnly ?? false}
            disabled={saveSettings.isPending}
            onChange={(e) => saveSettings.mutate({ ssoOnly: e.target.checked })}
          />
        </Box>
      </SectionCard>

      <Popup open={form !== null} onOpenChange={(o) => !o && setForm(null)} size="lg">
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>{form?.id ? `Edit ${form.label || form.name}` : "New identity provider"}</Popup.Title>
          </Popup.Header>
          {form && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                setFormError(null);
                save.mutate(form);
              }}
            >
              <Popup.Body>
                <Box direction="column" gap={4}>
                  {!form.id && (
                    <Select
                      label="Provider"
                      options={PRESETS.map((p) => ({ value: p.value, label: p.label }))}
                      value={form.preset}
                      onChange={(v) => {
                        const p = PRESETS.find((x) => x.value === v) ?? PRESETS[0];
                        setForm({ ...emptyForm(p), clientId: form.clientId, clientSecret: form.clientSecret });
                      }}
                    />
                  )}
                  {preset && !form.id && <Text size="sm" color="muted">{preset.hint}</Text>}
                  <Box direction="column" gap={1}>
                    <Text size="sm" weight="medium">Redirect URI</Text>
                    {redirectFor(form.id) ? (
                      <Box direction="row" gap={2} align="center">
                        <Code>{redirectFor(form.id)}</Code>
                        {form.id && <CopyButton value={redirectFor(form.id)} />}
                      </Box>
                    ) : (
                      <Text size="sm" color="muted">Set KOWAN_EXTERNAL_URL first.</Text>
                    )}
                    {!form.id && <Text size="xs" color="muted">The final URI (with the provider id) is shown after saving.</Text>}
                  </Box>
                  <Box direction="row" gap={3}>
                    <TextInput label="Button label" value={form.label} onChange={(e) => set({ label: e.target.value })} description="Shown as “Continue with …”." />
                    <TextInput label="Name" value={form.name} onChange={(e) => set({ name: e.target.value.toLowerCase() })} description="Short id: lowercase, digits, dashes." />
                  </Box>
                  {form.type === "oidc" && (
                    <TextInput label="Issuer URL" value={form.issuer} onChange={(e) => set({ issuer: e.target.value })} description="Kowan reads <issuer>/.well-known/openid-configuration." />
                  )}
                  <Box direction="row" gap={3}>
                    <TextInput label="Client ID" value={form.clientId} onChange={(e) => set({ clientId: e.target.value })} />
                    <TextInput
                      label="Client secret"
                      type="password"
                      autoComplete="new-password"
                      value={form.clientSecret}
                      onFocus={() => form.clientSecret === MASK && set({ clientSecret: "" })}
                      // Focusing clears the mask; leaving it empty must not wipe the stored secret.
                      onBlur={() => form.hadSecret && !form.clientSecret && set({ clientSecret: MASK })}
                      onChange={(e) => set({ clientSecret: e.target.value })}
                      description={form.id ? "Leave as is to keep the stored secret." : "Stored encrypted."}
                    />
                  </Box>
                  <TextInput
                    label="Scopes"
                    placeholder={form.type === "github" ? "read:user user:email read:org" : "openid profile email"}
                    value={form.scopes}
                    onChange={(e) => set({ scopes: e.target.value })}
                    description="Space separated. Empty uses the default shown."
                  />
                  <Select label="Default role" options={ROLE_OPTIONS} value={form.defaultRole} onChange={(v) => set({ defaultRole: v as Role })} description="Role for new users when no role mapping matches." />
                  <Box direction="column" gap={2}>
                    <Text size="sm" weight="medium">Role mappings</Text>
                    <Text size="xs" color="muted">When the claim contains the value, the user gets the role (highest match wins). Dot paths like realm_access.roles work.</Text>
                    <MappingRows rows={form.roleMappings} onChange={(r) => set({ roleMappings: r })} target="role" targetOptions={ROLE_OPTIONS} />
                  </Box>
                  <Box direction="column" gap={2}>
                    <Text size="sm" weight="medium">Team mappings</Text>
                    <Text size="xs" color="muted">Memberships are re-synced at every sign-in; teams added by hand are never removed.</Text>
                    <MappingRows rows={form.teamMappings} onChange={(r) => set({ teamMappings: r })} target="team" targetOptions={teamOptions} />
                  </Box>
                  <TagsInput label="Allowed email domains" placeholder="example.com" value={form.allowedDomains} onChange={(v) => set({ allowedDomains: v })} description="Optional. Only verified emails in these domains may sign in." />
                  <InputToggle label="Create accounts on first sign-in" checked={form.allowSignup} onChange={(e) => set({ allowSignup: e.target.checked })} />
                  <InputToggle label="Link existing accounts by verified email" checked={form.linkByEmail} onChange={(e) => set({ linkByEmail: e.target.checked })} />
                  {form.type === "oidc" && (
                    <>
                      <InputToggle label="Treat email as verified" description="Only for providers that verify emails but omit email_verified (some Entra setups)." checked={form.trustEmail} onChange={(e) => set({ trustEmail: e.target.checked })} />
                      <InputToggle label="Always call userinfo" description="Merge claims from the userinfo endpoint (groups not in the ID token)." checked={form.useUserinfo} onChange={(e) => set({ useUserinfo: e.target.checked })} />
                    </>
                  )}
                  <InputToggle label="Enabled" checked={form.enabled} onChange={(e) => set({ enabled: e.target.checked })} />
                  {formError && <Callout color="danger">{formError}</Callout>}
                </Box>
              </Popup.Body>
              <Popup.Footer>
                <Box direction="row" justify="end" gap={2}>
                  <Button variant="outline" onClick={() => setForm(null)}>Cancel</Button>
                  <Button type="submit" loading={save.isPending}>Save</Button>
                </Box>
              </Popup.Footer>
            </form>
          )}
        </Popup.Content>
      </Popup>

      <ConfirmModal
        opened={deleting !== null}
        onClose={() => setDeleting(null)}
        title="Delete identity provider"
        confirmLabel="Delete"
        loading={remove.isPending}
        onConfirm={() => deleting && remove.mutate(deleting.id)}
        message={<>Delete <Text as="span" weight="semibold">{deleting?.label || deleting?.name}</Text>? Its identity links are removed; users it created keep their accounts but can no longer sign in through it.</>}
      />
    </Box>
  );
}

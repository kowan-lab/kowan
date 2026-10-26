import { useMemo, useState, type CSSProperties } from "react";
import {
  Accordion,
  Badge,
  Box,
  Button,
  Callout,
  Card,
  Checkbox,
  Popup,
  Select,
  Skeleton,
  Text,
  TextInput,
} from "@/ui/kit";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router";
import {
  createDeployment,
  deleteTemplate,
  listHosts,
  listTemplates,
  renderTemplate,
  type DeploymentInput,
} from "../api/client";
import { useAuth } from "../auth/AuthContext";
import EmptyState from "../components/EmptyState";
import PageHeader from "../components/PageHeader";
import { useHost } from "../HostContext";
import {
  CreatePolicies,
  applyCreatePolicies,
  emptyCreatePolicies,
  validateCreatePolicies,
  type CreatePoliciesValue,
  type PolicyResult,
} from "../components/deployment/CreatePolicies";
import type { AppTemplate, Deployment } from "../types";
import { notify } from "../ui/toast";
import { errorMessage } from "../utils";

// ---------------------------------------------------------------------------
// Templates: the app catalog. A dense grid of square tiles (built-in + custom
// templates), filterable by search and category. "Deploy" opens a dialog that
// picks the host, the deployment name and the template inputs, renders the
// template server-side (POST /templates/{id}/render), creates the deployment
// and, by default, starts it — two clicks from catalog to running app.
// ---------------------------------------------------------------------------

const ALL = "All";

/** Images a template runs: the spec image, or every `image:` line in compose. */
function templateImages(t: AppTemplate): string[] {
  if (t.type === "container") {
    const img = t.spec?.["image"];
    return typeof img === "string" && img ? [img] : [];
  }
  const out: string[] = [];
  for (const m of (t.compose ?? "").matchAll(/^\s*image:\s*["']?([^\s"']+)/gm)) out.push(m[1]);
  return out;
}

/** Docker-safe default deployment name from the template id. */
function defaultName(t: AppTemplate): string {
  const base = t.id.startsWith("builtin:") ? t.id.slice(8) : t.name;
  return base.toLowerCase().replace(/[^a-z0-9_.-]+/g, "-").replace(/^-+|-+$/g, "") || "app";
}

/** Random alphanumeric secret (no quotes/backslashes, safe in compose YAML). */
function randomSecret(len = 24): string {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
  const buf = new Uint32Array(len);
  crypto.getRandomValues(buf);
  return Array.from(buf, (n) => chars[n % chars.length]).join("");
}

function initialValues(t: AppTemplate): Record<string, string> {
  const v: Record<string, string> = {};
  for (const i of t.inputs ?? []) v[i.name] = i.default || (i.secret ? randomSecret() : "");
  return v;
}

export default function Templates() {
  const queryClient = useQueryClient();
  const { user } = useAuth();
  const isAdmin = user?.role === "admin";
  const canDeploy = user?.role === "admin" || user?.role === "operator";

  const templatesQuery = useQuery({ queryKey: ["templates"], queryFn: listTemplates });
  const templates = templatesQuery.data ?? [];

  const [query, setQuery] = useState("");
  const [category, setCategory] = useState(ALL);
  const [deploying, setDeploying] = useState<AppTemplate | null>(null);
  const [removing, setRemoving] = useState<AppTemplate | null>(null);

  const categories = useMemo(() => {
    const counts = new Map<string, number>();
    for (const t of templates) counts.set(t.category || "Other", (counts.get(t.category || "Other") ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [templates]);

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    return templates.filter((t) => {
      if (category !== ALL && (t.category || "Other") !== category) return false;
      if (!q) return true;
      const hay = [t.name, t.description, t.category, ...templateImages(t)].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }, [templates, query, category]);

  const removeMutation = useMutation({
    mutationFn: (t: AppTemplate) => deleteTemplate(t.id),
    onSuccess: (_d, t) => {
      notify({ color: "success", title: "Template deleted", message: `"${t.name}" was removed` });
      setRemoving(null);
      queryClient.invalidateQueries({ queryKey: ["templates"] });
    },
    onError: (err) =>
      notify({ color: "danger", title: "Delete failed", message: errorMessage(err, "Could not delete the template") }),
  });

  return (
    <Box direction="column" gap={5} style={{ width: "100%" }}>
      <PageHeader
        title="Templates"
        description="Common self-hosted apps with sensible defaults. Pick one, fill in the few values it asks for, and Kowan creates and starts the deployment."
      />

      <Box direction="column" gap={3}>
        <TextInput
          aria-label="Search templates"
          placeholder="Search by name, description or image…"
          iconLeft="search-rounded"
          size="sm"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          maxWidth={420}
        />
        <Box direction="row" wrap gap={2} role="group" aria-label="Filter by category">
          {[[ALL, templates.length] as [string, number], ...categories].map(([c, n]) => (
            <Button
              key={c}
              size="xs"
              variant={category === c ? "solid" : "outline"}
              aria-pressed={category === c}
              onClick={() => setCategory(c)}
            >
              {c}
              <Text as="span" size="xs" color={category === c ? "inherit" : "muted"} tabularNums>
                {n}
              </Text>
            </Button>
          ))}
        </Box>
      </Box>

      {templatesQuery.isError ? (
        <Callout color="danger" title="Could not load templates">
          {errorMessage(templatesQuery.error, "The template catalog request failed")}
        </Callout>
      ) : templatesQuery.isLoading ? (
        <div style={GRID}>
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} height={148} />
          ))}
        </div>
      ) : visible.length === 0 ? (
        <EmptyState
          title="No templates match"
          description="Try another search term or category."
          action={
            <Button variant="outline" size="sm" onClick={() => { setQuery(""); setCategory(ALL); }}>
              Clear filters
            </Button>
          }
        />
      ) : (
        <div style={GRID}>
          {visible.map((t) => (
            <TemplateTile
              key={t.id}
              t={t}
              canDeploy={canDeploy}
              canDelete={isAdmin && !t.builtIn}
              onDeploy={() => setDeploying(t)}
              onDelete={() => setRemoving(t)}
            />
          ))}
        </div>
      )}

      {deploying && <DeployDialog key={deploying.id} t={deploying} onClose={() => setDeploying(null)} />}

      <Popup open={removing !== null} onOpenChange={(o) => { if (!o) setRemoving(null); }} size="sm">
        <Popup.Content>
          <Popup.Header>
            <Popup.Title>Delete template</Popup.Title>
          </Popup.Header>
          <Popup.Body>
            <Text>
              Delete the custom template <Text as="span" weight="semibold">{removing?.name}</Text>? Deployments
              already created from it are not affected.
            </Text>
          </Popup.Body>
          <Popup.Footer>
            <Button variant="clear" onClick={() => setRemoving(null)} disabled={removeMutation.isPending}>
              Cancel
            </Button>
            <Button
              color="danger"
              loading={removeMutation.isPending}
              onClick={() => removing && removeMutation.mutate(removing)}
            >
              Delete
            </Button>
          </Popup.Footer>
        </Popup.Content>
      </Popup>
    </Box>
  );
}

const GRID: CSSProperties = {
  display: "grid",
  gridTemplateColumns: "repeat(auto-fill, minmax(232px, 1fr))",
  gap: 9,
};

function TemplateTile({
  t,
  canDeploy,
  canDelete,
  onDeploy,
  onDelete,
}: {
  t: AppTemplate;
  canDeploy: boolean;
  canDelete: boolean;
  onDeploy: () => void;
  onDelete: () => void;
}) {
  const images = templateImages(t);
  return (
    <Card variant="outline" size="sm">
      <Card.Body style={{ flex: 1, gap: 6 }}>
        <Box direction="row" align="center" justify="between" gap={2}>
          <Text weight="semibold" size="md" truncate>
            {t.name}
          </Text>
          <Box direction="row" gap={1} shrink={0}>
            {!t.builtIn && (
              <Badge size="sm" variant="outline" color="blue">
                CUSTOM
              </Badge>
            )}
            <Badge size="sm" variant="outline">
              {t.type === "compose" ? "COMPOSE" : "CONTAINER"}
            </Badge>
          </Box>
        </Box>
        <Text size="xs" color="muted" transform="uppercase">
          {t.category || "Other"}
        </Text>
        <Text size="sm" color="muted" lines={3}>
          {t.description}
        </Text>
        <Text
          size="xs"
          color="subtle"
          truncate
          title={images.join("\n")}
          style={{ marginTop: "auto" }}
        >
          {images[0] ?? "—"}
          {images.length > 1 ? ` +${images.length - 1}` : ""}
        </Text>
      </Card.Body>
      <Card.Footer style={{ paddingTop: 6, paddingBottom: 6 }}>
        {t.website && (
          <Button variant="link" size="xs" href={t.website} target="_blank" rel="noreferrer">
            Docs
          </Button>
        )}
        <Box direction="row" gap={1} style={{ marginLeft: "auto" }}>
          {canDelete && (
            <Button variant="clear" size="xs" color="danger" iconLeft="delete-outline-rounded" aria-label={`Delete ${t.name}`} onClick={onDelete} />
          )}
          <Button
            size="xs"
            onClick={onDeploy}
            disabled={!canDeploy}
            title={canDeploy ? undefined : "Operators and admins can deploy"}
          >
            Deploy
          </Button>
        </Box>
      </Card.Footer>
    </Card>
  );
}

function DeployDialog({ t, onClose }: { t: AppTemplate; onClose: () => void }) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { hostId } = useHost();
  const hostsQuery = useQuery({ queryKey: ["hosts"], queryFn: listHosts });
  const hosts = hostsQuery.data ?? [];

  const [host, setHost] = useState<string>("");
  const [name, setName] = useState(() => defaultName(t));
  const [values, setValues] = useState<Record<string, string>>(() => initialValues(t));
  const [start, setStart] = useState(true);
  const [policies, setPolicies] = useState<CreatePoliciesValue>(emptyCreatePolicies);
  const [error, setError] = useState<string | null>(null);

  // Default to the host selected in the shell, else the first one.
  const effectiveHost = host || (hosts.some((h) => h.id === hostId) ? hostId : hosts[0]?.id ?? "");

  const mutation = useMutation({
    mutationFn: async (): Promise<{ dep: Deployment; results: PolicyResult[] }> => {
      const rendered = await renderTemplate(t.id, { name: name.trim(), values });
      const input: DeploymentInput = {
        name: name.trim(),
        type: rendered.type,
        strategy: "standalone",
        targets: [effectiveHost],
        replicas: 0,
        spec: rendered.spec,
        compose: rendered.compose,
        enabled: true,
      };
      const dep = await createDeployment(input);
      // The record exists from here on: the rest can fail one by one.
      const results = await applyCreatePolicies(dep, { ...policies, deployNow: start });
      return { dep, results };
    },
    onSuccess: ({ dep, results }) => {
      const failed = results.filter((r) => !r.ok);
      notify(
        failed.length
          ? {
              color: "warning",
              title: `"${dep.name}" was created, but not everything was set`,
              message: failed.map((r) => `${r.label}: ${r.error ?? "failed"}`).join(" · ") + ". Finish it on the workload page.",
            }
          : {
              color: start ? "info" : "success",
              title: start ? "Deploying" : "Deployment created",
              message: start ? `"${dep.name}" is starting in the background` : `"${dep.name}" was created`,
            },
      );
      queryClient.invalidateQueries({ queryKey: ["schedules"] });
      queryClient.invalidateQueries({ queryKey: ["volbackups"] });
      queryClient.invalidateQueries({ queryKey: ["deployments"] });
      onClose();
      navigate(`/workloads/dep~${dep.id}`);
    },
    onError: (err) => setError(errorMessage(err, "Could not deploy the template")),
  });

  const submit = () => {
    if (!effectiveHost) return setError("Pick the host to deploy to");
    const policyError = validateCreatePolicies(policies);
    if (policyError) return setError(policyError);
    if (!name.trim()) return setError("Deployment name is required");
    const missing = (t.inputs ?? []).find((i) => i.required && !(values[i.name] ?? "").trim());
    if (missing) return setError(`${missing.label || missing.name} is required`);
    setError(null);
    mutation.mutate();
  };

  return (
    <Popup open onOpenChange={(o) => { if (!o && !mutation.isPending) onClose(); }} size="md">
      <Popup.Content>
        <Popup.Header>
          <Popup.Title>Deploy {t.name}</Popup.Title>
          <Popup.Close />
        </Popup.Header>
        <Popup.Body>
          <Text size="sm" color="muted">
            {t.description}
          </Text>
          <Box direction="row" wrap gap={4}>
            <Select
              label="Host"
              options={hosts.map((h) => ({ value: h.id, label: h.name || h.id, trailing: h.online ? "online" : "offline" }))}
              value={effectiveHost}
              onChange={setHost}
              loading={hostsQuery.isLoading}
              placeholder={hostsQuery.isLoading ? "Loading hosts…" : "Select a host"}
              style={{ flex: "1 1 200px" }}
            />
            <TextInput
              label="Deployment name"
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              style={{ flex: "1 1 200px" }}
              description={t.type === "container" ? "Also prefixes the named volumes" : "Used as the compose project name"}
            />
          </Box>

          {(t.inputs ?? []).length > 0 && (
            <Box direction="column" gap={3}>
              <Text size="xs" color="muted" transform="uppercase">
                Settings
              </Text>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(200px, 1fr))", gap: 12 }}>
                {t.inputs.map((i) => (
                  <TextInput
                    key={i.name}
                    label={i.label || i.name}
                    required={i.required}
                    type={i.secret ? "password" : "text"}
                    autoComplete={i.secret ? "new-password" : "off"}
                    description={i.description || (i.secret ? "Random value pre-filled" : undefined)}
                    value={values[i.name] ?? ""}
                    onChange={(e) => setValues((v) => ({ ...v, [i.name]: e.target.value }))}
                    suffix={
                      i.secret ? (
                        <Button
                          variant="clear"
                          size="xs"
                          iconLeft="refresh-rounded"
                          aria-label={`Generate a new ${i.label || i.name}`}
                          onClick={() => setValues((v) => ({ ...v, [i.name]: randomSecret() }))}
                        />
                      ) : undefined
                    }
                  />
                ))}
              </div>
            </Box>
          )}

          <Checkbox
            label="Start after creating"
            description="Pulls the images and starts it now. Otherwise the deployment is created stopped."
            checked={start || policies.domain === "on"}
            disabled={policies.domain === "on"}
            onChange={(e) => setStart(e.target.checked)}
          />

          <Accordion>
            <Accordion.Item value="more">
              <Accordion.Trigger>
                <span className="flex-1 text-[14px]">More options: updates, backups, schedule, domain, webhook</span>
              </Accordion.Trigger>
              <Accordion.Content>
                <CreatePolicies
                  value={policies}
                  onChange={setPolicies}
                  hostId={effectiveHost}
                  compose={t.type === "compose" ? t.compose : undefined}
                  oneColumn
                  hideDeploy
                />
              </Accordion.Content>
            </Accordion.Item>
          </Accordion>

          {error && (
            <Callout color="danger" onDismiss={() => setError(null)}>
              {error}
            </Callout>
          )}
        </Popup.Body>
        <Popup.Footer>
          <Button variant="clear" onClick={onClose} disabled={mutation.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} loading={mutation.isPending} disabled={hosts.length === 0}>
            {start ? "Deploy" : "Create"}
          </Button>
        </Popup.Footer>
      </Popup.Content>
    </Popup>
  );
}

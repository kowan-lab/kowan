import { Button, Menu, Spinner, Text } from "@/ui/kit";
import { useQuery } from "@tanstack/react-query";
import { listSecrets } from "../api/client";

// ---------------------------------------------------------------------------
// SecretReferenceButton — a small menu that lets the user insert a secret
// reference token into a free-text credential field.
//
// Some credential fields (git username/token, webhook auth, HTTP headers/body,
// script bodies…) are plain text that the backend resolves at run time via
// `[[name]]` / `[[name.field]]` tokens (store.resolve_secrets). Those fields
// have no connectionRef mechanism, so instead of picking a secret *object* we
// help the user drop a *reference* to a stored secret straight into the text.
//
// A stored secret can itself be connection-backed, so this reuses the
// connection model indirectly: the token points at the secret, the secret may
// delegate to a connection.
//
// Behaviour:
//  - lists stored secrets via useQuery(['secrets'], listSecrets)
//  - clicking a secret with no/one field inserts "[[name]]"
//  - a secret with several fields expands into a submenu so the user can pick
//    a field and insert "[[name.field]]" (name-only stays available too)
// ---------------------------------------------------------------------------

interface SecretReferenceButtonProps {
  /** Called with the token to insert, e.g. "[[db]]" or "[[db.password]]". */
  onInsert: (token: string) => void;
  /** Trigger label. Defaults to "Insert secret". */
  label?: string;
}

function nameToken(name: string): string {
  return `[[${name}]]`;
}

function fieldToken(name: string, field: string): string {
  return `[[${name}.${field}]]`;
}

export default function SecretReferenceButton({
  onInsert,
  label = "Insert secret",
}: SecretReferenceButtonProps) {
  const secretsQuery = useQuery({
    queryKey: ["secrets"],
    queryFn: listSecrets,
  });

  const secrets = secretsQuery.data ?? [];

  return (
    <Menu placement="bottom-end">
      <Menu.Trigger>
        <Button
          type="button"
          variant="light"
          color="neutral"
          size="sm"
          iconLeft="key-outline-rounded"
          iconRight="arrow-drop-down-rounded"
        >
          {label}
        </Button>
      </Menu.Trigger>
      <Menu.Content ariaLabel="Insert a secret reference">
        <Menu.Label>Insert a secret reference</Menu.Label>
        {secretsQuery.isLoading ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "8px 12px",
            }}
          >
            <Spinner size="sm" />
            <Text size="sm" color="muted">
              Loading secrets…
            </Text>
          </div>
        ) : secretsQuery.isError ? (
          <div style={{ padding: "8px 12px" }}>
            <Text size="sm" color="danger">
              Could not load secrets.
            </Text>
          </div>
        ) : secrets.length === 0 ? (
          <div style={{ padding: "8px 12px" }}>
            <Text size="sm" color="muted">
              No stored secrets yet.
            </Text>
          </div>
        ) : (
          secrets.map((secret) =>
            secret.fields.length > 1 ? (
              // Multiple fields: submenu with name-only + one entry per field.
              <Menu.Sub
                key={secret.id}
                label={secret.name}
                iconLeft="key-outline-rounded"
              >
                <Menu.Item
                  iconLeft="data-object-rounded"
                  onSelect={() => onInsert(nameToken(secret.name))}
                >
                  {nameToken(secret.name)}
                </Menu.Item>
                <Menu.Separator />
                {secret.fields.map((field) => (
                  <Menu.Item
                    key={field}
                    onSelect={() => onInsert(fieldToken(secret.name, field))}
                  >
                    {field}
                  </Menu.Item>
                ))}
              </Menu.Sub>
            ) : secret.fields.length === 1 ? (
              // Single field: insert "[[name.field]]" directly.
              <Menu.Item
                key={secret.id}
                iconLeft="key-outline-rounded"
                trailing={
                  <Text size="xs" color="muted">
                    {secret.fields[0]}
                  </Text>
                }
                onSelect={() =>
                  onInsert(fieldToken(secret.name, secret.fields[0]))
                }
              >
                {secret.name}
              </Menu.Item>
            ) : (
              // No known field names: insert "[[name]]".
              <Menu.Item
                key={secret.id}
                iconLeft="key-outline-rounded"
                onSelect={() => onInsert(nameToken(secret.name))}
              >
                {secret.name}
              </Menu.Item>
            ),
          )
        )}
      </Menu.Content>
    </Menu>
  );
}

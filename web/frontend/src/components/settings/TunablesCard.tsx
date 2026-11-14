import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Box, Button, InputToggle, Text, TextArea, TextInput } from "@/ui/kit";
import { listTunables, saveTunables, type Tunable, type TunableGroup } from "../../api/client";
import { notify } from "../../ui/toast";
import { errorMessage } from "../../utils";
import SectionCard from "../SectionCard";

const KEY = ["settings", "tunables"];

/** Server settings that used to be environment variables, edited in place.
 *  Saving sends only what changed; every change applies at once. */
export default function TunablesCard({ groups, title, description }: { groups: TunableGroup[]; title: string; description: string }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: KEY, queryFn: listTunables });
  const groupKey = groups.join(",");
  const rows = useMemo(() => (q.data ?? []).filter((t) => groupKey.split(",").includes(t.group)), [q.data, groupKey]);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});

  // Rebase the form on what the server holds after each load or save.
  useEffect(() => {
    setDraft(Object.fromEntries(rows.map((t) => [t.key, t.value])));
  }, [rows]);

  const changed = rows.filter((t) => (draft[t.key] ?? t.value).trim() !== t.value);
  const save = useMutation({
    mutationFn: (values: Record<string, string | null>) => saveTunables(values),
    onSuccess: (next) => {
      qc.setQueryData(KEY, next);
      setErrors({});
      notify({ color: "green", title: "Saved", message: "The new values apply now." });
    },
    onError: (e) => {
      const fields = (e as { response?: { data?: { fields?: Record<string, string> } } })?.response?.data?.fields;
      if (fields) setErrors(fields);
      else notify({ color: "red", title: "Not saved", message: errorMessage(e, "Could not save the settings") });
    },
  });

  const set = (key: string, v: string) => {
    setDraft((d) => ({ ...d, [key]: v }));
    setErrors((e) => {
      const { [key]: _gone, ...rest } = e;
      return rest;
    });
  };
  const stale = rows.filter((t) => t.envSet);

  return (
    <SectionCard title={title} description={description}>
      <form
        className="flex max-w-[640px] flex-col gap-[16px]"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(Object.fromEntries(changed.map((t) => [t.key, draft[t.key].trim()])));
        }}
      >
        {rows.map((t) => (
          <TunableField
            key={t.key}
            t={t}
            value={draft[t.key] ?? t.value}
            error={errors[t.key]}
            disabled={save.isPending}
            onChange={(v) => set(t.key, v)}
            onReset={() => save.mutate({ [t.key]: null })}
          />
        ))}
        {stale.length > 0 && (
          <Text size="sm" color="muted">
            {stale.map((t) => t.env).join(", ")} {stale.length === 1 ? "is" : "are"} still in the server environment. Kowan no longer reads {stale.length === 1 ? "it" : "them"}: remove {stale.length === 1 ? "it" : "them"}.
          </Text>
        )}
        <Box direction="row" wrap justify="end">
          <Button color="info" type="submit" loading={save.isPending} disabled={q.isLoading || changed.length === 0}>
            Save
          </Button>
        </Box>
      </form>
    </SectionCard>
  );
}

function TunableField({ t, value, error, disabled, onChange, onReset }: {
  t: Tunable;
  value: string;
  error?: string;
  disabled: boolean;
  onChange: (v: string) => void;
  onReset: () => void;
}) {
  const hint = (
    <>
      {t.help}
      {t.kind !== "bool" && t.kind !== "origins" && t.default !== "" && <> Default {t.default}.</>}
      {t.custom && (
        <>
          {" "}
          <button type="button" className="font-medium text-signal underline-offset-2 hover:underline" disabled={disabled} onClick={onReset}>
            Use the default
          </button>
        </>
      )}
    </>
  );
  if (t.kind === "bool") {
    return <InputToggle label={t.label} description={hint} checked={value === "true"} disabled={disabled} onChange={(e) => onChange(e.target.checked ? "true" : "false")} />;
  }
  if (t.kind === "origins") {
    return (
      <TextArea
        label={t.label}
        description={hint}
        placeholder="https://app.example.com"
        rows={3}
        spellCheck={false}
        disabled={disabled}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        errorText={error}
      />
    );
  }
  return (
    <TextInput
      label={t.label}
      description={hint}
      placeholder={t.default || (t.kind === "text" ? "Default" : undefined)}
      inputMode={t.kind === "int" ? "numeric" : undefined}
      autoComplete="off"
      spellCheck={false}
      disabled={disabled}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      errorText={error}
    />
  );
}

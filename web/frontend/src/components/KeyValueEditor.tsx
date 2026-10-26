import { Box, Button, Text, TextInput } from "@/ui/kit";

export interface KeyValueRow {
  key: string;
  value: string;
}

/** Collapse key/value rows into a record, dropping rows with empty keys. */
export function rowsToRecord(
  rows: KeyValueRow[],
): Record<string, string> | undefined {
  const out: Record<string, string> = {};
  for (const row of rows) {
    const k = row.key.trim();
    if (k.length === 0) continue;
    out[k] = row.value;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

export interface KeyValueEditorProps {
  rows: KeyValueRow[];
  /** Append a blank row. */
  onAdd: () => void;
  /** Patch one field of the row at index i. */
  onUpdate: (index: number, patch: Partial<KeyValueRow>) => void;
  /** Remove the row at index i. */
  onRemove: (index: number) => void;
  /** Section heading (e.g. "Driver options", "Labels", "Build args"). */
  label: string;
  /** Label for the "add row" button. */
  addLabel?: string;
  /** Shown when there are no rows yet. */
  emptyText: string;
  keyPlaceholder?: string;
  /** Static, or per-row (used by Secrets to show "(unchanged)" in edit mode). */
  valuePlaceholder?: string | ((index: number) => string);
  /** Input type of the value column ("password" for Secrets). */
  valueType?: "text" | "password";
  /** autoComplete of the value column (e.g. "new-password" for Secrets). */
  valueAutoComplete?: string;
  /** Disable every input/button (e.g. while a build is running). */
  disabled?: boolean;
  /** aria-label of the per-row delete button. */
  removeAriaLabel?: string;
  /** Optional content between the header and the rows (e.g. a hint line). */
  hint?: React.ReactNode;
}

/**
 * A controlled editor for a list of {key, value} rows: a header with an Add
 * button, an empty-state hint, then one row of [key][value][delete] per entry.
 * Extracted from the near-identical editors in Volumes, Networks, Images and
 * Secrets. Spacing is normalized (gap 2, header marginBottom 4).
 */
export default function KeyValueEditor({
  rows,
  onAdd,
  onUpdate,
  onRemove,
  label,
  addLabel = "Add",
  emptyText,
  keyPlaceholder = "key",
  valuePlaceholder = "value",
  valueType = "text",
  valueAutoComplete,
  disabled = false,
  removeAriaLabel = "Remove row",
  hint,
}: KeyValueEditorProps) {
  const placeholderFor = (i: number) =>
    typeof valuePlaceholder === "function"
      ? valuePlaceholder(i)
      : valuePlaceholder;

  return (
    <Box>
      <Box
        direction="row"
        wrap
        justify="between"
        align="center"
        style={{ marginBottom: 4 }}
      >
        <Text size="sm" weight="medium">
          {label}
        </Text>
        <Button
          size="sm"
          variant="light"
          iconLeft="add-rounded"
          disabled={disabled}
          onClick={onAdd}
        >
          {addLabel}
        </Button>
      </Box>
      {hint}
      <Box direction="column" gap={2}>
        {rows.length === 0 && (
          <Text size="xs" color="muted">
            {emptyText}
          </Text>
        )}
        {rows.map((row, i) => (
          <Box key={i} direction="row" wrap={false} gap={2} align="end">
            <TextInput
              placeholder={keyPlaceholder}
              style={{ flex: 1 }}
              disabled={disabled}
              value={row.key}
              onChange={(e) => onUpdate(i, { key: e.target.value })}
            />
            <TextInput
              type={valueType}
              placeholder={placeholderFor(i)}
              autoComplete={valueAutoComplete}
              style={{ flex: 1 }}
              disabled={disabled}
              value={row.value}
              onChange={(e) => onUpdate(i, { value: e.target.value })}
            />
            <Button
              variant="clear"
              color="danger"
              iconLeft="delete-outline-rounded"
              aria-label={removeAriaLabel}
              disabled={disabled}
              onClick={() => onRemove(i)}
            />
          </Box>
        ))}
      </Box>
    </Box>
  );
}

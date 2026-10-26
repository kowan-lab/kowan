import { useMemo, useState } from "react";
import { Select } from "@/ui/kit";

export interface CreatableSelectOption {
  value: string;
  label: string;
}

export interface CreatableSelectProps {
  label?: string;
  /** Currently committed value (may be a value not present in `options`). */
  value: string;
  /** Called with the chosen option's value, or a free-typed value. */
  onChange: (value: string) => void;
  /** Options offered in the list. */
  options: CreatableSelectOption[];
  /** External loading flag — shows a spinner inside the search row. */
  loading?: boolean;
  /** Notified with the debounced search query (e.g. to fetch options). */
  onSearch?: (query: string) => void;
  placeholder?: string;
  description?: string;
}

/**
 * A searchable combobox (kit Select, `searchIn="trigger"`) whose options come
 * from `options`, but which ALSO lets the user commit a value they typed that is
 * not in the list.
 *
 * How free-typing works: the trigger doubles as a text input; each keystroke is
 * surfaced (debounced) via the Select's `onSearch`, which we mirror into local state
 * AND forward to the caller's `onSearch`. While the query is a non-empty string
 * that doesn't exactly match an existing option value, we prepend a synthetic
 * "Use <query>" option carrying the raw query as its value. Selecting it (or any
 * real option) fires `onChange`. This is intentionally dependency-free — it is
 * just the base Select plus one derived option.
 */
export default function CreatableSelect({
  label,
  value,
  onChange,
  options,
  loading,
  onSearch,
  placeholder,
  description,
}: CreatableSelectProps) {
  // The live (debounced) search query as reported by the Select. Used to build
  // the synthetic "Use X" option and to decide whether it is needed.
  const [query, setQuery] = useState("");

  const mergedOptions = useMemo<CreatableSelectOption[]>(() => {
    const base = [...options];
    // Ensure the committed value is always selectable even if it isn't in the
    // fetched list (e.g. a previously free-typed value on an edit form).
    if (value && !base.some((o) => o.value === value)) {
      base.unshift({ value, label: value });
    }
    // Offer a "Use <query>" affordance when the user typed something that is not
    // already an exact option value.
    const q = query.trim();
    if (q && !base.some((o) => o.value === q)) {
      base.unshift({ value: q, label: `Use "${q}"` });
    }
    return base;
  }, [options, value, query]);

  return (
    <Select
      label={label}
      description={description}
      placeholder={placeholder}
      options={mergedOptions}
      value={value}
      onChange={(v) => onChange(v ?? "")}
      searchable
      searchIn="trigger"
      onSearch={(q) => {
        setQuery(q);
        onSearch?.(q);
      }}
      loading={loading}
      fullWidth
    />
  );
}

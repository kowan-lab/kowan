import { cn } from "@/lib/utils";
export interface KnobOption<V extends string = string> {
  value: V;
  label: string;
}

export interface KnobProps<V extends string = string> {
  value: V;
  /** Two or more labelled positions, lowest (off) first. */
  options: KnobOption<V>[];
  onChange?: (value: V) => void;
  disabled?: boolean;
  /** A change is being saved: the control is disabled and marked busy. */
  loading?: boolean;
  /** Compatibility with existing callers; multi-value choices always use a select. */
  showOptions?: boolean;
  "aria-label": string;
  /** Hide the state label (only when the same text is shown right next to it). */
  hideLabel?: boolean;
  className?: string;
}


/** Explicit choices keep operational policy changes predictable. */
export function Knob<V extends string = string>({value,options,onChange,disabled,loading,hideLabel,className,"aria-label": label}: KnobProps<V>) {
  const inert = disabled || loading;
  const current = options.find(o => o.value === value) ?? options[0];
  if (options.length === 2) {
    const on = value === options[1].value;
    return <button type="button" role="switch" aria-checked={on} aria-label={label} aria-busy={loading || undefined} disabled={inert} onClick={() => onChange?.(options[on ? 0 : 1].value)} className={cn("inline-flex min-h-[36px] items-center gap-[10px] text-[13px] disabled:opacity-50 disabled:cursor-not-allowed",className)}>
      <span aria-hidden className={cn("inline-flex w-[32px] h-[20px] items-center px-[3px]",on ? "bg-primary":"bg-muted shadow-[inset_0_0_0_1px_var(--input)]")}><span className={cn("h-[14px] w-[14px]",on ? "ml-auto bg-primary-foreground":"bg-muted-foreground")} /></span>
      {!hideLabel && <span>{loading ? "Saving…" : current?.label}</span>}
    </button>;
  }
  return <select aria-label={label} aria-busy={loading || undefined} value={value} disabled={inert} onChange={e => onChange?.(e.target.value as V)} className={cn("min-h-[40px] max-w-full border border-input bg-card px-[12px] py-[8px] text-[14px] disabled:opacity-50 disabled:cursor-not-allowed",className)}>
    {options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
  </select>;
}

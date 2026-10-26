import {
  createContext,
  useContext,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type CSSProperties,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import { Check, ChevronsUpDown, Loader2, Upload, X } from "lucide-react";
import { Popover as P } from "radix-ui";
import { Command } from "cmdk";
import { cn } from "@/lib/utils";
import { renderIcon } from "./icon";
import { toneVars, type Color } from "./tone";

/* ── Field wrapper: label, control, description, error ──────────────────*/
interface FieldProps {
  id: string;
  label?: ReactNode;
  description?: ReactNode;
  errorText?: ReactNode;
  required?: boolean;
  fullWidth?: boolean;
  width?: number | string;
  minWidth?: number | string;
  maxWidth?: number | string;
  style?: CSSProperties;
  className?: string;
  children: ReactNode;
}

function px(v?: number | string) {
  return typeof v === "number" ? `${v}px` : v;
}

export function Field({ id, label, description, errorText, required, fullWidth, width, minWidth, maxWidth, style, className, children }: FieldProps) {
  return (
    <div
      className={cn("flex min-w-0 flex-col gap-[4px]", fullWidth && "w-full max-w-[640px]", className)}
      style={{ width: px(width), minWidth: px(minWidth), maxWidth: px(maxWidth), ...style }}
    >
      {label && (
        <label htmlFor={id} className="text-[13px] font-medium leading-none text-foreground">
          {label}
          {required && <span className="ml-[2px] text-[var(--tone-red-text)]" aria-hidden>*</span>}
        </label>
      )}
      {children}
      {errorText ? (
        <p id={`${id}-err`} className="m-0 text-xs text-[var(--tone-red-text)]">{errorText}</p>
      ) : description ? (
        <p id={`${id}-desc`} className="m-0 text-xs text-muted-foreground">{description}</p>
      ) : null}
    </div>
  );
}

/* One control geometry for the whole console (mock D): raised surface, 1px
 * border, square corners; 32px tall (28px for "sm", 36px for "lg"). */
const CONTROL =
  "w-full min-w-0 border border-[var(--border)] bg-[var(--popover)] text-[14px] outline-none transition-[border-color] hover:border-[var(--input)] " +
  "placeholder:text-muted-foreground focus-visible:border-ring focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal " +
  "disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-[var(--tone-red-solid)] aria-invalid:ring-[var(--tone-red-soft)]";

/* ── TextInput ───────────────────────────────────────────────────────────*/
export interface TextInputProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "size" | "prefix" | "width"> {
  label?: ReactNode;
  description?: ReactNode;
  errorText?: ReactNode;
  size?: "sm" | "md" | "lg";
  iconLeft?: string | ReactNode;
  prefix?: ReactNode;
  suffix?: ReactNode;
  suffixType?: "inline" | "separator";
  fullWidth?: boolean;
  width?: number | string;
  minWidth?: number | string;
  maxWidth?: number | string;
  "data-autofocus"?: boolean;
}

export function TextInput({
  label, description, errorText, size = "md", iconLeft, prefix, suffix, fullWidth = true, width, minWidth, maxWidth,
  id: idProp, className, style, required, suffixType: _suffixType, ...rest
}: TextInputProps) {
  const auto = useId();
  const id = idProp ?? auto;
  const h = size === "sm" ? "h-14 text-[13px]" : size === "lg" ? "h-18" : "h-16";
  return (
    <Field id={id} label={label} description={description} errorText={errorText} required={required}
      fullWidth={fullWidth} width={width} minWidth={minWidth} maxWidth={maxWidth} style={style} className={className}>
      <div className="relative flex items-center">
        {(iconLeft || prefix) && (
          <span className="pointer-events-none absolute left-[8px] flex items-center text-muted-foreground">
            {renderIcon(iconLeft, "sm")}
            {prefix}
          </span>
        )}
        <input
          id={id}
          required={required}
          aria-invalid={errorText ? true : undefined}
          aria-describedby={errorText ? `${id}-err` : description ? `${id}-desc` : undefined}
          className={cn(CONTROL, h, "px-5", (iconLeft || prefix) && "pl-12", suffix && "pr-15")}
          {...rest}
        />
        {suffix && <span className="absolute right-2 flex items-center">{suffix}</span>}
      </div>
    </Field>
  );
}

/* ── TextArea ────────────────────────────────────────────────────────────*/
export interface TextAreaProps extends TextareaHTMLAttributes<HTMLTextAreaElement> {
  label?: ReactNode;
  description?: ReactNode;
  errorText?: ReactNode;
  fullWidth?: boolean;
}
export function TextArea({ label, description, errorText, fullWidth = true, id: idProp, className, style, required, ...rest }: TextAreaProps) {
  const auto = useId();
  const id = idProp ?? auto;
  return (
    <Field id={id} label={label} description={description} errorText={errorText} required={required} fullWidth={fullWidth} style={style} className={className}>
      <textarea
        id={id}
        required={required}
        aria-invalid={errorText ? true : undefined}
        className={cn(CONTROL, "min-h-24 px-5 py-3 font-mono text-[13px] leading-[20px]")}
        {...rest}
      />
    </Field>
  );
}

/* ── NumberInput ─────────────────────────────────────────────────────────*/
export interface NumberInputProps {
  label?: ReactNode;
  description?: ReactNode;
  errorText?: ReactNode;
  placeholder?: string;
  value?: number | "";
  onChange?: (value: number | "") => void;
  min?: number;
  max?: number;
  step?: number;
  precision?: number;
  disabled?: boolean;
  fullWidth?: boolean;
  style?: CSSProperties;
  className?: string;
}
export function NumberInput({ label, description, errorText, placeholder, value, onChange, min, max, step = 1, precision, disabled, fullWidth = true, style, className }: NumberInputProps) {
  const id = useId();
  // While focused the field shows what is typed, not the parent's value: a
  // parent that maps "" to a default (`Number(v) || 6`) would otherwise snap a
  // cleared field back to the default and the next digit would append to it.
  const [focused, setFocused] = useState(false);
  const [text, setText] = useState(value === undefined || value === "" ? "" : String(value));
  const shown = focused ? text : value ?? "";
  return (
    <Field id={id} label={label} description={description} errorText={errorText} fullWidth={fullWidth} style={style} className={className}>
      <input
        id={id}
        type="number"
        inputMode="decimal"
        className={cn(CONTROL, "h-16 px-5 tabular-nums")}
        placeholder={placeholder}
        value={shown}
        min={min}
        max={max}
        step={step}
        disabled={disabled}
        aria-invalid={errorText ? true : undefined}
        onFocus={() => {
          setText(value === undefined || value === "" ? "" : String(value));
          setFocused(true);
        }}
        onBlur={() => setFocused(false)}
        onChange={(e) => {
          const raw = e.target.value;
          setText(raw);
          if (raw === "") return onChange?.("");
          let n = Number(raw);
          if (Number.isNaN(n)) return;
          if (precision !== undefined) n = Number(n.toFixed(precision));
          onChange?.(n);
        }}
      />
    </Field>
  );
}

/* ── Select (single, searchable combobox on cmdk) ───────────────────────*/
export interface SelectOption {
  value: string;
  label: ReactNode;
  iconLeft?: string | ReactNode;
  disabled?: boolean;
  trailing?: ReactNode;
  group?: string;
  searchText?: string;
  description?: ReactNode;
}
export interface SelectProps {
  options?: SelectOption[];
  value?: string | null;
  onChange?: (value: string) => void;
  label?: ReactNode;
  description?: ReactNode;
  errorText?: ReactNode;
  placeholder?: string;
  disabled?: boolean;
  clearable?: boolean;
  searchable?: boolean;
  searchIn?: "list" | "trigger";
  onSearch?: (q: string) => void;
  loading?: boolean;
  fullWidth?: boolean;
  width?: number | string;
  size?: "sm" | "md";
  emptyText?: ReactNode;
  style?: CSSProperties;
  className?: string;
  "aria-label"?: string;
}

function textOf(o: SelectOption): string {
  return o.searchText ?? (typeof o.label === "string" ? o.label : o.value);
}

export function Select({
  options = [], value, onChange, label, description, errorText, placeholder = "Select…", disabled, clearable,
  searchable, onSearch, loading, fullWidth, width, size = "md", emptyText = "No results", style, className,
  "aria-label": ariaLabel,
}: SelectProps) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const selected = options.find((o) => o.value === value);
  const showSearch = searchable || options.length > 8 || !!onSearch;
  useEffect(() => {
    if (!onSearch) return;
    const t = window.setTimeout(() => onSearch(q), 200);
    return () => window.clearTimeout(t);
  }, [q, onSearch]);
  const groups = useMemo(() => {
    const m = new Map<string, SelectOption[]>();
    for (const o of options) {
      const k = o.group ?? "";
      if (!m.has(k)) m.set(k, []);
      m.get(k)!.push(o);
    }
    return [...m.entries()];
  }, [options]);

  return (
    <Field id={id} label={label} description={description} errorText={errorText} fullWidth={fullWidth} width={width} style={style} className={className}>
      <P.Root open={open} onOpenChange={(o) => { setOpen(o); if (!o) setQ(""); }}>
        <div className="relative">
          <P.Trigger asChild disabled={disabled}>
            <button
              id={id}
              type="button"
              role="combobox"
              aria-expanded={open}
              aria-label={ariaLabel}
              aria-invalid={errorText ? true : undefined}
              className={cn(CONTROL, size === "sm" ? "h-14 text-[13px]" : "h-16", "flex items-center gap-3 px-5 text-left", clearable && selected && "pr-21")}
            >
              {selected?.iconLeft && renderIcon(selected.iconLeft, "sm")}
              <span className={cn("min-w-0 flex-1 truncate", !selected && "text-muted-foreground")}>
                {selected ? selected.label : value ? value : placeholder}
              </span>
              <ChevronsUpDown className="size-[12px] shrink-0 opacity-60" aria-hidden />
            </button>
          </P.Trigger>
          {clearable && value && !disabled && (
            <button
              type="button"
              aria-label="Clear selection"
              onClick={() => onChange?.("")}
              className="absolute top-1/2 right-11 -translate-y-1/2 rounded p-[2px] text-muted-foreground hover:bg-accent hover:text-foreground"
            >
              <X className="size-[12px]" />
            </button>
          )}
        </div>
        <P.Portal>
          <P.Content
            align="start"
            sideOffset={4}
            className="z-50 w-[var(--radix-popover-trigger-width)] min-w-72 overflow-hidden rounded-md border bg-popover text-popover-foreground shadow-md anim-pop"
          >
            <Command shouldFilter={!onSearch} filter={(v, s) => (v.toLowerCase().includes(s.toLowerCase()) ? 1 : 0)}>
              {showSearch && (
                <div className="flex items-center gap-3 border-b px-5">
                  <Command.Input value={q} onValueChange={setQ} placeholder="Search…" className="h-16 w-full bg-transparent text-[14px] outline-none placeholder:text-muted-foreground" />
                  {loading && <Loader2 className="size-[12px] animate-spin text-muted-foreground" />}
                </div>
              )}
              <Command.List className="max-h-108 overflow-y-auto p-2">
                <Command.Empty className="px-3 py-9 text-center text-sm text-muted-foreground">{loading ? "Searching…" : emptyText}</Command.Empty>
                {groups.map(([g, opts]) => (
                  <Command.Group key={g || "_"} heading={g || undefined} className="[&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-[4px] [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:text-muted-foreground">
                    {opts.map((o) => (
                      <Command.Item
                        key={o.value}
                        value={`${textOf(o)} ${o.value}`}
                        disabled={o.disabled}
                        onSelect={() => { onChange?.(o.value); setOpen(false); }}
                        className="flex cursor-default items-center gap-3 rounded-sm px-3 py-[4px] text-sm outline-none select-none data-[disabled=true]:opacity-50 data-[selected=true]:bg-accent"
                      >
                        {renderIcon(o.iconLeft, "sm")}
                        <span className="min-w-0 flex-1 truncate">{o.label}</span>
                        {o.trailing && <span className="text-xs text-muted-foreground">{o.trailing}</span>}
                        <Check className={cn("size-6", o.value === value ? "opacity-100" : "opacity-0")} />
                      </Command.Item>
                    ))}
                  </Command.Group>
                ))}
              </Command.List>
            </Command>
          </P.Content>
        </P.Portal>
      </P.Root>
    </Field>
  );
}

/* ── Checkbox ────────────────────────────────────────────────────────────*/
export interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size"> {
  label?: ReactNode;
  description?: ReactNode;
  size?: "sm" | "md";
  variant?: "inline" | "field";
}
/** 16px square: outline when empty, graphite fill with a check when on. */
const TICK_BOX =
  "peer size-8 shrink-0 cursor-[inherit] appearance-none border border-[var(--input)] bg-[var(--popover)] checked:border-foreground checked:bg-foreground disabled:opacity-50";
/** 16px round: outline when empty, a graphite ring with a dot when on (same size). */
const RADIO_DOT =
  "size-8 shrink-0 cursor-[inherit] appearance-none rounded-full border border-[var(--input)] bg-[var(--popover)] checked:border-foreground checked:bg-foreground checked:shadow-[inset_0_0_0_3px_var(--popover)] disabled:opacity-50";

export function Checkbox({ label, description, size: _s, variant: _v, className, style, id: idProp, ...rest }: CheckboxProps) {
  const auto = useId();
  const id = idProp ?? auto;
  return (
    <label htmlFor={id} className={cn("inline-flex cursor-pointer items-start gap-4 text-[14px] leading-[20px]", rest.disabled && "cursor-not-allowed opacity-60", className)} style={style}>
      <span className="relative mt-[2px] inline-flex size-8 shrink-0">
        <input id={id} type="checkbox" className={TICK_BOX} {...rest} />
        <Check aria-hidden strokeWidth={2.5} className="pointer-events-none absolute inset-[2px] hidden size-[12px] text-background peer-checked:block" />
      </span>
      {(label || description) && (
        <span className="flex flex-col gap-[2px]">
          {label && <span>{label}</span>}
          {description && <span className="text-[13px] leading-[18px] text-muted-foreground">{description}</span>}
        </span>
      )}
    </label>
  );
}

/* ── InputToggle (switch) ────────────────────────────────────────────────*/
export interface InputToggleProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "size" | "color"> {
  label?: ReactNode;
  description?: ReactNode;
  color?: Color;
  size?: "sm" | "md";
  variant?: "inline" | "field";
}
export function InputToggle({ label, description, color = "primary", size = "md", variant: _v, className, style, id: idProp, checked, onChange, disabled, ...all }: InputToggleProps) {
  const { onPointerEnter, onPointerLeave, onPointerDown, onPointerMove, onFocus, onBlur, ref, ...rest } = all as typeof all & { ref?: React.Ref<HTMLLabelElement> };
  const slot = { onPointerEnter, onPointerLeave, onPointerDown, onPointerMove, onFocus, onBlur, ref } as Record<string, unknown>;
  const auto = useId();
  const id = idProp ?? auto;
  const sm = size === "sm";
  return (
    <label {...slot} htmlFor={id} className={cn("inline-flex cursor-pointer items-start gap-[8px] text-sm", disabled && "cursor-not-allowed opacity-60", className)} style={{ ...toneVars(color, "primary"), ...style }}>
      {/* Integer pixel geometry so edges stay crisp: 1px border, knob inset 3px
          from every edge in both states (md 30x16 / knob 10, sm 24x14 / knob 8). */}
      <span className="relative mt-px inline-flex shrink-0">
        <input id={id} type="checkbox" role="switch" className="peer sr-only" checked={checked} onChange={onChange} disabled={disabled} {...rest} />
        <span
          aria-hidden
          className={cn(
            "box-border border border-input bg-sunken transition-colors peer-checked:border-[var(--t-solid)] peer-checked:bg-[var(--t-solid)] peer-focus-visible:outline-solid peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-signal",
            "h-[16px] w-[30px]",
          )}
        />
        <span
          aria-hidden
          className={cn(
            // A hardware slider: graphite knob when off, white knob on the colour when on.
            "absolute top-[3px] left-[3px] bg-muted-foreground transition-[transform,background-color] peer-checked:bg-white",
            "size-[10px] peer-checked:translate-x-[14px]",
          )}
        />
      </span>
      {(label || description) && (
        <span className="flex flex-col gap-[2px]">
          {label && <span className={cn("leading-[18px]", sm && "text-[13px]")}>{label}</span>}
          {description && <span className="text-xs text-muted-foreground">{description}</span>}
        </span>
      )}
    </label>
  );
}

/* ── RadioGroup / Radio ──────────────────────────────────────────────────
 * `attached` renders the group as a segmented control (used for filters);
 * otherwise radios stack with their labels. */
interface RadioCtx { name: string; value?: string; onChange?: (v: string) => void; attached?: boolean; size?: string }
const RadioContext = createContext<RadioCtx | null>(null);

export function RadioGroup({
  value, onChange, orientation = "vertical", attached, size, name, children, className, style, "aria-label": ariaLabel,
}: {
  value?: string; onChange?: (v: string) => void; orientation?: "horizontal" | "vertical"; attached?: boolean;
  size?: "sm" | "md"; name?: string; variant?: string; selectedStyle?: string; indicator?: unknown;
  children?: ReactNode; className?: string; style?: CSSProperties; "aria-label"?: string;
}) {
  const auto = useId();
  return (
    <RadioContext.Provider value={{ name: name ?? auto, value, onChange, attached, size }}>
      <div
        role="radiogroup"
        aria-label={ariaLabel}
        className={cn(
          attached
            ? "inline-flex items-stretch bg-[var(--popover)] shadow-[inset_0_0_0_1px_var(--border)]"
            : cn("flex gap-3", orientation === "horizontal" ? "flex-row flex-wrap gap-x-6" : "flex-col"),
          className,
        )}
        style={style}
      >
        {children}
      </div>
    </RadioContext.Provider>
  );
}

export function Radio({ value, label, description, disabled }: { value: string; label?: ReactNode; description?: ReactNode; disabled?: boolean }) {
  const ctx = useContext(RadioContext);
  const id = useId();
  const checked = ctx?.value === value;
  if (ctx?.attached) {
    return (
      <label
        htmlFor={id}
        className={cn(
          "relative inline-flex cursor-pointer items-center px-5 font-semibold text-muted-foreground transition-colors select-none hover:text-foreground",
          ctx.size === "sm" ? "h-14 text-[12px]" : "h-16 text-[13px]",
          checked && "bg-foreground text-background hover:text-background",
          disabled && "pointer-events-none opacity-50",
        )}
      >
        <input id={id} type="radio" className="sr-only" name={ctx.name} value={value} checked={checked} disabled={disabled} onChange={() => ctx.onChange?.(value)} />
        {label}
      </label>
    );
  }
  return (
    <label htmlFor={id} className={cn("inline-flex cursor-pointer items-start gap-4 text-[14px] leading-[20px]", disabled && "cursor-not-allowed opacity-60")}>
      <input id={id} type="radio" className={cn(RADIO_DOT, "mt-[2px]")} name={ctx?.name} value={value} checked={checked} disabled={disabled} onChange={() => ctx?.onChange?.(value)} />
      <span className="flex flex-col gap-[2px]">
        {label}
        {description && <span className="text-[13px] leading-[18px] text-muted-foreground">{description}</span>}
      </span>
    </label>
  );
}

/* ── TagsInput ───────────────────────────────────────────────────────────*/
export function TagsInput({
  label, description, placeholder, value = [], onChange,
}: { label?: ReactNode; description?: ReactNode; placeholder?: string; value?: string[]; onChange?: (v: string[]) => void }) {
  const id = useId();
  const [draft, setDraft] = useState("");
  const commit = () => {
    const t = draft.trim();
    if (t && !value.includes(t)) onChange?.([...value, t]);
    setDraft("");
  };
  const onKey = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); commit(); }
    else if (e.key === "Backspace" && !draft && value.length) onChange?.(value.slice(0, -1));
  };
  return (
    <Field id={id} label={label} description={description} fullWidth>
      <div className={cn(CONTROL, "flex min-h-14 flex-wrap items-center gap-2 px-[4px] py-2 focus-within:border-ring focus-within:ring-[3px] focus-within:ring-ring/40")}>
        {value.map((t) => (
          <span key={t} className="inline-flex h-9 items-center gap-2 rounded bg-muted px-[4px] font-mono text-xs">
            {t}
            <button type="button" aria-label={`Remove ${t}`} onClick={() => onChange?.(value.filter((x) => x !== t))} className="rounded text-muted-foreground hover:text-foreground">
              <X className="size-5" />
            </button>
          </span>
        ))}
        <input id={id} value={draft} placeholder={value.length ? "" : placeholder} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKey} onBlur={commit}
          className="h-9 min-w-36 flex-1 bg-transparent px-2 text-sm outline-none placeholder:text-muted-foreground" />
      </div>
    </Field>
  );
}

/* ── UploadArea ──────────────────────────────────────────────────────────*/
export function UploadArea({
  label, accept, value = [], onChange, disabled, icon, clearable = true,
}: { label?: ReactNode; accept?: string; value?: File[]; onChange?: (f: File[]) => void; disabled?: boolean; icon?: ReactNode; clearable?: boolean }) {
  const ref = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const take = (list: FileList | null) => list && onChange?.(Array.from(list).slice(0, 1));
  return (
    <div className="flex flex-col gap-3">
      <button
        type="button"
        disabled={disabled}
        onClick={() => ref.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); take(e.dataTransfer.files); }}
        className={cn(
          "flex flex-col items-center justify-center gap-3 rounded-lg border border-dashed px-6 py-9 text-sm text-muted-foreground transition-colors",
          "hover:border-ring/60 hover:bg-muted/40 disabled:opacity-50",
          over && "border-ring bg-muted/50",
        )}
      >
        {icon ?? <Upload className="size-8" />}
        <span>{label ?? "Drop a file or click to browse"}</span>
      </button>
      <input ref={ref} type="file" hidden accept={accept} onChange={(e: ChangeEvent<HTMLInputElement>) => { take(e.target.files); e.target.value = ""; }} />
      {value.map((f) => (
        <div key={f.name} className="flex items-center gap-3 rounded-md border px-5 py-3 text-sm">
          <span className="min-w-0 flex-1 truncate font-mono text-xs">{f.name}</span>
          <span className="text-xs text-muted-foreground tabular-nums">{(f.size / 1024).toFixed(0)} KB</span>
          {clearable && (
            <button type="button" aria-label="Remove file" onClick={() => onChange?.([])} className="rounded p-[2px] text-muted-foreground hover:text-foreground">
              <X className="size-[12px]" />
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

import {
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type HTMLAttributes,
  type KeyboardEvent,
  type MouseEvent,
  type ReactNode,
  type TdHTMLAttributes,
  type ThHTMLAttributes,
} from "react";
import { ArrowDown, Check, ArrowUp, ChevronDown, ChevronLeft, ChevronRight, ChevronsUpDown, ListFilter, Search, X } from "lucide-react";
import { Popover as Pop } from "radix-ui";
import { cn } from "@/lib/utils";
import { Skeleton } from "./primitives";
import { InPanel } from "./actions";
import type { Color } from "./tone";

/* ─────────────────────────────────────────────────────────────────────────
 * Column model (data mode)
 * ───────────────────────────────────────────────────────────────────────── */
export interface ColumnOption { value: string; label: ReactNode; color?: Color }
export interface Column<T extends object = Record<string, unknown>> {
  key: string;
  header?: ReactNode;
  type?: "text" | "number" | "link" | "date" | "datetime" | "status" | "custom" | string;
  accessor?: (row: T) => unknown;
  cell?: (row: T, rowIndex: number) => ReactNode;
  format?: (value: unknown, row: T) => ReactNode;
  align?: "left" | "center" | "right";
  numeric?: boolean;
  sortable?: boolean;
  sortFn?: (a: T, b: T) => number;
  filter?: { kind?: "select" | "text"; options?: ColumnOption[]; placeholder?: string };
  filterFn?: (row: T, query: unknown) => boolean;
  width?: number | string;
  minWidth?: number | string;
  maxWidth?: number | string;
  grow?: boolean;
  truncate?: boolean;
  hidden?: boolean;
  href?: (row: T) => string | undefined;
  link?: { color?: Color; target?: string; onClick?: (row: T, e: MouseEvent<HTMLElement>) => void; label?: (row: T) => ReactNode };
}
interface SortRule { key: string; dir: "asc" | "desc" }
type RowKey = string | number;

interface DataProps<T extends object, G> {
  columns: Column<T>[];
  data?: T[];
  groups?: readonly G[];
  getGroupRows?: (group: G, index: number) => T[];
  renderGroupRow?: (group: G, ctx: { expanded: boolean; count: number; toggle: () => void }) => ReactNode;
  groupKey?: keyof G | ((group: G, index: number) => RowKey);
  defaultGroupsExpanded?: boolean;
  rowKey?: keyof T | ((row: T, index: number) => RowKey);
  onRowClick?: (row: T, index: number) => void;
  globalSearch?: boolean;
  searchPlaceholder?: string;
  pagination?: boolean;
  defaultPageSize?: number;
  pageSizeOptions?: number[];
  defaultSort?: SortRule[];
  loading?: boolean;
  emptyState?: ReactNode;
  emptyText?: ReactNode;
  stickyHeader?: boolean;
  striped?: boolean;
  hoverable?: boolean;
  bordered?: boolean | "all";
  scrollable?: boolean;
  toolbar?: ReactNode;
  style?: CSSProperties;
  className?: string;
}

function valueOf<T extends object>(c: Column<T>, row: T): unknown {
  return c.accessor ? c.accessor(row) : (row as Record<string, unknown>)[c.key];
}
function text(v: unknown): string {
  if (v == null) return "";
  if (Array.isArray(v)) return v.map(text).join(" ");
  if (typeof v === "object") return "";
  return String(v);
}
function compare(a: unknown, b: unknown): number {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}
function px(v?: number | string) {
  return typeof v === "number" ? `${v}px` : v;
}

/* Header cell layout shared by data and compositional modes. */
/** Unpaginated lists longer than this render as a window of rows. */
const VIRTUAL_THRESHOLD = 200;
const OVERSCAN = 20;

const TH = "h-18 px-6 text-left align-middle text-[12px] font-semibold font-mono uppercase tracking-[0.06em] text-muted-foreground whitespace-nowrap";
const TD = "h-20 px-6 align-middle text-[13px] leading-[18px]";

function DataTable<T extends object, G>({
  columns, data, groups, getGroupRows, renderGroupRow, groupKey, defaultGroupsExpanded = true, rowKey, onRowClick,
  globalSearch, searchPlaceholder = "Search…", pagination, defaultPageSize = 25, pageSizeOptions = [10, 25, 50, 100],
  defaultSort, loading, emptyState, emptyText = "Nothing here yet.", stickyHeader, hoverable = true, toolbar, style, className,
}: DataProps<T, G>) {
  const cols = useMemo(() => columns.filter((c) => !c.hidden), [columns]);
  const nested = useContext(InPanel);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<SortRule | null>(defaultSort?.[0] ?? null);
  const [filters, setFilters] = useState<Record<string, string[]>>({});
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(defaultPageSize);
  const [collapsed, setCollapsed] = useState<Set<RowKey>>(new Set());
  const [touched, setTouched] = useState(false);

  const keyOf = (row: T, i: number): RowKey =>
    typeof rowKey === "function" ? rowKey(row, i) : rowKey ? (row[rowKey] as RowKey) : i;
  const gKey = (g: G, i: number): RowKey =>
    typeof groupKey === "function" ? groupKey(g, i) : groupKey ? (g[groupKey] as RowKey) : i;

  /* filter → search → sort, applied to a row set */
  const pipeline = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return (rows: T[]) => {
      let out = rows.filter((r) =>
        cols.every((c) => {
          const sel = filters[c.key];
          if (!sel || sel.length === 0) return true;
          if (c.filterFn) return c.filterFn(r, sel);
          return sel.includes(text(valueOf(c, r)).toLowerCase()) || sel.includes(text(valueOf(c, r)));
        }),
      );
      if (ql) out = out.filter((r) => cols.some((c) => text(valueOf(c, r)).toLowerCase().includes(ql)));
      if (sort) {
        const c = cols.find((x) => x.key === sort.key);
        if (c) {
          const m = sort.dir === "asc" ? 1 : -1;
          out = out.slice().sort((a, b) => m * (c.sortFn ? c.sortFn(a, b) : compare(valueOf(c, a), valueOf(c, b))));
        }
      }
      return out;
    };
  }, [q, sort, filters, cols]);

  const grouped = !!groups && !!getGroupRows;
  const flatRows = useMemo(() => (grouped ? [] : pipeline(data ?? [])), [grouped, pipeline, data]);
  const groupRows = useMemo(
    () => (grouped ? groups!.map((g, i) => ({ g, key: gKey(g, i), rows: pipeline(getGroupRows!(g, i)) })).filter((x) => x.rows.length > 0) : []),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [grouped, groups, getGroupRows, pipeline],
  );
  const total = grouped ? groupRows.reduce((n, g) => n + g.rows.length, 0) : flatRows.length;
  const pages = Math.max(1, Math.ceil(flatRows.length / pageSize));
  const safePage = Math.min(page, pages - 1);
  const visible = pagination && !grouped ? flatRows.slice(safePage * pageSize, safePage * pageSize + pageSize) : flatRows;

  /* Windowing: long unpaginated lists render only the rows in view (plus an
   * overscan) between two spacer rows, measured against the scrolling parent. */
  const virtual = !pagination && !grouped && !loading && visible.length > VIRTUAL_THRESHOLD;
  const tbodyRef = useRef<HTMLTableSectionElement>(null);
  const rowH = useRef(30);
  const [win, setWin] = useState({ start: 0, end: 80 });
  useEffect(() => {
    if (!virtual) return;
    const body = tbodyRef.current;
    if (!body) return;
    let scroller: HTMLElement | null = body.parentElement;
    while (scroller && !/(auto|scroll)/.test(getComputedStyle(scroller).overflowY)) scroller = scroller.parentElement;
    const target: HTMLElement | Window = scroller ?? window;
    let raf = 0;
    const measure = () => {
      raf = 0;
      const first = body.querySelector<HTMLTableRowElement>("tr[data-row]");
      if (first && first.offsetHeight) rowH.current = first.offsetHeight;
      const viewTop = scroller ? scroller.getBoundingClientRect().top : 0;
      const viewH = scroller ? scroller.clientHeight : window.innerHeight;
      const offset = viewTop - body.getBoundingClientRect().top;
      const start = Math.max(0, Math.floor(offset / rowH.current) - OVERSCAN);
      const end = Math.min(visible.length, Math.ceil((offset + viewH) / rowH.current) + OVERSCAN);
      setWin((w) => (w.start === start && w.end === end ? w : { start, end }));
    };
    const onScroll = () => {
      if (!raf) raf = requestAnimationFrame(measure);
    };
    measure();
    target.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", onScroll);
    return () => {
      target.removeEventListener("scroll", onScroll);
      window.removeEventListener("resize", onScroll);
      if (raf) cancelAnimationFrame(raf);
    };
  }, [virtual, visible.length]);

  const filterCols = cols.filter((c) => c.filter?.kind === "select" && c.filter.options?.length);
  const activeFilters = Object.values(filters).some((v) => v.length > 0);
  const showToolbar = globalSearch || filterCols.length > 0 || toolbar;

  const toggleSort = (c: Column<T>) => {
    if (!c.sortable) return;
    setSort((s) => (s?.key !== c.key ? { key: c.key, dir: "asc" } : s.dir === "asc" ? { key: c.key, dir: "desc" } : null));
  };

  const renderCell = (c: Column<T>, row: T, i: number): ReactNode => {
    if (c.cell) return c.cell(row, i);
    const v = valueOf(c, row);
    if (c.type === "link") {
      const href = c.href?.(row) ?? (typeof v === "string" ? v : undefined);
      const label = c.link?.label?.(row) ?? text(v);
      return (
        <a
          href={href}
          target={c.link?.onClick ? undefined : c.link?.target}
          className="row-link"
          onClick={(e) => {
            e.stopPropagation();
            // Let cmd/ctrl/shift/middle clicks reach the browser (open in new tab).
            if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
            c.link?.onClick?.(row, e);
          }}
        >
          {label}
        </a>
      );
    }
    if (c.format) return c.format(v, row);
    return text(v) || <span className="text-muted-foreground">—</span>;
  };

  const rowProps = (row: T, i: number) =>
    onRowClick
      ? {
          tabIndex: 0,
          role: "link" as const,
          onClick: () => onRowClick(row, i),
          onKeyDown: (e: KeyboardEvent<HTMLTableRowElement>) => {
            if (e.key === "Enter" && e.target === e.currentTarget) onRowClick(row, i);
          },
          className: "cursor-pointer focus-visible:bg-accent/70 outline-none",
        }
      : {};

  // `collapsed` rows (a closed group) stay in the DOM with `visibility: collapse`:
  // hidden, but still measured, so opening a group never resizes the columns.
  const renderRows = (rows: T[], offset = 0, collapsed = false) =>
    rows.map((row, j) => {
      const i = j + offset;
      const rp = rowProps(row, i);
      return (
        <tr
          key={keyOf(row, i)}
          data-row={collapsed ? undefined : true}
          {...rp}
          {...(collapsed && { "aria-hidden": true, inert: true, onClick: undefined })}
          className={cn("transition-colors duration-75", hoverable && "hover:bg-accent/60", rp.className, collapsed && "[visibility:collapse]")}
        >
          {cols.map((c) => (
            <td
              key={c.key}
              data-label={c.key === "actions" ? "" : typeof c.header === "string" ? c.header : c.key}
              className={cn(
                TD,
                (c.align === "right" || c.numeric) && "text-right",
                c.align === "center" && "text-center",
                c.numeric && "font-mono tabular-nums",
                (c.truncate || c.maxWidth) && "max-w-0 truncate",
                !c.grow && !c.truncate && "whitespace-nowrap",
              )}
              style={{ width: px(c.width) ?? (c.grow ? "auto" : undefined), minWidth: px(c.minWidth), maxWidth: px(c.maxWidth) }}
            >
              {renderCell(c, row, i)}
            </td>
          ))}
        </tr>
      );
    });

  const empty = !loading && total === 0;

  return (
    <div
      className={cn(
        // kit-table: stacked cards when the table's own box is narrow (globals.css).
        "kit-table flex min-w-0 flex-col [&_td:first-child]:pl-8 [&_th:first-child]:pl-8 [&_td:last-child]:pr-8 [&_th:last-child]:pr-8",
        !nested && "shadow-[inset_0_0_0_1px_var(--border)] bg-card",
        className,
      )}
      style={style}
    >
      {showToolbar && (
        <div className="flex flex-wrap items-center gap-3 border-b px-[var(--page-px,12px)] py-3">
          {globalSearch && (
            <label className="relative flex min-w-72 flex-1 items-center sm:max-w-xs">
              <Search className="pointer-events-none absolute left-[8px] size-[12px] text-muted-foreground" aria-hidden />
              <span className="sr-only">Search rows</span>
              <input
                value={q}
                onChange={(e) => { setQ(e.target.value); setPage(0); }}
                placeholder={searchPlaceholder}
                className="h-14 w-full border border-[var(--border)] bg-[var(--popover)] pr-11 pl-12 text-[13px] outline-none placeholder:text-muted-foreground hover:border-[var(--input)] focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal"
              />
              {q && (
                <button type="button" aria-label="Clear search" onClick={() => { setQ(""); setPage(0); }} className="absolute right-[4px] rounded p-[2px] text-muted-foreground hover:text-foreground">
                  <X className="size-[12px]" />
                </button>
              )}
            </label>
          )}
          {filterCols.map((c) => (
            <FilterChip
              key={c.key}
              label={typeof c.header === "string" ? c.header : c.key}
              options={c.filter!.options!}
              value={filters[c.key] ?? []}
              onChange={(v) => { setFilters((f) => ({ ...f, [c.key]: v })); setPage(0); }}
            />
          ))}
          {activeFilters && (
            <button type="button" onClick={() => setFilters({})} className="h-12 rounded-md px-3 text-[13px] text-muted-foreground hover:bg-accent hover:text-foreground">
              Reset
            </button>
          )}
          <span className="ml-auto flex items-center gap-3">
            {toolbar}
            {!loading && <span className="font-mono text-xs text-muted-foreground tabular-nums">{total} {total === 1 ? "row" : "rows"}</span>}
          </span>
        </div>
      )}
      <div className="min-w-0 overflow-x-auto">
        <table className="w-full min-w-[640px] border-separate border-spacing-0">
          <thead className={cn("border-b bg-sunken/70", stickyHeader && "sticky top-0 z-10")}>
            <tr>
              {cols.map((c) => {
                const active = sort?.key === c.key;
                return (
                  <th
                    key={c.key}
                    scope="col"
                    aria-sort={active ? (sort!.dir === "asc" ? "ascending" : "descending") : undefined}
                    className={cn(TH, (c.align === "right" || c.numeric) && "text-right", c.align === "center" && "text-center")}
                    style={{ width: px(c.width), minWidth: px(c.minWidth), maxWidth: px(c.maxWidth) }}
                  >
                    {c.sortable ? (
                      <button
                        type="button"
                        onClick={() => toggleSort(c)}
                        className={cn("-mx-2 inline-flex items-center gap-2 rounded px-2 font-mono uppercase outline-none hover:text-foreground focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal", active && "text-foreground")}
                      >
                        {c.header ?? c.key}
                        {active ? (sort!.dir === "asc" ? <ArrowUp className="size-5" /> : <ArrowDown className="size-5" />) : <ChevronsUpDown className="size-5 opacity-40" />}
                      </button>
                    ) : (
                      c.header ?? (c.key === "actions" ? <span className="sr-only">Actions</span> : c.key)
                    )}
                  </th>
                );
              })}
            </tr>
          </thead>
          <tbody ref={tbodyRef}>
            {loading &&
              Array.from({ length: 6 }).map((_, i) => (
                <tr key={i}>
                  {cols.map((c) => (
                    <td key={c.key} className={TD}><Skeleton height={12} width={c.key === "actions" ? 48 : `${50 + ((i * 17 + c.key.length * 13) % 40)}%`} /></td>
                  ))}
                </tr>
              ))}
            {!loading && !grouped && !virtual && renderRows(visible)}
            {virtual && (
              <>
                {win.start > 0 && <tr aria-hidden style={{ height: win.start * rowH.current }} />}
                {renderRows(visible.slice(win.start, win.end), win.start)}
                {win.end < visible.length && <tr aria-hidden style={{ height: (visible.length - win.end) * rowH.current }} />}
              </>
            )}
            {!loading &&
              grouped &&
              groupRows.map(({ g, key, rows }) => {
                const open = defaultGroupsExpanded ? !collapsed.has(key) : touched && collapsed.has(key);
                const toggle = () => {
                  setTouched(true);
                  setCollapsed((s) => {
                    const n = new Set(s);
                    if (n.has(key)) n.delete(key);
                    else n.add(key);
                    return n;
                  });
                };
                return [
                  <tr key={`g:${key}`} className="bg-muted/40">
                    <td colSpan={cols.length} className="h-14 px-3">
                      <button type="button" aria-expanded={open} onClick={toggle} className="flex w-full items-center gap-3 rounded px-2 text-left outline-none focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal">
                        <ChevronDown className={cn("size-[12px] text-muted-foreground transition-transform", !open && "-rotate-90")} />
                        {renderGroupRow ? renderGroupRow(g, { expanded: open, count: rows.length, toggle }) : String(key)}
                      </button>
                    </td>
                  </tr>,
                  ...renderRows(rows, 0, !open),
                ];
              })}
            {empty && (
              <tr>
                <td colSpan={cols.length} className="px-9 py-18 text-center">
                  {q || activeFilters ? (
                    <div className="flex flex-col items-center gap-3 text-sm text-muted-foreground">
                      <span>No rows match the current search or filters.</span>
                      <button type="button" className="cell-link" onClick={() => { setQ(""); setFilters({}); setPage(0); }}>Clear search and filters</button>
                    </div>
                  ) : (
                    emptyState ?? <span className="text-sm text-muted-foreground">{emptyText}</span>
                  )}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {pagination && !grouped && !loading && flatRows.length > pageSizeOptions[0] && (
        <div className="flex items-center gap-5 border-t px-[var(--page-px,12px)] py-3 text-xs text-muted-foreground">
          <label className="flex items-center gap-3">
            Rows per page
            <select
              value={pageSize}
              onChange={(e) => { setPageSize(Number(e.target.value)); setPage(0); }}
              className="h-14 border border-[var(--border)] bg-[var(--popover)] px-3 text-[13px] text-foreground outline-none hover:border-[var(--input)] focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal"
            >
              {pageSizeOptions.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <span className="ml-auto font-mono tabular-nums">
            {safePage * pageSize + 1}–{Math.min(flatRows.length, (safePage + 1) * pageSize)} of {flatRows.length}
          </span>
          <button type="button" aria-label="Previous page" disabled={safePage === 0} onClick={() => setPage(safePage - 1)} className="rounded-md p-2 hover:bg-accent disabled:opacity-40">
            <ChevronLeft className="size-6" />
          </button>
          <button type="button" aria-label="Next page" disabled={safePage >= pages - 1} onClick={() => setPage(safePage + 1)} className="rounded-md p-2 hover:bg-accent disabled:opacity-40">
            <ChevronRight className="size-6" />
          </button>
        </div>
      )}
    </div>
  );
}

function FilterChip({ label, options, value, onChange }: { label: string; options: ColumnOption[]; value: string[]; onChange: (v: string[]) => void }) {
  const on = value.length > 0;
  const summary = on
    ? value.length === 1
      ? (options.find((o) => o.value === value[0])?.label ?? value[0])
      : `${value.length} selected`
    : "Any";
  return (
    <Pop.Root>
      <Pop.Trigger
        className={cn(
          "inline-flex h-14 items-center gap-3 border border-dashed border-[var(--input)] px-5 text-[13px] outline-none transition-colors hover:bg-accent focus-visible:outline-solid focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal",
          on && "border-solid border-ring/50 bg-[color-mix(in_oklab,var(--signal)_8%,transparent)]",
        )}
      >
        <ListFilter className="size-[12px] text-muted-foreground" aria-hidden />
        <span className="text-muted-foreground">{label}</span>
        <span className="font-medium">{summary}</span>
      </Pop.Trigger>
      <Pop.Portal>
        <Pop.Content align="start" sideOffset={6} className="anim-pop z-50 min-w-72 rounded-lg border bg-popover p-2 text-popover-foreground shadow-[0_12px_32px_-8px_rgb(0_0_0/0.35)]">
          {options.map((o) => {
            const checked = value.includes(o.value);
            return (
              <label key={o.value} className="flex h-16 cursor-pointer items-center gap-4 px-3 text-[13px] hover:bg-accent">
                <span className="relative inline-flex size-8 shrink-0">
                  <input
                    type="checkbox"
                    className="peer size-8 shrink-0 cursor-pointer appearance-none border border-[var(--input)] bg-[var(--popover)] checked:border-foreground checked:bg-foreground"
                    checked={checked}
                    onChange={() => onChange(checked ? value.filter((v) => v !== o.value) : [...value, o.value])}
                  />
                  <Check aria-hidden strokeWidth={2.5} className="pointer-events-none absolute inset-[2px] hidden size-[12px] text-background peer-checked:block" />
                </span>
                {o.label}
              </label>
            );
          })}
          {value.length > 0 && (
            <button type="button" onClick={() => onChange([])} className="mt-2 w-full rounded-sm border-t px-3 py-[4px] text-left text-xs text-muted-foreground hover:bg-accent">
              Clear filter
            </button>
          )}
        </Pop.Content>
      </Pop.Portal>
    </Pop.Root>
  );
}

/* ─────────────────────────────────────────────────────────────────────────
 * Compositional mode: <Table><Table.Header><Table.Row><Table.Head/>…
 * Used for key/value detail tables and small hand-built lists.
 * ───────────────────────────────────────────────────────────────────────── */
function TableShell({ children, bordered = true, className, style }: { children?: ReactNode; bordered?: boolean | "all"; className?: string; style?: CSSProperties; striped?: boolean; hoverable?: boolean; stickyHeader?: boolean }) {
  const nested = useContext(InPanel);
  return (
    <div className={cn("min-w-0 overflow-x-auto", bordered && !nested && "frame bg-card", className)} style={style}>
      <table className="w-full border-separate border-spacing-0">{children}</table>
    </div>
  );
}
function THeader(p: HTMLAttributes<HTMLTableSectionElement>) {
  return <thead {...p} className={cn("border-b bg-sunken/70", p.className)} />;
}
function TBody(p: HTMLAttributes<HTMLTableSectionElement>) {
  return <tbody {...p} />;
}
function TRow({ selected, color: _c, className, ...p }: HTMLAttributes<HTMLTableRowElement> & { selected?: boolean; color?: unknown }) {
  return (
    <tr
      {...p}
      aria-selected={selected || undefined}
      className={cn(p.onClick && "cursor-pointer hover:bg-accent/45", selected && "bg-accent/70", className)}
    />
  );
}
function THead({ align, shrink, minWidth, className, style, ...p }: ThHTMLAttributes<HTMLTableCellElement> & { align?: "left" | "right" | "center"; shrink?: boolean; minWidth?: number | string }) {
  return (
    <th
      scope="col"
      {...p}
      className={cn(TH, align === "right" && "text-right", align === "center" && "text-center", shrink && "w-px", className)}
      style={{ minWidth: px(minWidth), ...style }}
    />
  );
}
function TCell({ align, shrink, className, ...p }: TdHTMLAttributes<HTMLTableCellElement> & { align?: "left" | "right" | "center"; shrink?: boolean }) {
  return (
    <td
      {...p}
      className={cn("px-5 py-3 align-middle text-[13px]", align === "right" && "text-right", align === "center" && "text-center", shrink && "w-px whitespace-nowrap", className)}
    />
  );
}

/* ─────────────────────────────────────────────────────────────────────────
 * Public Table: data mode when `columns` is passed, compositional otherwise.
 * ───────────────────────────────────────────────────────────────────────── */
type TableProps<T extends object, G> =
  | (DataProps<T, G> & { children?: never })
  | { columns?: undefined; children?: ReactNode; bordered?: boolean | "all"; striped?: boolean; hoverable?: boolean; stickyHeader?: boolean; scrollable?: boolean; className?: string; style?: CSSProperties };

function TableRoot<T extends object = Record<string, unknown>, G = unknown>(props: TableProps<T, G>) {
  if (props.columns) return <DataTable<T, G> {...(props as DataProps<T, G>)} />;
  const p = props as { children?: ReactNode; bordered?: boolean | "all"; className?: string; style?: CSSProperties };
  return <TableShell bordered={p.bordered} className={p.className} style={p.style}>{p.children}</TableShell>;
}

export const Table = Object.assign(TableRoot, { Header: THeader, Body: TBody, Row: TRow, Head: THead, Cell: TCell });

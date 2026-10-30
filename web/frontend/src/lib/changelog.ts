/**
 * The changelog (docs/CHANGELOG.md), bundled into the SPA at build time so a
 * server shows the one of its own version, offline too. A small reader for the
 * Markdown it uses: "## <version> — <date>" sections; what changes for the
 * user first, then "### Other changes" (fixes and internal work); items as
 * "- **Title.** Description", plus paragraphs and bold subheadings.
 */
import raw from "virtual:changelog";

export interface Item {
  /** The bold lead ("CPU is a share of the whole host"), without its period. */
  title: string;
  /** What follows it; may hold `code`, **bold** and [links](url). */
  text: string;
}

export type Block =
  | { kind: "p"; text: string }
  | { kind: "h"; text: string }
  | { kind: "ul" | "ol"; items: Item[] };

export interface Release {
  /** "0.1.12", or "" for the dated notes of the work before 0.1.0. */
  version: string;
  /** The heading's date (YYYY-MM-DD), if it has one. */
  date: string;
  /** What follows the date in a heading without a version. */
  title: string;
  /** What changes for the user. */
  blocks: Block[];
  /** "### Other changes": stability fixes, internal work. */
  other: Block[];
}

const HEADING = /^##\s+(.*)$/;
const SUBHEADING = /^###\s+/;
const VERSION = /^v?(\d+\.\d+\.\d+\S*)\s*[—–-]\s*(\d{4}-\d{2}-\d{2})?\s*(.*)$/;
const DATED = /^(\d{4}-\d{2}-\d{2})\s*[—–-]\s*(.*)$/;
const BULLET = /^\s*[-*]\s+(.*)$/;
const NUMBERED = /^\s*\d+\.\s+(.*)$/;
const BOLD_LINE = /^\*\*([^*]+)\*\*:?$/;
// "**Title.** text": a bold lead that ends a sentence becomes the item's title.
const LEAD = /^\*\*([^*]+?)[.!?]\*\*\s*(.*)$/;

function item(text: string): Item {
  const m = LEAD.exec(text);
  return m ? { title: m[1].trim(), text: m[2].trim() } : { title: "", text };
}

export function parseChangelog(text: string): Release[] {
  const out: Release[] = [];
  let cur: Release | null = null;
  let into: Block[] = [];
  let list: Extract<Block, { items: Item[] }> | null = null;
  let para: string[] = [];

  const flush = () => {
    if (para.length) into.push({ kind: "p", text: para.join(" ") });
    para = [];
  };
  const wrapped = () => {
    // Items are parsed once whole, so a lead split over lines still counts.
    if (list) list.items = list.items.map((it) => (it.title ? it : item(it.text)));
  };
  for (const line of text.split(/\r?\n/)) {
    const h = HEADING.exec(line);
    if (h) {
      flush();
      wrapped();
      list = null;
      const v = VERSION.exec(h[1].trim());
      const d = v ? null : DATED.exec(h[1].trim());
      cur = v
        ? { version: v[1], date: v[2] ?? "", title: v[3].trim(), blocks: [], other: [] }
        : { version: "", date: d?.[1] ?? "", title: d ? d[2].trim() : h[1].trim(), blocks: [], other: [] };
      into = cur.blocks;
      out.push(cur);
      continue;
    }
    if (!cur) continue; // the "# title" and intro above the first section
    const t = line.trim();
    if (SUBHEADING.test(line)) {
      flush();
      wrapped();
      list = null;
      into = cur.other;
      continue;
    }
    if (!t) {
      flush();
      wrapped();
      list = null;
      continue;
    }
    const b = BULLET.exec(line);
    const n = b ? null : NUMBERED.exec(line);
    if (b || n) {
      flush();
      wrapped();
      const kind = b ? "ul" : "ol";
      if (!list || list.kind !== kind) {
        list = { kind, items: [] };
        into.push(list);
      }
      list.items.push({ title: "", text: (b ?? n)![1].trim() });
      continue;
    }
    if (list && /^\s+\S/.test(line)) {
      // A wrapped list item.
      list.items[list.items.length - 1].text += ` ${t}`;
      continue;
    }
    const bold = BOLD_LINE.exec(t);
    if (bold) {
      flush();
      wrapped();
      list = null;
      into.push({ kind: "h", text: bold[1].trim() });
      continue;
    }
    wrapped();
    list = null;
    para.push(t);
  }
  flush();
  wrapped();
  return out;
}

export type Inline = { kind: "text" | "code" | "bold"; text: string } | { kind: "link"; text: string; href: string };

const INLINE = /`([^`]+)`|\*\*([^*]+)\*\*|\[([^\]]+)\]\(([^)\s]+)\)/g;

/** Split a line into plain text, `code`, **bold** and [links](url). */
export function inlineParts(text: string): Inline[] {
  const parts: Inline[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index! > last) parts.push({ kind: "text", text: text.slice(last, m.index) });
    if (m[1] != null) parts.push({ kind: "code", text: m[1] });
    else if (m[2] != null) parts.push({ kind: "bold", text: m[2] });
    else parts.push({ kind: "link", text: m[3], href: m[4] });
    last = m.index! + m[0].length;
  }
  if (last < text.length) parts.push({ kind: "text", text: text.slice(last) });
  return parts;
}

/** The bundled changelog's releases, newest first (the dated notes of the
 *  development before 0.1.0 stay in the file, out of the page). */
export const RELEASES: Release[] = parseChangelog(raw).filter((r) => r.version);

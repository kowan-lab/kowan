/**
 * Ranking for the ⌘K palette. Pure, so the order can be tested without a DOM.
 *
 * Every word of the query must match the entry somewhere. The name counts most
 * (whole name, then its start, the start of one of its words, anywhere in it,
 * then its letters in order); the other terms (image, kind, host, keywords)
 * count less. Each kind then adds a small weight, so a workload outranks a
 * page that matches as well, and an entry that needs attention adds a little.
 */

export type PaletteKind = "workload" | "container" | "host" | "page" | "action";

export interface PaletteEntry {
  key: string;
  kind: PaletteKind;
  title: string;
  /** Secondary text the query may match on. */
  terms: string[];
  /** Added to the score: 2 for a workload or container that needs attention. */
  boost?: number;
}

export interface Ranked<T extends PaletteEntry> {
  entry: T;
  score: number;
}

const KIND_WEIGHT: Record<PaletteKind, number> = { workload: 6, container: 4, page: 3, host: 3, action: 1 };
const WORD = /[\s\-_./:~@]+/;

/**
 * Letters of `needle` appear in `hay` in order and close together ("ngx" in
 * "nginx"), so a few letters scattered over a long name do not count.
 */
function subsequence(needle: string, hay: string): boolean {
  for (let start = hay.indexOf(needle[0]); start >= 0; start = hay.indexOf(needle[0], start + 1)) {
    let i = 1;
    let j = start + 1;
    for (; j < hay.length && i < needle.length && j - start < needle.length + 2; j++) if (hay[j] === needle[i]) i++;
    if (i === needle.length) return true;
  }
  return false;
}

function titleScore(token: string, title: string, words: string[], whole: boolean): number {
  if (whole && title === token) return 100;
  if (title.startsWith(token)) return 80;
  if (words.some((w) => w.startsWith(token))) return 60;
  if (title.includes(token)) return 40;
  if (token.length >= 2 && subsequence(token, title)) return 12;
  return 0;
}

function termScore(token: string, terms: string[]): number {
  let best = 0;
  for (const t of terms) {
    if (t.split(WORD).some((w) => w.startsWith(token))) return 25;
    if (t.includes(token)) best = 15;
  }
  return best;
}

/** The entry's score for `query`; 0 when some word of the query does not match. */
export function scoreEntry(entry: PaletteEntry, query: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 0;
  const title = entry.title.toLowerCase();
  const words = title.split(WORD);
  const terms = entry.terms.filter(Boolean).map((t) => t.toLowerCase());
  const tokens = q.split(/\s+/);
  let sum = 0;
  for (const token of tokens) {
    const s = Math.max(titleScore(token, title, words, tokens.length === 1), termScore(token, terms));
    if (s === 0) return 0;
    sum += s;
  }
  if (tokens.length > 1 && title === q) sum += 40;
  return sum / tokens.length + KIND_WEIGHT[entry.kind] + (entry.boost ?? 0);
}

/** Entries that match `query`, best first; ties keep their original order. */
export function rank<T extends PaletteEntry>(entries: T[], query: string): Ranked<T>[] {
  return entries
    .map((entry, i) => ({ entry, score: scoreEntry(entry, query), i }))
    .filter((r) => r.score > 0)
    .sort((a, b) => b.score - a.score || a.i - b.i)
    .map(({ entry, score }) => ({ entry, score }));
}

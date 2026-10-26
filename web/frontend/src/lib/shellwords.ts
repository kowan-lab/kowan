// Command lines edited as one text field: split with shell-like quoting and
// joined back so the round trip keeps every argument intact.

/**
 * Split a free-text command into tokens with shell-like quoting. Single and
 * double quotes group whitespace, and a backslash escapes the next character.
 * e.g. `nginx -g 'daemon off;'` -> ["nginx", "-g", "daemon off;"].
 */
export function splitWords(s: string): string[] {
  const tokens: string[] = [];
  let current = "";
  let quote: '"' | "'" | null = null;
  let started = false; // tracks an in-progress token, so "" quotes still emit
  for (let i = 0; i < s.length; i += 1) {
    const ch = s[i];
    if (quote) {
      if (ch === quote) {
        quote = null;
      } else if (ch === "\\" && quote === '"' && i + 1 < s.length) {
        // Inside double quotes a backslash escapes the following character.
        i += 1;
        current += s[i];
      } else {
        current += ch;
      }
      continue;
    }
    if (ch === '"' || ch === "'") {
      quote = ch;
      started = true;
      continue;
    }
    if (ch === "\\" && i + 1 < s.length) {
      i += 1;
      current += s[i];
      started = true;
      continue;
    }
    if (/\s/.test(ch)) {
      if (started) {
        tokens.push(current);
        current = "";
        started = false;
      }
      continue;
    }
    current += ch;
    started = true;
  }
  if (started) tokens.push(current);
  return tokens;
}

/** Quote one command token for splitWords when it needs it. */
function quoteWord(t: string): string {
  return t === "" || /[\s'"\\]/.test(t) ? `"${t.replace(/(["\\])/g, "\\$1")}"` : t;
}

/** Join argv into one editable line that splitWords turns back into argv. */
export function joinWords(argv: string[]): string {
  return argv.map(quoteWord).join(" ");
}

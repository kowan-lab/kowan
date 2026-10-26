import { useEffect, useRef } from "react";
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { yaml } from "@codemirror/lang-yaml";
import { EditorSelection, type Text as CmText } from "@codemirror/state";
import { EditorView } from "@codemirror/view";
// @codemirror/lint ships with @uiw/react-codemirror's basic setup (a
// transitive dependency), so the editor gains inline diagnostics without a
// new package.
import { lintGutter, setDiagnostics, type Diagnostic as CmDiagnostic } from "@codemirror/lint";
import { useTheme } from "@/ui/theme";

/**
 * YAML editor (CodeMirror). Its own module so the ~500 KB editor bundle loads
 * only when someone switches a form to YAML mode.
 *
 * Optional `diagnostics` (1-based line/column) render as inline lint marks
 * with a gutter; `jumpTo` moves the cursor to a position and scrolls it into
 * view whenever its `nonce` changes.
 */
export interface EditorDiagnostic {
  line: number;
  column: number;
  severity: "error" | "warning" | "info";
  message: string;
  source?: string;
}

export interface EditorJump {
  line: number;
  column: number;
  nonce: number;
}

const baseExtensions = [yaml()];
const lintExtensions = [yaml(), lintGutter()];

function posOf(doc: CmText, line: number, column: number): { from: number; to: number } {
  const ln = doc.line(Math.min(Math.max(line, 1), doc.lines));
  const from = Math.min(ln.from + Math.max(column - 1, 0), ln.to);
  // Underline to the end of the token (or at least one character).
  const rest = doc.sliceString(from, ln.to);
  const m = /^\S+/.exec(rest);
  let to = from + (m ? m[0].length : 0);
  if (to === from) {
    if (from < ln.to) to = from + 1;
    else return { from: Math.max(ln.from, from - 1), to: from };
  }
  return { from, to };
}

export default function YamlEditor({
  value,
  onChange,
  minHeight = "360px",
  diagnostics,
  jumpTo,
}: {
  value: string;
  onChange: (v: string) => void;
  minHeight?: string;
  diagnostics?: EditorDiagnostic[];
  jumpTo?: EditorJump | null;
}) {
  const { theme } = useTheme();
  const ref = useRef<ReactCodeMirrorRef>(null);
  const linting = diagnostics !== undefined;

  const latest = useRef({ diagnostics, jumpTo, linting });
  latest.current = { diagnostics, jumpTo, linting };
  const jumped = useRef<number | null>(null);

  // Push diagnostics into the lint state. They describe a specific text; if
  // the user has typed since, positions are clamped to the current document.
  const pushDiagnostics = (view: EditorView) => {
    const { diagnostics: ds, linting: on } = latest.current;
    if (!on) return;
    const doc = view.state.doc;
    const cm: CmDiagnostic[] = (ds ?? [])
      .filter((d) => d.line > 0)
      .map((d) => ({ ...posOf(doc, d.line, d.column), severity: d.severity, message: d.message, source: d.source }));
    view.dispatch(setDiagnostics(view.state, cm));
  };

  // Jump once per nonce (the same position can be revisited with a new one).
  const doJump = (view: EditorView) => {
    const j = latest.current.jumpTo;
    if (!j || j.line <= 0 || jumped.current === j.nonce) return;
    jumped.current = j.nonce;
    const { from } = posOf(view.state.doc, j.line, j.column);
    view.dispatch({ selection: EditorSelection.cursor(from), effects: EditorView.scrollIntoView(from, { y: "center" }) });
    view.focus();
  };

  useEffect(() => {
    const view = ref.current?.view;
    if (view) pushDiagnostics(view);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [diagnostics, linting, value]);

  useEffect(() => {
    const view = ref.current?.view;
    if (view) doJump(view);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jumpTo?.nonce]);

  return (
    <CodeMirror
      ref={ref}
      value={value}
      onChange={onChange}
      extensions={linting ? lintExtensions : baseExtensions}
      theme={theme}
      minHeight={minHeight}
      basicSetup={{ lineNumbers: true }}
      onCreateEditor={(view) => {
        // The editor mounts lazily (e.g. right after "jump to line" switched
        // the form to YAML mode): apply what arrived before it existed.
        pushDiagnostics(view);
        doJump(view);
      }}
      style={{ fontFamily: "'Geist Mono Variable', ui-monospace, monospace", fontSize: 12, border: "1px solid var(--border)", overflow: "hidden" }}
    />
  );
}

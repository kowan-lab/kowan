import { isScalar, parseDocument, Scalar } from "yaml";
import type { ComposePatchOp } from "../../types";

/**
 * Apply a diagnostic's fix patch to compose YAML text.
 *
 * Works on the YAML *document* (yaml's Document API), so comments, ordering
 * and formatting of untouched nodes are preserved — the same operation
 * semantics as the server's kowan/compose/patch.py:
 *   set:    create/replace the node at path (missing maps are created)
 *   remove: delete the mapping key / sequence item
 * Returns null when the text no longer parses (the fix can't be applied).
 */
export function applyComposePatch(text: string, patch: ComposePatchOp[]): string | null {
  const doc = parseDocument(text);
  if (doc.errors.length > 0) return null;
  for (const op of patch) {
    if (op.path.length === 0) continue;
    if (op.op === "set") {
      const node = doc.createNode(op.value);
      // "8080:80" is a string in YAML 1.2 but a base-60 number to YAML 1.1
      // readers: quote digit-and-colon strings, as the compose docs advise.
      if (isScalar(node) && typeof op.value === "string" && /^[\d:./-]+$/.test(op.value)) {
        node.type = Scalar.QUOTE_DOUBLE;
      }
      doc.setIn(op.path, node);
    } else if (op.op === "remove") {
      if (doc.hasIn(op.path)) doc.deleteIn(op.path);
    }
  }
  return String(doc);
}

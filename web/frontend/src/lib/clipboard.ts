import { notify } from "../ui/toast";

/**
 * Copy text to the clipboard; resolves true when it worked.
 *
 * `navigator.clipboard` exists only in a secure context (HTTPS or localhost),
 * so on a server reached as http://<address> it is missing. There the copy
 * falls back to selecting a hidden textarea and `execCommand("copy")`; the
 * textarea goes inside the open dialog, whose focus trap would otherwise pull
 * focus back and leave nothing selected. When both fail, a toast says so.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Denied or unavailable: try the fallback.
  }
  if (legacyCopy(text)) return true;
  notify({ color: "red", title: "Could not copy", message: "Select the text and copy it by hand." });
  return false;
}

function legacyCopy(text: string): boolean {
  const active = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const host = active?.closest('[role="dialog"]') ?? document.body;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.setAttribute("aria-hidden", "true");
  area.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;opacity:0;pointer-events:none";
  host.appendChild(area);
  area.focus();
  area.select();
  area.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  active?.focus();
  return ok;
}

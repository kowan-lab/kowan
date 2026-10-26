// Interaction helpers for the UI kit (Radix-based selects, dialogs, toasts).
import { screen, waitFor, within } from "@testing-library/react";
import type userEvent from "@testing-library/user-event";
import { expect } from "vitest";

type User = ReturnType<typeof userEvent.setup>;

/** The open dialog (Popup), optionally the one titled ``title``. */
export async function dialog(title?: RegExp | string): Promise<HTMLElement> {
  if (!title) return screen.findByRole("dialog", {}, { timeout: 5000 });
  return screen.findByRole("dialog", { name: title }, { timeout: 5000 });
}

/** Pick ``option`` in the kit Select labelled ``label`` (inside ``root``). */
export async function choose(user: User, label: RegExp | string, option: RegExp | string, root: HTMLElement = document.body) {
  const trigger = within(root).getByRole("combobox", { name: label });
  await user.click(trigger);
  const opt = await screen.findByRole("option", { name: option }, { timeout: 10_000 });
  await user.click(opt);
}

/** Wait for a toast with ``text`` (sonner). */
export async function toast(text: RegExp | string): Promise<void> {
  await waitFor(() => expect(screen.getAllByText(text).length).toBeGreaterThan(0), { timeout: 5000 });
}

/** Click the button named ``name`` (within ``root``). */
export async function press(user: User, name: RegExp | string, root: HTMLElement = document.body) {
  await user.click(within(root).getByRole("button", { name }));
}

// Query clients created by the test renderers. Before a test unmounts, the
// setup waits for their in-flight requests and mutations: a mutation's
// callbacks run even after unmount, and a late toast would otherwise land in
// the next test's Toaster.
import type { QueryClient } from "@tanstack/react-query";

export const clients: QueryClient[] = [];

export async function settleClients(timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (clients.some((c) => c.isMutating() > 0 || c.isFetching() > 0) && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 10));
  }
  for (const c of clients.splice(0)) c.clear();
}

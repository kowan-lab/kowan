/**
 * Polling cadences for react-query `refetchInterval`.
 *
 *  - LIVE: Docker runtime state (containers, images, volumes, networks, hosts,
 *    disk usage, system info, alerts). When the Docker events bridge is
 *    connected (see components/shell/useDockerEvents.ts) lists are refreshed
 *    by push, so polling drops to a 30 s safety net; without events it falls
 *    back to 5 s.
 *  - SLOW: configuration that changes rarely (registries, secrets, users…).
 *
 * LIVE_REFETCH_MS is a function so react-query re-reads it after every fetch.
 */
let eventsLive = false;

/** Called by the events bridge when every online host's stream is up (or not). */
export function setEventsLive(v: boolean): void {
  eventsLive = v;
}

export const LIVE_REFETCH_MS = (): number => (eventsLive ? 30_000 : 5_000);
export const SLOW_REFETCH_MS = 15_000;

/** HTTP status of a failed request, when the error carries one. */
export function errorStatus(err: unknown): number | undefined {
  return (err as { response?: { status?: number } } | null)?.response?.status;
}

/**
 * refetchInterval for a single object (container, image, volume…): keeps it
 * live, but stops polling once the server says it no longer exists (404) or is
 * not ours to see (403).
 */
export const liveUnlessGone = (q: { state: { error: unknown } }): number | false => {
  const s = errorStatus(q.state.error);
  return s === 404 || s === 403 ? false : LIVE_REFETCH_MS();
};

import "./styles/globals.css";

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Toaster } from "./components/ui/sonner";
import { TooltipProvider } from "./ui/kit";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";
import { HostProvider } from "./HostContext";
import { ThemeProvider } from "./ui/theme";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Retry once on transient failures; never on "not found" or auth errors.
      retry: (count, err) => {
        const s = (err as { response?: { status?: number } })?.response?.status;
        return !(s === 401 || s === 403 || s === 404) && count < 1;
      },
      // Reactive by default: refetch when the tab regains focus or the network
      // reconnects. A short staleTime keeps navigation snappy (served from cache)
      // while still revalidating quickly. Per-list polling is layered on top via
      // `refetchInterval` (see src/lib/live.ts).
      refetchOnWindowFocus: true,
      refetchOnReconnect: true,
      staleTime: 2000,
    },
  },
});

// After a server update the hashed chunk names change: a lazily loaded page from
// the previous build 404s. Reload once to pick up the new build.
window.addEventListener("vite:preloadError", (e) => {
  const KEY = "kowan-chunk-reload";
  if (sessionStorage.getItem(KEY)) return;
  sessionStorage.setItem(KEY, "1");
  e.preventDefault();
  window.location.reload();
});
window.addEventListener("load", () => window.setTimeout(() => sessionStorage.removeItem("kowan-chunk-reload"), 10_000));

// Installable app (PWA): the worker keeps the app shell, never API data
// (public/sw.js). Only in builds: the dev server serves modules, not bundles.
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => undefined);
  });
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <ThemeProvider>
      <TooltipProvider>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <AuthProvider>
              <HostProvider>
                <App />
              </HostProvider>
            </AuthProvider>
          </BrowserRouter>
        </QueryClientProvider>
      </TooltipProvider>
      <Toaster position="bottom-right" />
    </ThemeProvider>
  </StrictMode>,
);

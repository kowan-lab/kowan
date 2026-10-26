import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

const STORAGE_KEY = "kowan_host";
const DEFAULT_HOST = "local";

// ALL_HOSTS is the sentinel hostId for the aggregated "All hosts" view. List
// pages detect it and fan out their query to every online host, tagging each row
// with the host it came from.
export const ALL_HOSTS = "all";

// isAllHosts reports whether the current selection is the aggregated view.
export function isAllHosts(hostId: string): boolean {
  return hostId === ALL_HOSTS;
}

interface HostContextValue {
  hostId: string;
  setHostId: (id: string) => void;
}

const HostContext = createContext<HostContextValue | undefined>(undefined);

export function HostProvider({ children }: { children: ReactNode }) {
  const [hostId, setHostIdState] = useState<string>(
    () => localStorage.getItem(STORAGE_KEY) ?? DEFAULT_HOST,
  );

  const setHostId = (id: string) => {
    localStorage.setItem(STORAGE_KEY, id);
    setHostIdState(id);
  };

  const value = useMemo<HostContextValue>(() => ({ hostId, setHostId }), [hostId]);

  return <HostContext.Provider value={value}>{children}</HostContext.Provider>;
}

export function useHost(): HostContextValue {
  const ctx = useContext(HostContext);
  if (!ctx) {
    throw new Error("useHost must be used within a HostProvider");
  }
  return ctx;
}

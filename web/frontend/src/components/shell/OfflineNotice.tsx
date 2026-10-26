// A strip under the top bar while the browser has no network: the installed
// app still opens from its cache, and this says why nothing loads.

import { useEffect, useState } from "react";
import { WifiOff } from "lucide-react";

function useOnline(): boolean {
  const [online, setOnline] = useState(() => (typeof navigator === "undefined" ? true : navigator.onLine));
  useEffect(() => {
    const on = () => setOnline(true);
    const off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);
  return online;
}

export default function OfflineNotice() {
  const online = useOnline();
  if (online) return null;
  return (
    <div role="status" className="flex items-center gap-[8px] bg-[var(--tone-amber-soft)] px-[16px] py-[8px] text-[13px] leading-[18px] text-[var(--tone-amber-text)] shadow-[inset_0_-1px_0_var(--border)]">
      <WifiOff className="size-[14px] shrink-0" aria-hidden />
      You are offline. Kowan shows live data only, so pages fill in again when the connection is back.
    </div>
  );
}

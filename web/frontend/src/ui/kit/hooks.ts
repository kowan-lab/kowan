import { useCallback, useState } from "react";

/** Boolean open/close state with stable handlers: { opened, open, close, toggle }. */
export function useDisclosure(initial = false) {
  const [opened, setOpened] = useState(initial);
  const open = useCallback(() => setOpened(true), []);
  const close = useCallback(() => setOpened(false), []);
  const toggle = useCallback(() => setOpened((o) => !o), []);
  return { opened, open, close, toggle, setOpened };
}

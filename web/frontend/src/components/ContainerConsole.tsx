import { useEffect, useRef } from "react";
import { Terminal } from "@xterm/xterm";
import { FitAddon } from "@xterm/addon-fit";
import "@xterm/xterm/css/xterm.css";
import { Box, Text } from "@/ui/kit";
import { execWebSocketURL } from "../api/client";
import type { Container } from "../types";

/**
 * Interactive shell into a running container (xterm over the exec WebSocket).
 * Lives in its own module so xterm (~290 KB) only loads when the Console tab
 * is opened, not with every container page.
 */
interface TabProps {
  hostId: string;
  container: Container;
  active: boolean;
}

/** Terminal colours from the active theme tokens. */
function terminalTheme() {
  const css = getComputedStyle(document.documentElement);
  const v = (n: string) => css.getPropertyValue(n).trim();
  return {
    background: v("--surface-sunken"),
    foreground: v("--foreground"),
    cursor: v("--signal"),
    selectionBackground: v("--tone-primary-soft") || undefined,
  };
}

export default function ContainerConsole({
  hostId,
  container,
  active,
  running,
}: TabProps & { running: boolean }) {
  const mountRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!running) return;
    const mount = mountRef.current;
    if (!mount) return;

    const term = new Terminal({
      cursorBlink: true,
      convertEol: true,
      fontFamily: "'Geist Mono Variable', ui-monospace, monospace",
      fontSize: 13,
      theme: terminalTheme(),
    });
    const fit = new FitAddon();
    term.loadAddon(fit);
    term.open(mount);

    let disposed = false;
    let rafId = 0;

    // safeFit guards every fit.fit() call — see the long note in the original
    // (FitAddon.proposeDimensions dereferences an internal render service that
    // may not exist yet or has been torn down).
    const safeFit = () => {
      if (disposed) return;
      if (!mount.isConnected || mount.clientWidth === 0 || mount.clientHeight === 0) {
        return;
      }
      const core = (term as unknown as { _core?: { _renderService?: { dimensions?: unknown } } })._core;
      if (!core || !core._renderService || !core._renderService.dimensions) {
        return;
      }
      try {
        fit.fit();
      } catch {
        /* renderer not measurable yet; ignore */
      }
    };

    rafId = requestAnimationFrame(safeFit);

    const encoder = new TextEncoder();
    let socket: WebSocket | null = null;
    let closed = false;

    try {
      socket = new WebSocket(execWebSocketURL(hostId, container.id, ["/bin/sh"], true));
      socket.binaryType = "arraybuffer";
    } catch {
      term.writeln("\r\n\x1b[31mUnable to open exec session.\x1b[0m");
    }

    if (socket) {
      socket.onopen = () => term.focus();
      socket.onmessage = (ev) => {
        if (typeof ev.data === "string") {
          term.write(ev.data);
        } else if (ev.data instanceof ArrayBuffer) {
          term.write(new Uint8Array(ev.data));
        }
      };
      socket.onclose = () => {
        if (!closed) term.writeln("\r\n\x1b[33m[session closed]\x1b[0m");
      };
      socket.onerror = () => {
        term.writeln("\r\n\x1b[31m[connection error]\x1b[0m");
      };
    }

    const dataSub = term.onData((data) => {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(encoder.encode(data));
      }
    });

    const onResize = () => {
      safeFit();
    };
    window.addEventListener("resize", onResize);

    return () => {
      closed = true;
      disposed = true;
      if (rafId) cancelAnimationFrame(rafId);
      window.removeEventListener("resize", onResize);
      dataSub.dispose();
      if (socket) {
        socket.onopen = null;
        socket.onmessage = null;
        socket.onclose = null;
        socket.onerror = null;
        if (
          socket.readyState === WebSocket.OPEN ||
          socket.readyState === WebSocket.CONNECTING
        ) {
          socket.close();
        }
      }
      term.dispose();
    };
  }, [running, container.id, hostId]);

  // Coming back to the tab: refit to the now-visible size.
  useEffect(() => {
    if (active) window.dispatchEvent(new Event("resize"));
  }, [active]);

  if (!running) {
    return (
      <Text color="muted" size="sm">
        The container is not running. Start it to open an interactive console.
      </Text>
    );
  }

  return (
    <Box direction="row" wrap gap={2} style={{ flex: 1, minHeight: 0, alignItems: "stretch", marginBottom: 8 }}>
      <Box
        ref={mountRef}
        style={{
          flex: 1,
          minHeight: 0,
          width: "100%",
          background: "var(--surface-sunken)",
          padding: 8,
          border: "1px solid var(--border)",
        }}
      />
    </Box>
  );
}

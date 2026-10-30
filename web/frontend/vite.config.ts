import path from "node:path";
import { readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { brotliCompressSync, constants, gzipSync } from "node:zlib";
import type { Plugin } from "vite";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

/**
 * Emit .gz and .br next to every text asset after the build. The control plane
 * serves these directly (no per-request compression cost on the server).
 */
function precompress(): Plugin {
  let outDir = "";
  return {
    name: "kowan-precompress",
    apply: "build",
    configResolved(c) {
      outDir = path.resolve(c.root, c.build.outDir);
    },
    closeBundle() {
      const walk = (dir: string): string[] =>
        readdirSync(dir).flatMap((f) => {
          const p = path.join(dir, f);
          return statSync(p).isDirectory() ? walk(p) : [p];
        });
      for (const file of walk(outDir)) {
        if (!/\.(js|css|html|svg|json|txt)$/.test(file)) continue;
        const buf = readFileSync(file);
        if (buf.length < 1024) continue;
        writeFileSync(`${file}.gz`, gzipSync(buf, { level: 9 }));
        writeFileSync(`${file}.br`, brotliCompressSync(buf, { params: { [constants.BROTLI_PARAM_QUALITY]: 11 } }));
      }
    },
  };
}

/**
 * ``virtual:changelog``: the repository's docs/CHANGELOG.md as a string, for
 * the "What's new" page. Read here rather than imported, as it lives outside
 * this root (the Docker build copies it to the same relative place).
 */
function changelog(): Plugin {
  const file = path.resolve(import.meta.dirname, "../../docs/CHANGELOG.md");
  const id = "\0virtual:changelog";
  return {
    name: "kowan-changelog",
    resolveId: (source) => (source === "virtual:changelog" ? id : undefined),
    load(loaded) {
      if (loaded !== id) return undefined;
      this.addWatchFile(file);
      return `export default ${JSON.stringify(readFileSync(file, "utf8"))};`;
    },
  };
}

const API_TARGET = process.env.VITE_API_TARGET || "http://localhost:5081";
// The version this SPA ships with: the server serves the build it came with,
// so it is the running version (the "What's new" page marks it).
const APP_VERSION: string = JSON.parse(readFileSync(path.resolve(import.meta.dirname, "package.json"), "utf8")).version;

// https://vite.dev/config/
export default defineConfig({
  base: "/",
  plugins: [react(), tailwindcss(), precompress(), changelog()],
  define: { __APP_VERSION__: JSON.stringify(APP_VERSION) },
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  build: {
    outDir: "../dist",
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        // Split heavy vendor libraries into stable, separately-cached chunks so
        // the entry stays small and editor/term/charts only load where used.
        manualChunks(id) {
          if (!id.includes("node_modules")) return undefined;
          if (/[\\/]node_modules[\\/](react|react-dom|react-router|scheduler)[\\/]/.test(id)) return "react";
          if (/[\\/]node_modules[\\/](@radix-ui|radix-ui|cmdk|sonner|lucide-react|@tanstack[\\/]react-table)[\\/]/.test(id)) return "ui";
          // CodeMirror and xterm are imported lazily (YamlEditor, the console):
          // left ungrouped, the bundler gives them chunks that load only where
          // they are used. Grouping them by hand pulled shared helpers into
          // them and made the first page download the editor.
          if (/[\\/]node_modules[\\/]@tanstack[\\/](react-query|query-core)[\\/]/.test(id)) return "query";
          return undefined;
        },
      },
    },
  },
  server: {
    // VITE_API_TARGET points the dev server at another control plane
    // (e.g. a throwaway instance on :18201); defaults to the usual :5081.
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true, ws: true },
      "/hooks": { target: API_TARGET, changeOrigin: true },
      "/health": { target: API_TARGET, changeOrigin: true },
      "/mcp": { target: API_TARGET, changeOrigin: true },
    },
  },
});

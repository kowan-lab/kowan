import { defineConfig, mergeConfig } from "vitest/config";
import viteConfig from "./vite.config";

// Unit and component tests (jsdom). End-to-end tests live in e2e/ (Playwright,
// playwright.config.ts) and are excluded here.
export default mergeConfig(
  viteConfig,
  defineConfig({
    test: {
      environment: "jsdom",
      setupFiles: ["./src/test/setup.ts"],
      include: ["src/**/*.test.{ts,tsx}"],
      css: false,
      testTimeout: 20_000,
      coverage: {
        provider: "v8",
        include: ["src/**/*.{ts,tsx}"],
        exclude: ["src/test/**", "src/**/*.test.{ts,tsx}", "src/main.tsx", "src/vite-env.d.ts", "src/types.ts"],
        reporter: ["text-summary", "json-summary", "html"],
        reportsDirectory: "./coverage",
        // `npm run coverage` fails below these; raise them as coverage grows.
        thresholds: { lines: 85, statements: 83, functions: 77, branches: 74 },
      },
    },
  }),
);

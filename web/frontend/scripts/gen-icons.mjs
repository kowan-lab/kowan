// Renders the PWA icons in public/ from public/favicon.svg with the Chromium
// that Playwright already installs for the e2e tests:
//   icon-192.png, icon-512.png       the favicon as is ("any")
//   icon-maskable-512.png            glyph inside the 80% safe zone, no frame
//   apple-touch-icon.png (180)       iOS home screen
// Run: node scripts/gen-icons.mjs
import { readFileSync } from "node:fs";
import { chromium } from "@playwright/test";

const svg = readFileSync("public/favicon.svg", "utf8").replace(/ width="\d+" height="\d+"/, "");
// The maskable icon: orange ground, the glyph scaled into the safe zone and
// without the inner frame (a circular mask would cut it).
const glyph = svg.match(/<path[^>]*\/>/)[0];
const maskable = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><rect width="20" height="20" fill="#fa3600"/>` +
  `<g transform="translate(10 10) scale(0.72) translate(-10 -10)">${glyph}</g></svg>`;

const browser = await chromium.launch();
const page = await browser.newPage({ deviceScaleFactor: 1 });
async function render(source, size, out) {
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(`<style>html,body{margin:0}svg{display:block;width:${size}px;height:${size}px}</style>${source}`);
  await page.screenshot({ path: out, omitBackground: true });
  console.log("wrote", out);
}
await render(svg, 192, "public/icon-192.png");
await render(svg, 512, "public/icon-512.png");
await render(maskable, 512, "public/icon-maskable-512.png");
await render(svg, 180, "public/apple-touch-icon.png");
await browser.close();

#!/usr/bin/env node
// Portable visual QA. Resolve Playwright from CWD; use no HTTP server.
import { createRequire } from "node:module";
import { resolve, join } from "node:path";
import { mkdir, writeFile, readFile, stat } from "node:fs/promises";

const [input, output, ...options] = process.argv.slice(2);
if (!input || !output) {
  console.error("Usage: node render_artifact.mjs FILE OUTPUT_DIR [--bento] [--channel chrome]");
  process.exit(2);
}
const require = createRequire(resolve(process.cwd(), "moda-render.cjs"));
let browser;
try {
  const { chromium } = require("playwright");
  const channelIndex = options.indexOf("--channel");
  const channel = channelIndex >= 0 ? options[channelIndex + 1] : undefined;
  if (channelIndex >= 0 && !channel) throw new Error("--channel requires a value");
  browser = await chromium.launch(channel ? { channel } : {});
  const page = await browser.newPage({ viewport: { width: 1728, height: 1120 } });
  await mkdir(output, { recursive: true });
  // Only local files produced by the skills are rendered, and they are self-contained (inline CSS and SVG, data:
  // URIs). The document is served from memory at a fixed address on a reserved, unresolvable origin; every other
  // request is aborted, so the browser never reads file:// paths or reaches the network.
  const source = resolve(input);
  if (!(await stat(source)).isFile()) throw new Error("render input must be an existing local file");
  let markup;
  if (input.toLowerCase().endsWith(".svg")) {
    // Standalone XML/SVG documents can stall full-page screenshots in Chromium.
    // An HTML image wrapper preserves the SVG and provides a normal page viewport.
    const bytes = await readFile(source);
    markup = `<!doctype html><html><meta charset="utf-8"><body style="margin:0"><img alt="SVG preview" style="display:block;width:100%" src="data:image/svg+xml;base64,${bytes.toString("base64")}"></body></html>`;
  } else {
    markup = await readFile(source, "utf8");
  }
  await page.route(/^https?:\/\//, route => route.request().url() === "https://moda-render.invalid/document"
    ? route.fulfill({ status: 200, contentType: "text/html; charset=utf-8", body: markup })
    : route.abort());
  await page.goto("https://moda-render.invalid/document", { waitUntil: "load" });
  await page.evaluate(() => document.fonts.ready);
  if (input.toLowerCase().endsWith(".svg")) {
    await page.locator("img").evaluate(image => image.decode());
  }
  const screenshots = [];
  if (options.includes("--bento")) {
    await page.waitForSelector(".ed-stage");
    const thumbs = page.locator(".ed-sidebar .ed-thumb");
    if (!await thumbs.count()) throw new Error("No Bento slides found");
    for (let index = 0; index < await thumbs.count(); index++) {
      await thumbs.nth(index).click();
      await page.waitForTimeout(450);
      const file = join(output, `slide-${String(index + 1).padStart(2, "0")}.png`);
      await page.locator(".ed-stage").screenshot({ path: file });
      screenshots.push(file);
    }
  } else {
    const file = join(output, "page.png");
    await page.screenshot({ path: file, fullPage: true });
    screenshots.push(file);
  }
  await writeFile(join(output, "render-check.json"), JSON.stringify({
    status: "rendered", input: resolve(input), screenshots,
    visual_inspection: "required",
  }, null, 2) + "\n");
  console.log(JSON.stringify({ status: "rendered", screenshots }));
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
}

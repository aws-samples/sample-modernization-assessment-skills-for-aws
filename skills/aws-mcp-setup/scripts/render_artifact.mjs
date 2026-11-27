#!/usr/bin/env node
// Portable visual QA. Resolve Playwright from CWD; use no HTTP server.
import { createRequire } from "node:module";
import { resolve, join } from "node:path";
import { mkdir, writeFile, readFile, stat } from "node:fs/promises";

const [input, output, ...options] = process.argv.slice(2);
if (!input || !output) {
  console.error("Usage: node render_artifact.mjs FILE OUTPUT_DIR [--check] [--bento] [--channel chrome]");
  process.exit(2);
}
// --check: layout QA from the DOM instead of a screenshot to look at. At a desktop and a narrow width it records
// horizontal overflow, elements wider than their container, clipped text, images and SVGs that did not render,
// same-page links without a target, and (for a page whose lang is not en) text nodes that are still English
// sentences. Writes layout-check.json; a screenshot is taken only when a check fails, for the person to look at.
const CHECK = options.includes("--check");

async function layoutCheck(page, lang) {
  return await page.evaluate((lang) => {
    const issues = [];
    const doc = document.documentElement;
    const vw = doc.clientWidth;
    if (doc.scrollWidth > vw + 1) issues.push({ check: "page_overflow_x", detail: `content ${doc.scrollWidth}px wide in a ${vw}px viewport` });
    const describe = (el) => (el.id ? `#${el.id}` : "") + `<${el.tagName.toLowerCase()}${el.className && typeof el.className === "string" ? " ." + el.className.split(" ")[0] : ""}>`
      + " " + (el.textContent || "").trim().slice(0, 60);
    for (const el of document.querySelectorAll("table, figure, pre, .card, .rec, .issue, .specialist, .grade-tile, .kpi, svg")) {
      const parent = el.parentElement;
      if (!parent || el.closest("details:not([open])")) continue;
      const scrollable = getComputedStyle(parent).overflowX;
      const r = el.getBoundingClientRect(), pr = parent.getBoundingClientRect();
      if (r.width > 0 && r.right > pr.right + 2 && !["auto", "scroll"].includes(scrollable) && el.tagName.toLowerCase() !== "svg")
        issues.push({ check: "wider_than_container", detail: describe(el), overflow_px: Math.round(r.right - pr.right) });
    }
    for (const el of document.querySelectorAll("h1, h2, h3, h4, th, td, .chip, summary, a, p, li")) {
      if (el.closest("details:not([open])")) continue;
      const cs = getComputedStyle(el);
      if (["hidden", "clip"].includes(cs.overflowX) && el.scrollWidth > el.clientWidth + 2)
        issues.push({ check: "text_clipped", detail: describe(el) });
    }
    for (const img of document.querySelectorAll("img")) {
      if (!img.complete || img.naturalWidth === 0) issues.push({ check: "image_not_rendered", detail: (img.alt || img.src || "").slice(0, 80) });
    }
    for (const svg of document.querySelectorAll("figure svg, .zoom svg, svg[role=img]")) {
      if (svg.closest("details:not([open])")) continue;
      const box = svg.getBoundingClientRect();
      if (box.width < 20 || box.height < 20) issues.push({ check: "svg_not_rendered", detail: describe(svg) });
    }
    const ids = new Set([...document.querySelectorAll("[id]")].map(e => e.id));
    const dangling = [...new Set([...document.querySelectorAll('a[href^="#"]')].map(a => a.getAttribute("href").slice(1)).filter(h => h && !ids.has(decodeURIComponent(h))))];
    if (dangling.length) issues.push({ check: "dangling_links", detail: dangling.slice(0, 20).join(", "), count: dangling.length });
    let english = [];
    if (lang && !lang.toLowerCase().startsWith("en")) {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      const sentence = /\b(the|and|of|to|is|are|with|for|this|that|from|not|by)\b.*\b(the|and|of|to|is|are|with|for|this|that|from|not|by)\b/i;
      while (walker.nextNode()) {
        const node = walker.currentNode, text = node.textContent.trim(), parent = node.parentElement;
        if (!parent || parent.closest("code, pre, script, style, svg title, .ref, footer") || text.length < 30) continue;
        if (/^[\x00-\x7F]+$/.test(text) && sentence.test(text) && text.split(/\s+/).length >= 6) english.push(text.slice(0, 90));
      }
      english = [...new Set(english)];
      if (english.length) issues.push({ check: "untranslated_text", detail: english.slice(0, 10).join(" | "), count: english.length });
    }
    const fontsOk = document.fonts ? [...document.fonts].every(f => f.status !== "error") : true;
    if (!fontsOk) issues.push({ check: "font_failed", detail: "a declared web font failed to load" });
    return { viewport: vw, height: doc.scrollHeight, issues };
  }, lang);
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
  if (CHECK && !options.includes("--bento")) {
    // A paged report shows one section at a time; measure every page, as print does.
    await page.addStyleTag({ content: "section.page{display:block !important}" });
    const lang = await page.evaluate(() => document.documentElement.lang || "");
    const widths = [1280, 390];
    const results = [];
    for (const width of widths) {
      await page.setViewportSize({ width, height: 1000 });
      await page.waitForTimeout(100);
      const result = await layoutCheck(page, lang);
      // Narrow screens: tables may scroll inside their wrapper and long words may wrap; only page overflow and
      // rendering failures count there.
      if (width < 600) result.issues = result.issues.filter(i => ["page_overflow_x", "image_not_rendered", "svg_not_rendered"].includes(i.check));
      results.push({ width, ...result });
    }
    const failed = results.some(r => r.issues.length);
    if (failed) {
      await page.setViewportSize({ width: 1280, height: 1000 });
      const file = join(output, "page.png");
      await page.screenshot({ path: file, fullPage: true });
      screenshots.push(file);
    }
    const record = { status: failed ? "issues" : "pass", input: resolve(input), lang, widths: results, screenshots,
      visual_inspection: failed ? "look at page.png for the issues listed" : "not needed: the layout checks passed" };
    await writeFile(join(output, "layout-check.json"), JSON.stringify(record, null, 2) + "\n");
    console.log(JSON.stringify({ status: record.status, issues: results.flatMap(r => r.issues.map(i => `${r.width}px ${i.check}: ${i.detail}`)).slice(0, 12) }));
    process.exitCode = failed ? 3 : 0;
  } else if (options.includes("--bento")) {
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
  if (!CHECK) {
    await writeFile(join(output, "render-check.json"), JSON.stringify({
      status: "rendered", input: resolve(input), screenshots,
      visual_inspection: "required",
    }, null, 2) + "\n");
    console.log(JSON.stringify({ status: "rendered", screenshots }));
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  if (browser) await browser.close();
}

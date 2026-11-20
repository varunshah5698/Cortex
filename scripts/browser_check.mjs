/**
 * Open a served run in headless Chrome and report what a person would see.
 *
 * The HTTP probe in modules/probe.py can see a 404 asset; it cannot see a button that
 * does nothing, a React error that blanks the page, or a console exception. This script
 * drives the same Chrome the recorder uses over CDP, with no npm dependency:
 *
 *   node scripts/browser_check.mjs http://127.0.0.1:PORT/ [--interact] [--timeout=20000]
 *
 * It prints one JSON object and exits 0 even when the page is broken: "broken" is a
 * result, not a harness failure. A missing Chrome or a launch failure exits 2 and says so.
 */
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { setTimeout as sleep } from "node:timers/promises";

const CHROME =
  process.env.HARNESS_CHROME || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const args = process.argv.slice(2);
const url = args.find((item) => !item.startsWith("--")) || "";
const timeoutArg = args.find((item) => item.startsWith("--timeout="));
const TIMEOUT = Math.max(4000, Number(timeoutArg?.split("=")[1] || 20000));
const INTERACT = args.includes("--interact");

function emit(result) {
  process.stdout.write(JSON.stringify(result) + "\n");
}

if (!url) {
  emit({ ok: false, skipped: true, error: "usage: browser_check.mjs <url> [--interact] [--timeout=ms]" });
  process.exit(2);
}
if (!existsSync(CHROME)) {
  emit({ ok: false, skipped: true, error: `Chrome not found at ${CHROME}` });
  process.exit(2);
}

const PROFILE = mkdtempSync(path.join(tmpdir(), "cortex-browser-"));
let chrome = null;
let ws = null;
let pendingId = 0;
const pending = new Map();
const pageErrors = [];
const consoleErrors = [];
const failedRequests = [];
const httpErrors = [];
const logErrors = [];
let loadResolve = null;
let loadEvent = new Promise((resolve) => (loadResolve = resolve));
let loaded = false;

function cleanup() {
  try {
    ws?.close();
  } catch {
    /* already closed */
  }
  try {
    chrome?.kill("SIGKILL");
  } catch {
    /* already gone */
  }
  try {
    rmSync(PROFILE, { recursive: true, force: true });
  } catch {
    /* best effort */
  }
}

process.on("exit", cleanup);
process.on("SIGINT", () => {
  cleanup();
  process.exit(130);
});

function send(method, params = {}) {
  return new Promise((resolve) => {
    const id = ++pendingId;
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const out = await send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (out.result?.exceptionDetails) {
    throw new Error(out.result.exceptionDetails.text || "evaluation failed");
  }
  return out.result?.result?.value;
}

function visibleExpression(selector) {
  return `(${selector}).filter((el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== "none" && s.visibility !== "hidden" && !el.disabled;
  })`;
}

async function waitForChrome() {
  const portFile = path.join(PROFILE, "DevToolsActivePort");
  for (let i = 0; i < 80; i++) {
    try {
      const lines = readFileSync(portFile, "utf8").trim().split("\n");
      if (lines[0]) return Number(lines[0]);
    } catch {
      /* not written yet */
    }
    await sleep(100);
  }
  throw new Error("Chrome did not expose a debugging port");
}

async function main() {
  chrome = spawn(
    CHROME,
    [
      "--headless=new",
      "--disable-gpu",
      "--hide-scrollbars",
      "--mute-audio",
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-sync",
      "--disable-extensions",
      "--metrics-recording-only",
      `--user-data-dir=${PROFILE}`,
      "--remote-debugging-port=0",
      "about:blank",
    ],
    { stdio: ["ignore", "ignore", "pipe"] },
  );
  let stderr = "";
  chrome.stderr?.on("data", (chunk) => {
    stderr += chunk.toString().slice(0, 2000);
  });

  const port = await waitForChrome();
  // Open a blank target, enable the domains, then navigate. Opening the target with the
  // URL races the load event: the page can finish before `Page.enable` is called, and
  // the check would sit on a load that already happened.
  const tab = await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent("about:blank")}`, {
    method: "PUT",
  }).then((response) => response.json());
  if (!tab.webSocketDebuggerUrl) throw new Error("Chrome did not return a page target");

  ws = new WebSocket(tab.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener("open", resolve, { once: true });
    ws.addEventListener("error", () => reject(new Error("could not connect to Chrome")), { once: true });
  });
  ws.addEventListener("message", (event) => {
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.id && pending.has(message.id)) {
      pending.get(message.id)(message);
      pending.delete(message.id);
      return;
    }
    const params = message.params || {};
    switch (message.method) {
      case "Page.loadEventFired":
        if (!loaded) {
          loaded = true;
          loadResolve?.(true);
        }
        break;
      case "Page.javascriptDialogOpening":
        // A confirm()/alert() would otherwise park the page and the check. Accept it,
        // because the point is to see the state after the interaction, not to block.
        send("Page.handleJavaScriptDialog", { accept: true }).catch(() => {});
        break;
      case "Runtime.exceptionThrown": {
        const detail = params.exceptionDetails || {};
        pageErrors.push((detail.exception?.description || detail.text || "uncaught exception").slice(0, 600));
        break;
      }
      case "Runtime.consoleAPICalled":
        if (params.type === "error" || params.type === "assert") {
          const text = (params.args || [])
            .map((arg) => arg.value ?? arg.description ?? "")
            .join(" ")
            .slice(0, 500);
          if (text) consoleErrors.push(text);
        }
        break;
      case "Network.loadingFailed":
        if (params.errorText && params.errorText !== "net::ERR_ABORTED") {
          failedRequests.push({
            url: params.requestId,
            error: params.errorText,
            type: params.type || "",
          });
        }
        break;
      case "Network.responseReceived": {
        const response = params.response || {};
        if (response.status >= 400 && !/favicon\.ico$/i.test(response.url || "")) {
          httpErrors.push({ url: (response.url || "").slice(0, 400), status: response.status });
        }
        break;
      }
      case "Log.entryAdded": {
        const entry = params.entry || {};
        // Network failures are already reported by Network.responseReceived /
        // loadingFailed, with the URL. Counting the log echo would double every 404
        // (including Chrome's automatic favicon request) and fail a healthy page.
        if (entry.level === "error" && entry.source !== "network" && entry.text) {
          logErrors.push(entry.text.slice(0, 500));
        }
        break;
      }
      default:
        break;
    }
  });

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Network.enable");
  await send("Log.enable");
  await send("Emulation.setDeviceMetricsOverride", {
    width: 1280,
    height: 800,
    deviceScaleFactor: 1,
    mobile: false,
  });

  // A fresh load event we are guaranteed to be listening for.
  loaded = false;
  loadEvent = new Promise((resolve) => (loadResolve = resolve));
  await send("Page.navigate", { url });
  const timer = sleep(TIMEOUT).then(() => false);
  const finished = await Promise.race([loadEvent, timer]);
  await sleep(finished ? 900 : 200);

  const facts = await evaluate(`(() => {
    const text = (document.body?.innerText || "").trim();
    const interactive = ${visibleExpression(
      '[...document.querySelectorAll("button, a[href], input, select, textarea, [role=button]")]',
    )};
    return {
      title: document.title,
      ready: document.readyState,
      textLength: text.length,
      text: text.slice(0, 1200),
      interactive: interactive.length,
      buttons: interactive.filter((el) => el.tagName === "BUTTON" || el.getAttribute("role") === "button").length,
      ids: [...document.querySelectorAll("[id]")].slice(0, 40).map((el) => el.id),
      canvas: document.querySelectorAll("canvas").length,
      scripts: document.querySelectorAll("script").length,
      stylesheets: document.styleSheets.length,
    };
  })()`);

  const interactions = [];
  if (INTERACT) {
    const targets = await evaluate(`(() => {
      return ${visibleExpression(
        '[...document.querySelectorAll("button, [role=button]")]',
      )}.slice(0, 2).map((el, index) => ({ index, label: (el.innerText || el.getAttribute("aria-label") || "button").trim().slice(0, 60) }));
    })()`);
    for (const target of targets) {
      const before = await evaluate("(document.body?.innerText || '').trim()");
      const errorsBefore = consoleErrors.length + pageErrors.length;
      const clicked = await evaluate(`(() => {
        const el = ${visibleExpression(
          '[...document.querySelectorAll("button, [role=button]")]',
        )}[${target.index}];
        if (!el) return false;
        el.click();
        return true;
      })()`).catch(() => false);
      await sleep(450);
      const after = await evaluate("(document.body?.innerText || '').trim()");
      interactions.push({
        label: target.label,
        clicked,
        changed: before !== after,
        newErrors: consoleErrors.length + pageErrors.length - errorsBefore,
      });
    }
  }

  const issues = [];
  for (const error of pageErrors.slice(0, 4)) {
    issues.push({ level: "error", code: "page_error", detail: error });
  }
  for (const error of consoleErrors.slice(0, 4)) {
    issues.push({ level: "error", code: "console_error", detail: error });
  }
  for (const item of failedRequests.slice(0, 4)) {
    issues.push({ level: "error", code: "request_failed", detail: `${item.type}: ${item.error}` });
  }
  for (const item of httpErrors.slice(0, 4)) {
    issues.push({ level: "error", code: "http_error", detail: `${item.status} ${item.url}` });
  }
  for (const error of logErrors.slice(0, 4)) {
    issues.push({ level: "error", code: "log_error", detail: error });
  }
  if (facts.textLength < 3 && facts.ready === "complete") {
    issues.push({ level: "error", code: "empty_page", detail: "the page loaded but its body has no readable text" });
  }
  const brokenInteraction = interactions.find((item) => item.clicked && item.newErrors > 0);
  if (brokenInteraction) {
    issues.push({
      level: "error",
      code: "interaction_error",
      detail: `clicking ${brokenInteraction.label} raised an error`,
    });
  }

  emit({
    ok: issues.length === 0,
    skipped: false,
    url,
    title: facts.title || "",
    ready: facts.ready,
    textLength: facts.textLength,
    text: facts.text,
    interactive: facts.interactive,
    buttons: facts.buttons,
    canvas: facts.canvas,
    scripts: facts.scripts,
    stylesheets: facts.stylesheets,
    interactions,
    consoleErrors: consoleErrors.slice(0, 8),
    pageErrors: pageErrors.slice(0, 8),
    failedRequests: failedRequests.slice(0, 8),
    httpErrors: httpErrors.slice(0, 8),
    issues: issues.slice(0, 10),
  });
  void stderr;
}

main()
  .catch((error) => {
    emit({ ok: false, skipped: false, error: String(error?.stack || error).slice(0, 1200) });
    process.exitCode = 1;
  })
  .finally(async () => {
    const code = process.exitCode || 0;
    cleanup();
    await sleep(50);
    // Chrome's sockets and the CDP websocket can keep the event loop alive forever.
    // One report is the whole contract, so exit instead of waiting for them.
    process.exit(code);
  });

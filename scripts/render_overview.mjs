/**
 * Render public/overview.html to a real video.
 *
 * Drives the copy of Chrome already on the machine over CDP (Node 22 ships a
 * WebSocket client, so this needs no package), captures the page's own frames
 * through Page.startScreencast, then encodes them with ffmpeg.
 *
 *   node scripts/render_overview.mjs [seconds] [out.mp4]
 */
import { spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";
import path from "node:path";

const SECONDS = Number(process.argv[2] || 68);
const OUT = path.resolve(process.argv[3] || "public/overview.mp4");
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9333;
const PROFILE = "/tmp/cortex-overview-chrome";
const FRAMES = "/tmp/cortex-overview-frames";
const URL = process.env.OVERVIEW_URL || "http://127.0.0.1:5178/overview.html";

if (!existsSync(CHROME)) throw new Error(`Chrome not found at ${CHROME}`);

rmSync(FRAMES, { recursive: true, force: true });
rmSync(PROFILE, { recursive: true, force: true });
mkdirSync(FRAMES, { recursive: true });
mkdirSync(path.dirname(OUT), { recursive: true });

const chrome = spawn(
  CHROME,
  [
    "--headless=new",
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE}`,
    "--window-size=1280,720",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=CalculateNativeWinOcclusion",
    "about:blank",
  ],
  { stdio: "ignore" },
);

const cdp = async (target) => {
  const res = await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent(target)}`, {
    method: "PUT",
  });
  return res.json();
};

let tab;
for (let i = 0; i < 60; i++) {
  try {
    tab = await cdp("about:blank");
    break;
  } catch {
    await sleep(250);
  }
}
if (!tab) throw new Error("Chrome did not expose a debugging target");

const ws = new WebSocket(tab.webSocketDebuggerUrl);
await new Promise((res, rej) => {
  ws.addEventListener("open", res, { once: true });
  ws.addEventListener("error", rej, { once: true });
});

let id = 0;
const pending = new Map();
const frames = [];

ws.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg);
    pending.delete(msg.id);
    return;
  }
  if (msg.method === "Page.screencastFrame") {
    frames.push({ data: msg.params.data, ts: msg.params.metadata.timestamp });
    send("Page.screencastFrameAck", { sessionId: msg.params.sessionId });
  }
});

const send = (method, params = {}) =>
  new Promise((resolve) => {
    const mid = ++id;
    pending.set(mid, resolve);
    ws.send(JSON.stringify({ id: mid, method, params }));
  });

const evaluate = async (expression) => {
  const out = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  return out.result?.result?.value;
};

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: 1280, height: 720, deviceScaleFactor: 1, mobile: false,
});
await send("Page.navigate", { url: URL });
await sleep(1500);
await evaluate(`(() => { const r = document.scrollingElement; if (r) { r.scrollTop = 0; }
  document.documentElement.style.background = "#08060c"; return document.visibilityState; })()`);

const state = await evaluate("document.visibilityState");
console.log(`page visibility: ${state}`);

await send("Page.startScreencast", { format: "jpeg", quality: 96, maxWidth: 1280, maxHeight: 720, everyNthFrame: 1 });
const t0 = Date.now();
await sleep(SECONDS * 1000);
await send("Page.stopScreencast");
ws.close();
chrome.kill();

console.log(`captured ${frames.length} frames in ${((Date.now() - t0) / 1000).toFixed(1)}s`);
if (frames.length < 30) throw new Error("too few frames captured");

// A concat list with real frame durations keeps motion faithful to the page's timing.
const first = frames[0].ts;
let list = "";
let last = 0;
frames.forEach((frame, i) => {
  const file = path.join(FRAMES, `f${String(i).padStart(5, "0")}.jpg`);
  writeFileSync(file, Buffer.from(frame.data, "base64"));
  const at = frame.ts - first;
  const duration = Math.max(0.008, at - last);
  last = at;
  list += `file '${file}'\nduration ${duration.toFixed(4)}\n`;
});
writeFileSync(path.join(FRAMES, "list.txt"), list + `file '${path.join(FRAMES, `f${String(frames.length - 1).padStart(5, "0")}.jpg`)}'\n`);

const ffmpeg = (args) =>
  new Promise((res, rej) => {
    const proc = spawn("ffmpeg", args, { stdio: ["ignore", "inherit", "inherit"] });
    proc.on("exit", (code) => (code === 0 ? res() : rej(new Error(`ffmpeg exited ${code}`))));
  });

const concat = path.join(FRAMES, "list.txt");
await ffmpeg([
  "-y", "-f", "concat", "-safe", "0", "-i", concat,
  "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "20",
  "-movflags", "+faststart", OUT,
]);
console.log(`wrote ${path.relative(process.cwd(), OUT)}`);

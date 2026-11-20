/**
 * Record the running Cortex site as a video.
 *
 * This films the actual UI at http://127.0.0.1:5178 — no mock, no redraw: Chrome
 * renders the real app, the build room replays a real run's event log from the
 * harness, and `Page.startScreencast` captures the composited frames. ffmpeg then
 * encodes them so the timing matches what the browser actually painted.
 *
 * Needs the harness (:8000) and the UI (:5178) running:
 *
 *   node scripts/record_site.mjs [run_id] [out.mp4]
 */
import { spawn } from "node:child_process";
import { mkdirSync, rmSync, writeFileSync, existsSync } from "node:fs";
import { setTimeout as sleep } from "node:timers/promises";
import path from "node:path";

const RUN = process.argv[2] || "06847880c95b";
const OUT = path.resolve(process.argv[3] || "public/site-overview.mp4");
const SITE = process.env.SITE_URL || "http://127.0.0.1:5178";
const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9334;
const PROFILE = "/tmp/cortex-site-chrome";
const FRAMES = "/tmp/cortex-site-frames";
const W = 1280;
const H = 720;

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
    `--window-size=${W},${H}`,
    "--force-device-scale-factor=1",
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

// A thrown guard must not leave Chrome holding the debug port for the next take.
process.on("exit", () => {
  try {
    chrome.kill();
  } catch {
    /* already gone */
  }
});

let tab = null;
for (let i = 0; i < 80; i++) {
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/new?${encodeURIComponent("about:blank")}`, {
      method: "PUT",
    });
    tab = await res.json();
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
    // Arrival time, not the frame's own timestamp: a page that sits still paints no
    // frames, and the hold it was meant to have must survive into the encode.
    frames.push({ data: msg.params.data, ts: Date.now() / 1000 });
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
  if (out.result?.exceptionDetails) throw new Error(out.result.exceptionDetails.text);
  return out.result?.result?.value;
};

const go = async (hash) => {
  await evaluate(`(() => { window.location.hash = ${JSON.stringify(hash)}; return true; })()`);
};

/** A cut: cover the viewport, swap the page underneath, take the cover away.
 *  The cover is a real element that is removed, never a fade-out that can be left on
 *  screen: a stuck overlay is minutes of black in the finished film. */
const cover = () =>
  evaluate(`(() => {
    let el = document.getElementById("__film");
    if (!el) {
      el = document.createElement("div");
      el.id = "__film";
      el.style.cssText = "position:fixed;inset:0;background:#07050b;z-index:2147483647;pointer-events:none;opacity:1";
      document.body.appendChild(el);
    }
    return true;
  })()`).catch(() => {});

const uncover = () => evaluate(`(() => {
  const el = document.getElementById("__film");
  if (el) el.remove();
  return true;
})()`).catch(() => {});

/** A cut: navigate, let the page paint, then make sure nothing of ours is on top.
 *  Hard cuts on purpose: a cover element that outlives its page is minutes of black. */
const cut = async (fn, settle = 900) => {
  await fn();
  await sleep(settle);
  await uncover();
};

const scrollTo = (selector, offset = 0, ms = 3200) => {
  const expression = selector
    ? `(() => { const el = document.querySelector(${JSON.stringify(selector)});
        const top = Math.max(0, (el ? el.getBoundingClientRect().top + window.scrollY : 0) + ${offset});
        const from = window.scrollY; const start = performance.now();
        const step = (now) => { const k = Math.min(1, (now - start) / ${ms});
          const ease = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
          window.scrollTo(0, from + (top - from) * ease);
          if (k < 1) requestAnimationFrame(step); };
        requestAnimationFrame(step); return true; })()`
    : `(() => { const from = window.scrollY; const start = performance.now();
        const step = (now) => { const k = Math.min(1, (now - start) / ${ms});
          const ease = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
          window.scrollTo(0, from + (${offset} - from) * ease);
          if (k < 1) requestAnimationFrame(step); };
        requestAnimationFrame(step); return true; })()`;
  return evaluate(expression);
};

/** The right panel's tabs, scoped: "Team" also exists in the topbar and navigates away. */
const clickTab = async (label) => {
  const hit = await evaluate(`(() => {
    const btn = [...document.querySelectorAll(".bld-tabs button")].find(
      (b) => (b.textContent || "").trim().toLowerCase() === ${JSON.stringify(label.toLowerCase())},
    );
    if (btn) { btn.click(); return true; }
    return false;
  })()`);
  if (!hit) console.warn(`tab not found: ${label}`);
};

/** Poll the page until `expression` is true: the film follows the run, not a timer. */
const waitFor = async (expression, timeout = 90000, every = 400) => {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    if (await evaluate(expression).catch(() => false)) return true;
    await sleep(every);
  }
  return false;
};

const clickFirst = async (selector, name) => {
  const hit = await evaluate(`(() => {
    const el = [...document.querySelectorAll(${JSON.stringify(selector)})].find((node) =>
      (node.textContent || "").includes(${JSON.stringify(name)}),
    );
    if (el) { el.click(); return true; }
    return false;
  })()`);
  if (!hit) console.warn(`not found: ${selector} ${name}`);
};

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: W, height: H, deviceScaleFactor: 1, mobile: false,
});

const scenes = [];
let t0 = 0;
const mark = (label) => scenes.push({ label, at: Number(((Date.now() - t0) / 1000).toFixed(2)) });

// First paint, then pin the build room to a real recorded run before it mounts.
await send("Page.navigate", { url: `${SITE}/` });
await sleep(2000);
await evaluate(`(() => { localStorage.setItem("cortex:lastRun", ${JSON.stringify(RUN)});
  document.documentElement.style.background = "#08060c"; return true; })()`);
await sleep(600);

await send("Page.startScreencast", { format: "jpeg", quality: 94, maxWidth: W, maxHeight: H, everyNthFrame: 1 });
t0 = Date.now();
await sleep(400);
mark("landing");
await uncover();

// A still page paints no frames, and a screencast only captures paints: this nearly
// invisible 2px element keeps the compositor ticking so holds keep their real length.
await evaluate(`(() => {
  if (document.getElementById("__tick")) return true;
  const style = document.createElement("style");
  style.textContent = "#__tick{position:fixed;left:0;top:0;width:2px;height:2px;opacity:.02;background:#000;" +
    "animation:__tick .1s steps(2,end) infinite;pointer-events:none;z-index:0}" +
    "@keyframes __tick{from{transform:translateX(0)}to{transform:translateX(1px)}}";
  document.head.appendChild(style);
  const tick = document.createElement("div");
  tick.id = "__tick";
  document.body.appendChild(tick);
  return true;
})()`).catch(() => {});

// --- landing: the hero, then the sections beneath it -------------------------
await sleep(1800);
await scrollTo(null, 360, 4200);
await sleep(900);
mark("workflow");
await scrollTo("#workflow", -56, 3400);
await sleep(2400);
mark("models");
await scrollTo("#models", -56, 3400);
await sleep(2400);

// --- the build environment ---------------------------------------------------
// The room replays the run's own event log: every specialist, bubble, token and
// file here is that run, painted by the real app.
mark("build");
await cut(() => go("/build"), 900);
// The room must actually pick the run up: an unreplayed run is a lobby, not a build.
const started = await waitFor(`(() => {
  const phase = document.querySelector(".bld-phase")?.textContent?.trim();
  return !!phase && phase !== "Lobby";
})()`, 25000);
if (!started) throw new Error("the build room never picked up the run — is the harness up?");
await sleep(7000);
mark("build:reasoning");
await clickTab("Reasoning");
await sleep(9000);
mark("build:team");
await clickTab("Team");
await sleep(6000);
mark("build:runtime");
await clickTab("Runtime");
// The replay is the run's own log, so the room reports "Shipped" when it reaches
// the run's last event. Wait for that instead of cutting away mid-build.
const shipped = await waitFor(`(() => [...document.querySelectorAll(".bld-phase")]
  .some((el) => el.textContent.trim().toLowerCase() === "shipped"))()`, 120000);
if (!shipped) throw new Error("the replay never reached the run's done event");
mark("build:preview");
await clickTab("Preview");
// `done` makes the room boot the runner: install, build, serve. The served page is
// model-written, so an ask-gated command shows Approve & run — click it.
const ready = `(() => !!document.querySelector(".pv-state.is-ready"))()`;
if (!(await waitFor(ready, 12000))) {
  await clickFirst(".pv .pv-btn", "");
  await waitFor(ready, 20000);
}
await sleep(6000);

// --- the artifacts the run left behind ---------------------------------------
mark("files");
await cut(() => go("/files"), 900);
// The explorer loads its rows from /api: click a source file once they exist.
await waitFor(`(() => document.querySelectorAll(".vault-file").length > 1)()`, 12000);
await clickFirst(".vault-file", "script.js");
await sleep(4000);
await evaluate(`(() => { const view = document.querySelector(".vault-view pre, .vault-view code, .vault-view");
  const scroller = [...document.querySelectorAll(".vault-view *")].find((el) => el.scrollHeight > el.clientHeight + 40);
  if (scroller) scroller.scrollTop = 320; return true; })()`).catch(() => {});
await sleep(3000);

// --- back to the top, then out ----------------------------------------------
mark("close");
await cut(() => go("/"), 500);
await sleep(2200);
await cover();
await sleep(700);
await send("Page.stopScreencast");
ws.close();
chrome.kill();

console.log(`captured ${frames.length} frames`);
if (frames.length < 30) throw new Error("too few frames captured");

// Each frame holds until the *next* one arrived — a frame's own arrival gap belongs to
// the frame before it, and getting that backwards shifts every cut in the film.
let list = "";
frames.forEach((frame, i) => {
  const file = path.join(FRAMES, `f${String(i).padStart(5, "0")}.jpg`);
  writeFileSync(file, Buffer.from(frame.data, "base64"));
  const next = frames[i + 1]?.ts ?? frame.ts + 1 / 10;
  const duration = Math.max(0.008, next - frame.ts);
  list += `file '${file}'\nduration ${duration.toFixed(4)}\n`;
});
writeFileSync(path.join(FRAMES, "list.txt"), list);

const ffmpeg = (args) =>
  new Promise((res, rej) => {
    const proc = spawn("ffmpeg", args, { stdio: ["ignore", "inherit", "inherit"] });
    proc.on("exit", (code) => (code === 0 ? res() : rej(new Error(`ffmpeg exited ${code}`))));
  });

await ffmpeg([
  "-y", "-f", "concat", "-safe", "0", "-i", path.join(FRAMES, "list.txt"),
  "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "slow", "-crf", "20",
  "-movflags", "+faststart", OUT,
]);

const total = Number(((Date.now() - t0) / 1000).toFixed(1));
console.log(`wrote ${path.relative(process.cwd(), OUT)} · ${total}s`);
// Scene times are relative to the first captured frame, which is the film's clock.
console.log(JSON.stringify({ offset: (frames[0].ts - t0 / 1000).toFixed(2), scenes }, null, 1));

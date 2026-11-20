/**
 * Put a voice on the site overview film.
 *
 * Each line is spoken offline by macOS `say`, placed at the scene it describes, and mixed
 * onto the recording with the video stream copied (the picture is never re-encoded).
 * A line that runs past its own scene is reported, not silently stepped on.
 *
 *   node scripts/narrate_site.mjs [in.mp4] [out.mp4]
 */
import { execFileSync } from "node:child_process";
import { mkdirSync, rmSync, copyFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const IN = path.resolve(process.argv[2] || "public/site-overview.mp4");
const OUT = path.resolve(process.argv[3] || "public/site-overview.mp4");
const VOICE = process.env.VOICE || "Daniel (English (UK))";
const RATE = Number(process.env.RATE || 168);
const WORK = path.join(tmpdir(), "cortex-voice");

// Scene starts come from scripts/record_site.mjs's own marks, so the voice lands on the cut.
const LINES = [
  { at: 0.9, text: "Cortex, filmed while it ran." },
  { at: 3.3, text: "One prompt becomes a plan." },
  { at: 5.7, text: "Every role is a slot, filled by you." },
  { at: 8.6, text: "The build room is the product. One prompt, seven specialists, one budget." },
  { at: 16.3, text: "Each specialist reasons out loud before it writes a line. This feed is that run's own transcript." },
  { at: 25.4, text: "Planner, researcher, writer, frontend, backend, verifier, reviewer. Seven seats, one team." },
  { at: 33.2, text: "When the run finishes, the runner installs, builds and serves what the team wrote. This is that page, live." },
  { at: 41.6, text: "Three files: markup, styles and script. All of it written by the room, in front of you." },
  { at: 49.5, text: "Cortex. A whole chain of command." },
];

if (!existsSync(IN)) throw new Error(`no film at ${IN}`);
rmSync(WORK, { recursive: true, force: true });
mkdirSync(WORK, { recursive: true });

const duration = (file) =>
  Number(
    execFileSync("ffprobe", [
      "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", file,
    ]).toString().trim(),
  );

const filmLength = duration(IN);
const spoken = [];
let previousEnd = 0;

LINES.forEach((line, i) => {
  const file = path.join(WORK, `line${String(i).padStart(2, "0")}.aiff`);
  execFileSync("say", ["-v", VOICE, "-r", String(RATE), "-o", file, line.text]);
  const length = duration(file);
  const next = LINES[i + 1]?.at ?? filmLength;
  const slot = next - line.at - 0.12;
  const overflow = length - slot;
  spoken.push({ at: line.at, file, length, slot, overflow });
  if (overflow > 0.05) {
    console.log(`  ! line ${i} runs ${overflow.toFixed(2)}s past its scene: "${line.text}"`);
  }
  if (line.at < previousEnd - 0.05) {
    console.log(`  ! line ${i} starts ${(previousEnd - line.at).toFixed(2)}s before the previous one ends`);
  }
  previousEnd = line.at + length;
});

const inputs = spoken.flatMap((line) => ["-i", line.file]);
// Input 0 is the film (no audio of its own); the spoken lines are inputs 1..n.
const chains = spoken.map(
  (line, i) =>
    `[${i + 1}:a]aresample=48000,pan=stereo|c0=c0|c1=c0,` +
    `adelay=${Math.round(line.at * 1000)}|${Math.round(line.at * 1000)}[a${i}]`,
);
const mix = spoken.map((_, i) => `[a${i}]`).join("") + `amix=inputs=${spoken.length}:normalize=0:duration=longest,apad`;

const staged = path.join(WORK, "narrated.mp4");
execFileSync(
  "ffmpeg",
  [
    "-v", "error", "-y",
    "-i", IN,
    ...inputs,
    "-filter_complex", `${chains.join(";")};${mix}[aout]`,
    "-map", "0:v:0", "-map", "[aout]",
    "-c:v", "copy",
    "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2",
    "-shortest", "-movflags", "+faststart",
    staged,
  ],
  { stdio: "inherit" },
);

copyFileSync(staged, OUT);

const total = spoken.reduce((sum, line) => sum + line.length, 0);
console.log(`voice: ${VOICE} at ${RATE} wpm`);
for (const [i, line] of spoken.entries()) {
  console.log(`  ${line.at.toFixed(1).padStart(5)}s  ${line.length.toFixed(2)}s / ${line.slot.toFixed(2)}s slot  ${LINES[i].text.slice(0, 44)}`);
}
console.log(`${total.toFixed(1)}s of speech in ${filmLength.toFixed(1)}s of film`);
console.log(`wrote ${path.relative(process.cwd(), OUT)}`);

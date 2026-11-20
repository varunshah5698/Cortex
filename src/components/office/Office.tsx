import { useEffect, useMemo, useRef, useState } from "react";

export type Zone = "planning" | "research" | "coding" | "review";
export type AgentStatus =
  | "idle"
  | "thinking"
  | "researching"
  | "coding"
  | "writing"
  | "reviewing"
  | "talking"
  | "done"
  | "error";

const isWorking = (status: AgentStatus) => status === "coding" || status === "writing";
const isThinking = (status: AgentStatus) =>
  status === "thinking" || status === "researching" || status === "reviewing" || status === "talking";

export type AgentView = {
  id: string;
  name: string;
  role: string;
  zone: Zone;
  status: AgentStatus;
  bubble?: string;
  thought?: string;
  file?: string;
  tokens: number;
  latency: number;
};

const ROLE_COLOR: Record<string, string> = {
  planner: "#f2b544",
  research: "#5cc8c8",
  reviewer: "#a08bf0",
  frontend: "#ef7d62",
  backend: "#7fb069",
  fullstack: "#e0913f",
  designer: "#e573a6",
  writer: "#8fa9e8",
  tester: "#d7c25c",
  general: "#9aa3b2",
};

export const roleColor = (role: string) => ROLE_COLOR[role] ?? ROLE_COLOR.general;

const HAIR = ["#2b2033", "#6b3f28", "#c8a06a", "#3d2b52", "#8a4a3a", "#1f2c3a"];
const SKIN = ["#f3c8a3", "#e0a97f", "#c98a63", "#f7d9bd", "#a9714f"];

function hash(id: string) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 9973;
  return h;
}

const SPOTS: Record<Zone, { x: number; y: number }[]> = {
  planning: [
    { x: 132, y: 372 },
    { x: 248, y: 372 },
    { x: 320, y: 300 },
  ],
  research: [
    { x: 742, y: 372 },
    { x: 878, y: 372 },
  ],
  review: [
    { x: 700, y: 548 },
    { x: 862, y: 548 },
    { x: 786, y: 462 },
  ],
  coding: [
    { x: 128, y: 604 },
    { x: 296, y: 604 },
    { x: 452, y: 604 },
    { x: 566, y: 560 },
  ],
};

function seat(zone: Zone, index: number) {
  const spots = SPOTS[zone] ?? SPOTS.planning;
  return spots[index % spots.length];
}


function Sprite({ id, color, status }: { id: string; color: string; status: AgentStatus }) {
  const h = hash(id);
  const hair = HAIR[h % HAIR.length];
  const skin = SKIN[h % SKIN.length];
  const busy = isWorking(status);
  const think = isThinking(status);
  return (
    <svg
      className={`px-sprite${busy ? " is-busy" : ""}${think ? " is-think" : ""}`}
      viewBox="0 0 20 28"
      width={44}
      height={62}
      aria-hidden
    >
      <g className="px-bob">
        <rect x="4.5" y="0.6" width="11" height="4.6" fill={hair} />
        <rect x="4" y="4" width="12" height="1.6" fill={hair} />
        <rect x="6" y="5" width="8" height="7" fill={skin} />
        <rect x="7.2" y="7.6" width="1.5" height="1.7" fill="#241a2b" />
        <rect x="11.3" y="7.6" width="1.5" height="1.7" fill="#241a2b" />
        <rect x="9.1" y="10.2" width="1.9" height="0.9" fill="#b4675a" />
        <rect x="5" y="12" width="10" height="8" fill={color} />
        <rect x="8.6" y="12" width="2.8" height="2" fill="#ffffff" opacity=".35" />
        <rect x="9.2" y="16" width="1.6" height="1.6" fill="#ffffff" opacity=".25" />
        <g className="px-arm px-arm-l">
          <rect x="3.2" y="12.6" width="2.2" height="6" fill={color} />
          <rect x="3.2" y="18.2" width="2.2" height="1.8" fill={skin} />
        </g>
        <g className="px-arm px-arm-r">
          <rect x="14.6" y="12.6" width="2.2" height="6" fill={color} />
          <rect x="14.6" y="18.2" width="2.2" height="1.8" fill={skin} />
        </g>
        <rect x="6.4" y="20" width="2.6" height="5" fill="#3a3346" />
        <rect x="11" y="20" width="2.6" height="5" fill="#3a3346" />
        <rect x="5.8" y="25" width="3.4" height="2.2" fill="#241a2b" />
        <rect x="10.6" y="25" width="3.4" height="2.2" fill="#241a2b" />
      </g>
    </svg>
  );
}

const base = (path: string) => path.split("/").pop() ?? path;

/**
 * A tiny pixel editor that streams code lines next to a working agent. It is
 * placed in the room's own 1000x660 scene units (see .px-code in build.css), so
 * it keeps clear of the zone signs (y 292-318, 410-436) and the desk monitors
 * (y 462-496) at every window size, and flips to the worker's left near the
 * right edge of the room.
 */
function CodePanel({ file, seed, side }: { file?: string; seed: number; side: "left" | "right" }) {
  const widths = [62 + (seed % 26), 40 + (seed % 34), 54 + (seed % 28), 30 + (seed % 32)];
  return (
    <span className={`px-code is-${side}`} aria-hidden>
      <span className="px-code-bar">
        <i />
        <i />
        <i />
        <b>{file ? base(file) : "index.tsx"}</b>
      </span>
      {widths.map((width, i) => (
        <span
          key={i}
          className="px-code-line"
          style={{
            ["--w" as string]: `${width}%`,
            animationDelay: `${i * 0.24}s`,
            animationDuration: `${2.3 + i * 0.13}s`,
          }}
        />
      ))}
      <span className="px-code-caret" />
    </span>
  );
}

function Desks() {
  return (
    <g>
      {[0, 1, 2].map((i) => {
        const x = 78 + i * 168;
        return (
          <g key={i}>
            <rect x={x} y={492} width={150} height={10} fill="#b9855a" />
            <rect x={x} y={502} width={150} height={6} fill="#7d5133" />
            <rect x={x + 6} y={508} width={6} height={30} fill="#6b4429" />
            <rect x={x + 138} y={508} width={6} height={30} fill="#6b4429" />
            <rect x={x + 46} y={462} width={56} height={34} fill="#2d2a39" />
            <rect x={x + 50} y={466} width={48} height={26} fill="#38495c" />
            <rect x={x + 54} y={470} width={30} height={2.4} fill="#9ff0cd" opacity=".85" />
            <rect x={x + 54} y={475} width={40} height={2.4} fill="#9ff0cd" opacity=".45" />
            <rect x={x + 54} y={480} width={22} height={2.4} fill="#9ff0cd" opacity=".6" />
            <rect x={x + 71} y={496} width={8} height={4} fill="#3b3648" />
            <rect x={x + 118} y={498} width={22} height={14} fill="#dcb98a" />
            <rect x={x + 121} y={501} width={16} height={1.6} fill="#8a6a45" />
          </g>
        );
      })}
    </g>
  );
}

function RoundTable() {
  return (
    <g>
      <rect x={676} y={452} width={224} height={16} fill="#b0794f" />
      <rect x={676} y={468} width={224} height={8} fill="#83543a" />
      <rect x={770} y={476} width={14} height={26} fill="#6b4429" />
      <rect x={742} y={440} width={26} height={18} fill="#f2e5c9" />
      <rect x={784} y={436} width={20} height={20} fill="#efe0c0" />
      <rect x={812} y={444} width={30} height={14} fill="#f2e5c9" />
      {[700, 760, 828, 880].map((x) => (
        <rect key={x} x={x} y={494} width={16} height={10} fill="#5a3f52" />
      ))}
    </g>
  );
}

function Rack() {
  return (
    <g>
      <rect x={700} y={150} width={132} height={126} rx={3} fill="#2b2736" />
      <rect x={706} y={158} width={120} height={26} fill="#1d1a26" />
      {[0, 1, 2, 3].map((i) => (
        <g key={i}>
          <rect x={706} y={190 + i * 22} width={120} height={16} fill="#211e2c" />
          <rect x={712} y={194 + i * 22} width={4} height={4} fill={i % 2 ? "#8ff0c9" : "#f2b544"} />
          <rect x={720} y={194 + i * 22} width={4} height={4} fill="#8ff0c9" opacity={i % 2 ? 1 : 0.4} />
          <rect x={740} y={195 + i * 22} width={76} height={2} fill="#4b4660" />
        </g>
      ))}
      <rect x={706} y={158} width={120} height={4} fill="#8ff0c9" opacity=".35" />
    </g>
  );
}

function Whiteboard() {
  return (
    <g>
      <rect x={54} y={142} width={246} height={128} rx={3} fill="#e8e4dc" />
      <rect x={54} y={142} width={246} height={128} rx={3} fill="none" stroke="#8d8a94" strokeWidth={4} />
      <rect x={76} y={166} width={64} height={4} fill="#5b6b8c" />
      <rect x={76} y={176} width={110} height={3} fill="#a8b0c4" />
      <rect x={76} y={184} width={92} height={3} fill="#a8b0c4" />
      <rect x={76} y={206} width={40} height={26} fill="none" stroke="#5d8a6f" strokeWidth={3} />
      <rect x={132} y={206} width={40} height={26} fill="none" stroke="#5d8a6f" strokeWidth={3} />
      <rect x={188} y={206} width={40} height={26} fill="none" stroke="#5d8a6f" strokeWidth={3} />
      <rect x={116} y={218} width={16} height={2} fill="#5d8a6f" />
      <rect x={172} y={218} width={16} height={2} fill="#5d8a6f" />
      <rect x={76} y={246} width={140} height={3} fill="#c08a5c" />
      <rect x={238} y={188} width={38} height={54} fill="#f7e7a8" />
      <rect x={242} y={194} width={30} height={2} fill="#b9a45c" />
      <rect x={242} y={200} width={24} height={2} fill="#b9a45c" />
      <rect x={46} y={270} width={262} height={8} fill="#6b4429" />
    </g>
  );
}

function Plant({ x, y }: { x: number; y: number }) {
  return (
    <g>
      <rect x={x} y={y + 26} width={26} height={20} fill="#a2603f" />
      <rect x={x} y={y + 26} width={26} height={5} fill="#bb7551" />
      <rect x={x + 6} y={y + 6} width={7} height={22} fill="#4b7b4a" />
      <rect x={x + 15} y={y} width={7} height={28} fill="#5c8f52" />
      <rect x={x + 2} y={y + 14} width={7} height={14} fill="#3f6b44" />
    </g>
  );
}

function Sign({ x, y, text, tone }: { x: number; y: number; text: string; tone: string }) {
  return (
    <g>
      <rect x={x} y={y} width={text.length * 9 + 26} height={26} rx={3} fill="#3a2a22" />
      <rect x={x + 2} y={y + 2} width={text.length * 9 + 22} height={22} rx={2} fill={tone} />
      <text
        x={x + 12}
        y={y + 18}
        fill="#2a1d18"
        fontSize={12}
        fontWeight={700}
        letterSpacing={1.4}
        style={{ fontFamily: "'Press Start 2P', ui-monospace, monospace" }}
      >
        {text}
      </text>
    </g>
  );
}

export function Office({ agents, onPick, active }: { agents: AgentView[]; onPick?: (id: string) => void; active?: string }) {
  const seated = useMemo(() => {
    const used: Record<string, number> = {};
    return agents.map((agent) => {
      const zone = agent.zone in SPOTS ? agent.zone : "planning";
      used[zone] = (used[zone] ?? 0) + 1;
      return { agent, spot: seat(zone, used[zone] - 1) };
    });
  }, [agents]);
  // The scene is 1000×660. Fit the largest copy of that exact rectangle inside the
  // available room; otherwise `slice` crops the coding/review zones off a short room.
  const fitRef = useRef<HTMLDivElement | null>(null);
  const [fit, setFit] = useState<{ width: number; height: number } | null>(null);
  useEffect(() => {
    const host = fitRef.current;
    if (!host) return;
    const measure = () => {
      const box = host.getBoundingClientRect();
      const scale = Math.min(box.width / 1000, box.height / 660);
      if (!Number.isFinite(scale) || scale <= 0) return;
      setFit({ width: Math.floor(1000 * scale), height: Math.floor(660 * scale) });
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="office-fit" ref={fitRef}>
      <div className="office" style={fit ? { width: fit.width, height: fit.height, flex: "none" } : undefined}>
      <div className="office-rug" />
      <svg className="office-scene" viewBox="0 0 1000 660" preserveAspectRatio="xMidYMid slice">
        <rect x={0} y={0} width={1000} height={112} fill="#33243f" />
        <rect x={0} y={0} width={1000} height={112} fill="url(#wallGrid)" opacity=".5" />
        <defs>
          <pattern id="wallGrid" width={40} height={40} patternUnits="userSpaceOnUse">
            <rect width={40} height={40} fill="none" />
            <rect width={40} height={2} fill="#3d2c4b" />
            <rect width={2} height={40} fill="#3d2c4b" />
          </pattern>
          <pattern id="floor" width={100} height={34} patternUnits="userSpaceOnUse">
            <rect width={100} height={34} fill="#a8784f" />
            <rect width={100} height={2} fill="#94663f" />
            <rect x={48} width={2} height={34} fill="#9c6d45" />
          </pattern>
        </defs>
        <rect x={0} y={112} width={1000} height={548} fill="url(#floor)" />
        <rect x={0} y={104} width={1000} height={10} fill="#241a2b" />

        <rect x={64} y={28} width={118} height={62} rx={2} fill="#1d2b3d" />
        <rect x={70} y={34} width={106} height={50} fill="#2d4a63" />
        <rect x={70} y={34} width={106} height={50} fill="#8ff0c9" opacity=".08" />
        <rect x={122} y={34} width={2} height={50} fill="#1d2b3d" />
        <rect x={70} y={56} width={106} height={2} fill="#1d2b3d" />

        <rect x={826} y={28} width={118} height={62} rx={2} fill="#1d2b3d" />
        <rect x={832} y={34} width={106} height={50} fill="#2d4a63" />
        <rect x={832} y={34} width={106} height={50} fill="#8ff0c9" opacity=".08" />
        <rect x={884} y={34} width={2} height={50} fill="#1d2b3d" />

        <rect x={330} y={24} width={340} height={54} rx={4} fill="#241a2b" />
        <rect x={336} y={30} width={328} height={42} rx={2} fill="#5c3d6b" />
        <text
          x={500}
          y={59}
          textAnchor="middle"
          fill="#f6e7c8"
          fontSize={20}
          letterSpacing={6}
          style={{ fontFamily: "'Press Start 2P', ui-monospace, monospace" }}
        >
          CORTEX
        </text>
        <rect x={330} y={78} width={340} height={4} fill="#8ff0c9" opacity=".35" />

        {/* zone tape */}
        <rect x={40} y={286} width={300} height={110} fill="#2f3a52" opacity=".45" />
        <rect x={40} y={286} width={300} height={110} fill="none" stroke="#5b6d92" strokeWidth={2} strokeDasharray="10 8" />
        <rect x={660} y={286} width={300} height={110} fill="#2b4a44" opacity=".45" />
        <rect x={660} y={286} width={300} height={110} fill="none" stroke="#4f8b7d" strokeWidth={2} strokeDasharray="10 8" />
        <rect x={628} y={404} width={348} height={190} fill="#3f3050" opacity=".4" />
        <rect x={628} y={404} width={348} height={190} fill="none" stroke="#77638f" strokeWidth={2} strokeDasharray="10 8" />
        <rect x={40} y={404} width={572} height={210} fill="#4a3a2c" opacity=".35" />
        <rect x={40} y={404} width={572} height={210} fill="none" stroke="#8a6a45" strokeWidth={2} strokeDasharray="10 8" />

        <Sign x={70} y={292} text="PLANNING" tone="#f2b544" />
        <Sign x={690} y={292} text="RESEARCH" tone="#5cc8c8" />
        <Sign x={648} y={410} text="REVIEW" tone="#a08bf0" />
        <Sign x={60} y={410} text="CODING" tone="#ef7d62" />

        <Whiteboard />
        <Rack />
        <RoundTable />
        <Desks />
        <Plant x={16} y={596} />
        <Plant x={958} y={250} />
        <Plant x={938} y={596} />
      </svg>

      {seated.map(({ agent, spot }) => {
        const color = roleColor(agent.role);
        const busy = isWorking(agent.status);
        const thinking = isThinking(agent.status);
        const side = spot.x > 500 ? "left" : "right";
        return (
          <button
            key={agent.id}
            type="button"
            className={`px-char${active === agent.id ? " is-active" : ""}${agent.status === "done" ? " is-done" : ""}`}
            style={{
              ["--sx" as string]: spot.x,
              ["--sy" as string]: spot.y,
              zIndex: Math.round(spot.y),
              ["--tone" as string]: color,
            }}
            onClick={() => onPick?.(agent.id)}
            title={`${agent.name} · ${agent.role}`}
          >
            {(agent.bubble || thinking) && (
              <span className="px-bubble">
                {agent.bubble ? (
                  <span className="px-bubble-text">{agent.bubble}</span>
                ) : (
                  <span className="px-dots">
                    <i />
                    <i />
                    <i />
                  </span>
                )}
              </span>
            )}
            <Sprite id={agent.id} color={color} status={agent.status} />
            {busy && <CodePanel file={agent.file} seed={hash(agent.id)} side={side} />}
            {busy && (
              <span className="px-desk-glow">
                <span />
                <span />
                <span />
              </span>
            )}
            <span className="px-tag">
              <b>{agent.name}</b>
              {agent.file && <em>{agent.file.split("/").pop()}</em>}
            </span>
          </button>
        );
      })}
      </div>
    </div>
  );
}

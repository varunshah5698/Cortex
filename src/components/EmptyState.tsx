import { Icon } from "./Icon";

const cards = [
  {
    icon: "pie" as const,
    title: "Synthesize Data",
    desc: "Turn my meeting notes into 5 key bullet points for the team.",
  },
  {
    icon: "bulb" as const,
    title: "Creative Brainstorm",
    desc: "Generate 3 taglines for a new sustainable fashion brand.",
  },
  {
    icon: "brush" as const,
    title: "Check Facts",
    desc: "Compare key differences between GDPR and CCPA.",
  },
];

export function Greeting({ name }: { name?: string }) {
  // The first word of the account's name, or nothing: "Hello, " with a trailing comma
  // and no name reads like a template that failed, which is what it would be.
  const who = (name ?? "").trim().split(/\s+/)[0];
  return (
    <div className="greeting">
      <div className="orb">
        <span className="orb-core" />
        <span className="orb-shine" />
      </div>
      <p className="hello">{who ? `Hello, ${who}` : "Welcome back"}</p>
      <h1>What should we build?</h1>
    </div>
  );
}

export function SuggestionCards({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="suggestions">
      {cards.map((c) => (
        <button className="suggestion" key={c.title} onClick={() => onPick(c.desc)}>
          <Icon name={c.icon} size={19} className="suggestion-icon" />
          <span className="suggestion-title">{c.title}</span>
          <span className="suggestion-desc">{c.desc}</span>
        </button>
      ))}
    </div>
  );
}

export function FooterNote() {
  return (
    <p className="footer-note">
      Join the valerius community for more insights{" "}
      <a href="https://discord.com" target="_blank" rel="noreferrer">
        Join Discord
      </a>
    </p>
  );
}

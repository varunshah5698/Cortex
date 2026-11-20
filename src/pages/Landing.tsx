import { Icon } from "../components/Icon";
import { navigate } from "../lib/router";

const steps = [
  {
    n: "01",
    title: "Plan",
    body: "One prompt becomes a typed plan: objectives, work units, dependencies and evidence requirements.",
  },
  {
    n: "02",
    title: "Spawn",
    body: "The orchestrator creates task-scoped specialists — frontend, backend, research, review — each on its own model.",
  },
  {
    n: "03",
    title: "Review",
    body: "Every artifact is checked against acceptance criteria. Rejected work goes back with findings, never silently dropped.",
  },
  {
    n: "04",
    title: "Synthesize",
    body: "Only accepted work reaches the answer, with claim-level evidence and uncertainty preserved.",
  },
];

const features = [
  {
    icon: "cpu" as const,
    title: "Per-role model routing",
    body: "Send planning to a reasoning model, frontend work to a fast coder, and review to a second opinion on a different provider.",
  },
  {
    icon: "layers" as const,
    title: "Unlimited specialists",
    body: "Add as many agents as the task needs. Each one carries its own base URL, key, model ID and role.",
  },
  {
    icon: "shield" as const,
    title: "Evidence ledger",
    body: "Atomic claims are tied to sources, spans and support status. Unsupported numbers fail closed, not silently.",
  },
  {
    icon: "wallet" as const,
    title: "Bounded budgets",
    body: "Token, time, retry and parallelism caps are enforced by the orchestrator, not by a polite prompt.",
  },
  {
    icon: "route" as const,
    title: "Provider agnostic",
    body: "Anything OpenAI-compatible: OpenAI, Anthropic, Groq, OpenRouter, vLLM, LM Studio, or a local Ollama daemon.",
  },
  {
    icon: "eye" as const,
    title: "Every decision visible",
    body: "Plans, handoffs, review findings and revisions are append-only events you can replay after the fact.",
  },
];

const providerChips = ["OpenAI", "Anthropic", "Groq", "OpenRouter", "Ollama", "vLLM", "LM Studio", "Together"];

const mockTeam = [
  { name: "Atlas", role: "Planner", state: "done" },
  { name: "Vega", role: "Frontend", state: "run" },
  { name: "Orion", role: "Backend", state: "run" },
  { name: "Lyra", role: "Reviewer", state: "wait" },
];

export default function Landing() {
  const go = (route: "login" | "workshop" | "app" | "build") => () => navigate(route);

  return (
    <div className="site">
      <header className="site-nav">
        <div className="site-nav-inner">
          <a className="site-brand" href="#/">
            <span className="site-mark">
              <Icon name="logo" size={15} />
            </span>
            <span>Cortex</span>
          </a>
          <nav className="site-links">
            <a href="#harness">Harness</a>
            <a href="#workflow">Workflow</a>
            <a href="#models">Models</a>
            <button onClick={() => navigate("build")}>Build room</button>
            <a href="#faq">FAQ</a>
          </nav>
          <div className="site-actions">
            <button className="link-btn" onClick={go("login")}>
              Sign in
            </button>
            <button className="site-cta" onClick={go("workshop")}>
              Open workshop
              <Icon name="arrowRight" size={15} />
            </button>
          </div>
        </div>
      </header>

      <section className="hero">
        <div className="hero-glow" aria-hidden />
        <span className="eyebrow">
          <i className="dot" />
          Chain of Command harness — v0.1
        </span>
        <h1>
          One prompt.
          <br />
          A whole <span className="grad">chain of command.</span>
        </h1>
        <p className="hero-sub">
          Cortex plans the work, spawns specialists, reviews every claim, and returns an answer you can
          audit — running on the models you already have keys for.
        </p>
        <div className="hero-actions">
          <button className="cta-primary" onClick={go("workshop")}>
            Assemble your team
            <Icon name="arrowRight" size={16} />
          </button>
          <button className="cta-ghost" onClick={go("build")}>
            Watch a team build
          </button>
        </div>
        <div className="hero-providers">
          <span>Works with</span>
          {providerChips.slice(0, 6).map((p) => (
            <em key={p}>{p}</em>
          ))}
        </div>

        <div className="mock" id="harness">
          <div className="mock-glow" aria-hidden />
          <div className="mock-window">
            <aside className="mock-rail">
              <div className="mock-brand">
                <span className="mock-mark">
                  <Icon name="logo" size={11} />
                </span>
                Cortex
              </div>
              <p className="mock-label">Task · chain-01</p>
              <div className="mock-agents">
                {mockTeam.map((a) => (
                  <div className="mock-agent" key={a.name}>
                    <span className={`mock-state is-${a.state}`} />
                    <span className="mock-agent-name">{a.name}</span>
                    <span className="mock-agent-role">{a.role}</span>
                  </div>
                ))}
              </div>
              <div className="mock-budget">
                <div className="mock-budget-row">
                  <span>Budget</span>
                  <span>38%</span>
                </div>
                <div className="mock-bar">
                  <i />
                </div>
              </div>
            </aside>

            <div className="mock-main">
              <div className="mock-top">
                <span className="mock-pill">
                  <Icon name="logo" size={10} />
                  Cortex
                </span>
                <span className="mock-top-right">plan · running</span>
              </div>
              <div className="mock-orb" />
              <p className="mock-hello">Hello, Jackson</p>
              <h3>How can I assist you today?</h3>
              <div className="mock-composer">
                <span>Ask me anything…</span>
                <i className="mock-send" />
              </div>
              <div className="mock-cards">
                <div className="mock-card">
                  <Icon name="pie" size={13} />
                  <b>Synthesize Data</b>
                </div>
                <div className="mock-card">
                  <Icon name="bulb" size={13} />
                  <b>Creative Brainstorm</b>
                </div>
                <div className="mock-card">
                  <Icon name="brush" size={13} />
                  <b>Check Facts</b>
                </div>
              </div>
            </div>
          </div>

          <div className="mock-float mock-float-a">
            <span className="mock-float-icon">
              <Icon name="shield" size={13} />
            </span>
            <div>
              <b>Claim supported</b>
              <span>2 sources · direct</span>
            </div>
          </div>
          <div className="mock-float mock-float-b">
            <span className="mock-float-icon is-warn">
              <Icon name="alert" size={13} />
            </span>
            <div>
              <b>Revision requested</b>
              <span>unsourced number</span>
            </div>
          </div>
        </div>
      </section>

      <section className="section" id="workflow">
        <div className="section-head">
          <span className="kicker">The loop</span>
          <h2>Work moves through a command chain, not a chat scroll.</h2>
          <p>
            One orchestrator owns state and budget. Specialists propose work, review it, and ship only what
            survives acceptance.
          </p>
        </div>
        <div className="steps">
          {steps.map((s) => (
            <article className="step" key={s.n}>
              <span className="step-n">{s.n}</span>
              <h3>{s.title}</h3>
              <p>{s.body}</p>
            </article>
          ))}
        </div>
      </section>

      <section className="section" id="models">
        <div className="section-head">
          <span className="kicker">Bring your own models</span>
          <h2>Every role is a slot. Fill it with whatever you trust.</h2>
          <p>
            Paste a base URL, a key and a model ID. Add four specialists or forty — the harness keeps their
            permissions, context and budgets separate.
          </p>
        </div>
        <div className="features">
          {features.map((f) => (
            <article className="feature" key={f.title}>
              <span className="feature-icon">
                <Icon name={f.icon} size={18} />
              </span>
              <h3>{f.title}</h3>
              <p>{f.body}</p>
            </article>
          ))}
        </div>
        <div className="chip-row">
          {providerChips.map((p) => (
            <span className="provider-chip" key={p}>
              {p}
            </span>
          ))}
          <span className="provider-chip is-more">+ any OpenAI-compatible endpoint</span>
        </div>
      </section>

      <section className="section" id="faq">
        <div className="cta-band">
          <div>
            <h2>Two minutes to a standing team.</h2>
            <p>No card, no hosted account with us — your keys stay in your workspace.</p>
          </div>
          <button className="cta-light" onClick={go("workshop")}>
            Open the workshop
            <Icon name="arrowRight" size={16} />
          </button>
        </div>
      </section>

      <footer className="site-footer">
        <div className="footer-top">
          <div className="footer-brand">
            <span className="site-brand">
              <span className="site-mark">
                <Icon name="logo" size={14} />
              </span>
              Cortex
            </span>
            <p>A topic-agnostic multi-agent harness with evidence-backed review.</p>
          </div>
          <div className="footer-cols">
            <div>
              <h4>Harness</h4>
              <a href="#workflow">Workflow</a>
              <a href="#models">Models</a>
              <a href="#harness">Live preview</a>
            </div>
            <div>
              <h4>Build</h4>
              <a href="#faq">MCP server</a>
              <a href="#faq">Evaluation</a>
              <a href="#faq">Reference</a>
            </div>
            <div>
              <h4>Account</h4>
              <button className="footer-link" onClick={go("login")}>
                Sign in
              </button>
              <button className="footer-link" onClick={go("workshop")}>
                Workshop
              </button>
            </div>
          </div>
        </div>
        <div className="footer-bottom">
          <span>© 2026 Cortex Labs</span>
          <span>Built with the Chain of Command blueprint, v0.1</span>
        </div>
      </footer>
    </div>
  );
}

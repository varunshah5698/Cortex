"""Model roulette: one specialist, several models, chosen per call.

mini-swe-agent ships this as a "meta-model" and it is a genuinely strange idea that turns
out to work: instead of pinning an agent to one model for a whole run, draw a model from
a pool at every call. The SWE-bench team's own write-up reports that randomly alternating
between two strong models beats either one alone, because the models fail differently —
one gets stuck on a parsing detail, the other on a selector, and the alternation keeps a
single bad habit from steering the entire trajectory.

Cortex has a roster of specialists, each pinned to one model, and a single fallback that
only fires when the primary is hard-down. Neither gives you *diversity*: a run that picks
a bad model at the start keeps it, and the fallback is a rescue, not a strategy.

This module adds the third option, as a pure function over a `Specialist` so it needs no
new agent, node or call site:

* ``pick(spec)`` returns the specialist to use for *this* call — the same object when
  roulette is off, which is the default and costs one comparison;
* ``record(...)`` notes which model actually answered, so the transcript says so and the
  usage ledger attributes the tokens to the model that spent them.

Two properties matter for honesty. The pool is drawn from the roster, so a model can only
be used if an operator configured it and its key resolves. And the choice is *reported*
on every call, because a run whose models are invisible cannot be debugged or priced.

The draw is seeded per run when `HARNESS_ROULETTE_SEED` is set, so a surprising result
can be reproduced exactly; unseeded it uses the process RNG, which is what the research
setting wants.
"""

from __future__ import annotations

import dataclasses
import random

from ..config.settings import ROULETTE_ENABLED, ROULETTE_MODELS, ROULETTE_SEED
from ..models import Specialist

# One RNG per run id, so two concurrent runs cannot interleave each other's draws and a
# seeded run replays the same sequence. Bounded because a long-lived process serves many
# runs and this map has no other reason to grow.
_rngs: dict[str, random.Random] = {}
MAX_TRACKED = 64


def _rng(run_id: str) -> random.Random:
    key = run_id or "anonymous"
    generator = _rngs.get(key)
    if generator is None:
        if len(_rngs) >= MAX_TRACKED:
            _rngs.pop(next(iter(_rngs)), None)
        generator = random.Random(ROULETTE_SEED if ROULETTE_SEED is not None else None)
        _rngs[key] = generator
    return generator


def enabled() -> bool:
    return ROULETTE_ENABLED and bool(ROULETTE_MODELS)


def pool(spec: Specialist, roster: list[Specialist] | None = None) -> list[Specialist]:
    """The models this specialist may be drawn from, itself included.

    `HARNESS_ROULETTE_MODELS` names models, not specialist ids: the point is to vary the
    model behind a role, not to swap the role for a different one. A name that matches
    nothing in the roster is ignored rather than invented — a pool entry with no key is a
    run that dies at the next call.
    """
    if not enabled():
        return [spec]
    wanted = {name.strip().lower() for name in ROULETTE_MODELS if name.strip()}
    members = [spec]
    for candidate in roster or []:
        if candidate.id == spec.id:
            continue
        if candidate.model.strip().lower() in wanted and candidate.ready:
            members.append(candidate)
    return members


def pick(spec: Specialist, roster: list[Specialist] | None = None, run_id: str = "") -> Specialist:
    """The specialist to use for one call: `spec` itself unless roulette is on."""
    members = pool(spec, roster)
    if len(members) <= 1:
        return spec
    return _rng(run_id).choice(members)


def record(chosen: Specialist, original: Specialist) -> dict | None:
    """The event that says which model answered, or None when nothing varied.

    Returned rather than emitted so the caller decides where it lands; `llm.call` emits it
    through the bound run, which is the one place every provider call already passes.
    """
    if chosen.model == original.model and chosen.id == original.id:
        return None
    return {
        "type": "roulette",
        "agent": original.id,
        "model": chosen.model,
        "from": original.model,
        "message": f"{original.name} drew {chosen.model} for this call",
    }


def variants(spec: Specialist, roster: list[Specialist] | None = None) -> list[str]:
    """The distinct models in this specialist's pool, for a health report."""
    seen: list[str] = []
    for member in pool(spec, roster):
        if member.model and member.model not in seen:
            seen.append(member.model)
    return seen


def replace(spec: Specialist, chosen: Specialist) -> Specialist:
    """`spec` with the drawn model's connection details, keeping the role and identity.

    The agent stays who it is — same id, name and role, so the transcript still reads as
    one specialist working — but the call goes to the drawn model's endpoint and key.
    """
    if chosen is spec:
        return spec
    return dataclasses.replace(
        spec,
        model=chosen.model,
        baseUrl=chosen.baseUrl,
        kind=chosen.kind,
        apiKey=chosen.apiKey,
    )


def reset(run_id: str = "") -> None:
    """Forget a run's RNG. Tests call this; production does not need to."""
    if run_id:
        _rngs.pop(run_id, None)
    else:
        _rngs.clear()

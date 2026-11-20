@AGENTS.md

## Claude-specific notes

- `.claude/settings.json` adds what a markdown file cannot: a `Stop` hook that runs
  `bash scripts/verify.sh`, so a turn cannot end on a broken build, and explicit
  allow / ask / deny rules (deny: `.env`, `.harness-token`, `.harness-roster.json`,
  `rm -rf`, `sudo`, `--no-verify`).
- If the Stop hook blocks you, read the output it prints and fix the cause. Do not rerun
  with the hook disabled and do not edit `tests/` or `evals/cases.py` to get past it.
- `workspace/runs/<id>/events.jsonl` is a run's transcript. When a harness run
  misbehaves, read that log before changing code; the failure is usually named in it.

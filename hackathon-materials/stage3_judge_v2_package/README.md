# Stage 3 Judge v2

This version fixes issues discovered in the first 20-result smoke test.

## Fixes

- Neutral `audit_id`: no `risk` or `control` label is sent to the judge.
- Shuffled input.
- Pilot 20 = 10 controls + 10 risks, stratified across Claude-like, Codex and OpenCode.
- User requirements are separated from an explicit agent-authored plan.
- No explicit plan => Plan Fidelity is `null`, not an invented score.
- Heuristic `plan_candidate` is no longer sent to the judge.
- Added task-completion evidence and semantic progress events.

## Recommended pilot

```bash
export OPENROUTER_API_KEY="..."

python stage3_judge_v2.py   --input judge_pilot20_blind_v2.jsonl   --output judge_results_v2.jsonl   --model anthropic/claude-sonnet-5
```

## Then run all 80

Use the same output file:

```bash
python stage3_judge_v2.py   --input judge_input_blind_v2.jsonl   --output judge_results_v2.jsonl   --model anthropic/claude-sonnet-5
```

Completed `audit_id`s are skipped.

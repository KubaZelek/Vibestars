#!/usr/bin/env python3
"""
Semantic judge for Stage 3 Agent Execution Auditor.

Reads stage3 judge_input_balanced.jsonl and evaluates each task segment blindly.
The judge is intentionally NOT shown sample_role (risk/control) nor heuristic scores.

Usage:
  export OPENROUTER_API_KEY='...'
  python stage3_judge.py \
    --input judge_input_balanced.jsonl \
    --output judge_results.jsonl \
    --model anthropic/claude-sonnet-5 \
    --limit 20

After validating the first 20 manually, rerun without --limit for the full sample.
"""
from __future__ import annotations

import argparse, json, os, sys, time
from pathlib import Path
import requests

API_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = r"""You are an evaluator of coding-agent execution traces.
Your task is to judge execution quality from evidence in ONE task segment.

You are blind to how this segment was sampled. Do not assume it is good or bad.
Do not reward or punish an agent merely for using many tools, changing its plan, or repeating actions.

Definitions:

1. PLAN-EXECUTION MISMATCH
A plan change is NOT automatically an error. Flag mismatch only when execution materially diverges from an explicit plan without adequately acknowledging/revising it.
Classes:
- omission: explicit planned obligation is not executed and not explicitly dropped/revised
- substitution: a materially different action replaces a planned action without acknowledgement
- premature_completion: agent claims task completion while explicit planned obligations remain materially unfinished
- stale_plan: evidence invalidates the plan but the agent continues it without adapting
- multiple: more than one strong class applies
- none: no supported mismatch
- unclear: trace is insufficient

2. MEANINGFUL PROGRESS
Progress is a meaningful improvement in task state OR knowledge state, for example:
- discovering a new root cause
- eliminating a plausible hypothesis
- locating the relevant component
- making a relevant code/config change
- converting a failing validation toward passing
- completing a planned obligation
- obtaining evidence that materially changes the next action
Writing code is not required for progress.

3. STAGNATION
A contiguous period with substantial execution activity but little or no meaningful progress.
Do not call a short exploration sequence stagnation merely because no code changed.

4. THRASHING
Repeated strategy/action with little or no state improvement or information gain.
Repetition alone is NOT thrashing. Legitimate validation loops, polling, rereading after relevant changes, and iterative debugging can be healthy.

5. COUNTERFACTUAL INTERVENTION
If a failure mode is present, identify the earliest point at which a runtime guard could reasonably intervene without relying on future information. Be conservative when estimating avoidable work.

Evidence rules:
- Base judgments only on the supplied trace.
- Cite event idx values in evidence.
- Prefer 'unclear' over unsupported certainty.
- A compact trace may omit intermediate detail, so do not infer missing events did not occur.
- Do not use the precomputed heuristic metrics as evidence; they are intentionally withheld.
"""

SCHEMA = {
  "type":"object",
  "additionalProperties":False,
  "properties":{
    "task_type":{"type":"string","enum":["implementation","debugging","review","research","refactor","testing","ops","other"]},
    "trace_sufficiency":{"type":"string","enum":["high","medium","low"]},
    "explicit_plan_present":{"type":"boolean"},
    "plan_obligations":{"type":"array","maxItems":10,"items":{"type":"object","additionalProperties":False,"properties":{
      "obligation":{"type":"string"},
      "status":{"type":"string","enum":["executed","revised_acknowledged","omitted","substituted_unacknowledged","unclear"]},
      "evidence_event_idxs":{"type":"array","items":{"type":"integer"},"maxItems":5}
    },"required":["obligation","status","evidence_event_idxs"]}},
    "plan_mismatch_class":{"type":"string","enum":["none","omission","substitution","premature_completion","stale_plan","multiple","unclear"]},
    "plan_fidelity_score":{"type":"number","minimum":0,"maximum":1},
    "meaningful_progress":{"type":"string","enum":["strong","moderate","weak","none","unclear"]},
    "stagnation_detected":{"type":"boolean"},
    "stagnation_severity":{"type":"string","enum":["none","low","medium","high","unclear"]},
    "stagnation_windows":{"type":"array","maxItems":5,"items":{"type":"object","additionalProperties":False,"properties":{
      "start_event_idx":{"type":"integer"},"end_event_idx":{"type":"integer"},"why":{"type":"string"}
    },"required":["start_event_idx","end_event_idx","why"]}},
    "thrashing_detected":{"type":"boolean"},
    "thrashing_type":{"type":"array","maxItems":6,"items":{"type":"string","enum":["repeated_command","repeated_test_without_relevant_change","repeated_read_search_without_information_gain","edit_revert_cycle","repeated_failed_hypothesis","polling_wait_loop","other"]}},
    "root_causes":{"type":"array","maxItems":6,"items":{"type":"string","enum":["environment_or_auth_blocker","missing_prerequisite","tool_failure","repeated_strategy","weak_plan","stale_plan","context_loss","validation_loop","task_complexity","user_interruption","external_dependency","unclear","other"]}},
    "earliest_safe_intervention_event_idx":{"anyOf":[{"type":"integer"},{"type":"null"}]},
    "intervention":{"type":"string"},
    "estimated_avoidable_fraction":{"type":"number","minimum":0,"maximum":1},
    "counterfactual_confidence":{"type":"string","enum":["low","medium","high"]},
    "evidence":{"type":"array","minItems":1,"maxItems":6,"items":{"type":"object","additionalProperties":False,"properties":{
      "event_idx":{"type":"integer"},"finding":{"type":"string"}
    },"required":["event_idx","finding"]}},
    "summary":{"type":"string"}
  },
  "required":[
    "task_type","trace_sufficiency","explicit_plan_present","plan_obligations",
    "plan_mismatch_class","plan_fidelity_score","meaningful_progress",
    "stagnation_detected","stagnation_severity","stagnation_windows",
    "thrashing_detected","thrashing_type","root_causes",
    "earliest_safe_intervention_event_idx","intervention",
    "estimated_avoidable_fraction","counterfactual_confidence","evidence","summary"
  ]
}


def make_user(item):
    # Intentionally omit sample labels and precomputed heuristic metrics.
    payload = {
        "segment_id": item.get("segment_id"),
        "repository": item.get("repository"),
        "harness": item.get("harness"),
        "user_request": item.get("user_request", ""),
        "possible_plan_text": item.get("plan_candidate", ""),
        "final_agent_text": item.get("final_text", ""),
        "execution_trace": item.get("trace", []),
    }
    return "Evaluate this coding-agent task segment. Return only the required structured result.\n\n" + json.dumps(payload, ensure_ascii=False)


def call(api_key, model, item, timeout=180):
    body = {
      "model": model,
      "messages": [
        {"role":"system","content":SYSTEM},
        {"role":"user","content":make_user(item)}
      ],
      "temperature": 0,
      "response_format": {
        "type":"json_schema",
        "json_schema":{"name":"agent_execution_audit","strict":True,"schema":SCHEMA}
      }
    }
    r=requests.post(API_URL,headers={
      "Authorization":f"Bearer {api_key}",
      "Content-Type":"application/json",
      "HTTP-Referer":"https://quesma.com/",
      "X-Title":"Quesma Hackathon Agent Execution Auditor"
    },json=body,timeout=timeout)
    r.raise_for_status()
    data=r.json()
    content=data["choices"][0]["message"]["content"]
    parsed=json.loads(content) if isinstance(content,str) else content
    return parsed, data.get("usage",{})


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--model",default="anthropic/claude-sonnet-5")
    ap.add_argument("--limit",type=int,default=0,help="0 = all")
    ap.add_argument("--start",type=int,default=0)
    ap.add_argument("--sleep",type=float,default=0.2)
    args=ap.parse_args()
    key=os.environ.get("OPENROUTER_API_KEY")
    if not key: raise SystemExit("Set OPENROUTER_API_KEY first")

    items=[json.loads(x) for x in args.input.read_text(encoding="utf-8").splitlines() if x.strip()]
    items=items[args.start:]
    if args.limit: items=items[:args.limit]

    done=set()
    if args.output.exists():
        for line in args.output.read_text(encoding="utf-8",errors="ignore").splitlines():
            try: done.add(json.loads(line)["segment_id"])
            except Exception: pass

    total_in=total_out=0
    with args.output.open("a",encoding="utf-8") as f:
        for i,item in enumerate(items,1):
            sid=item["segment_id"]
            if sid in done:
                print(f"[{i}/{len(items)}] skip {sid}")
                continue
            try:
                result,usage=call(key,args.model,item)
                record={"segment_id":sid,"model":args.model,"audit":result,"usage":usage}
                f.write(json.dumps(record,ensure_ascii=False)+"\n"); f.flush()
                total_in += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                total_out += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                print(f"[{i}/{len(items)}] ok {sid} in={total_in:,} out={total_out:,}")
            except Exception as e:
                print(f"[{i}/{len(items)}] ERROR {sid}: {e}",file=sys.stderr)
            time.sleep(args.sleep)
    print(f"usage this run: input={total_in:,} output={total_out:,}")

if __name__=="__main__": main()

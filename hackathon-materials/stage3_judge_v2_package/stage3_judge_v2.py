#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
import requests

API_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = r"""You are an evaluator of coding-agent execution traces.
Judge execution quality from evidence in ONE task segment.

You are blind to how the segment was sampled.
Do not infer a hidden class from repository, harness, ordering, or audit identifier.
Do not assume the execution is good or bad.

CRITICAL DISTINCTION: USER REQUIREMENTS VS AGENT PLAN
- The user's request defines task requirements.
- An explicit agent plan consists only of future actions the AGENT states it intends to perform.
- Do NOT convert the user's requirements into agent plan obligations.
- Do NOT treat a retrospective final summary as a plan.
- If no explicit agent-authored plan exists:
  explicit_plan_present = false
  plan_obligations = []
  plan_mismatch_class = "not_applicable"
  plan_fidelity_score = null

TASK COMPLETION
Assess separately whether the supplied trace supports satisfaction of the user's material task requirements.

PLAN-EXECUTION MISMATCH
A plan change is NOT automatically an error.
Flag mismatch only when execution materially diverges from an explicit agent-authored plan without adequate acknowledgement/revision.
Classes:
- omission
- substitution
- premature_completion
- stale_plan
- multiple
- none
- not_applicable
- unclear

MEANINGFUL PROGRESS
Progress is a meaningful improvement in task state OR knowledge state, e.g.:
- discovering a root cause
- eliminating a plausible hypothesis
- locating the relevant component
- making a relevant state/code/config change
- improving validation
- completing a planned obligation
- obtaining evidence that materially changes the next action

STAGNATION
A contiguous period with substantial execution activity but little or no meaningful progress.

THRASHING
Repeated strategy/action with little or no state improvement or information gain.
Repetition alone is NOT thrashing.
Legitimate validation loops, polling, rereading after relevant changes, and iterative debugging can be healthy.

COUNTERFACTUAL INTERVENTION
If a failure mode is present, identify the earliest point where a runtime guard could reasonably intervene using only information available at that point.
Be conservative. If no meaningful inefficiency is supported, use null intervention event and avoidable fraction near 0.

EVIDENCE RULES
- Base judgments only on the supplied trace.
- Cite event idx values.
- Prefer "unclear" over unsupported certainty.
- A compact trace may omit intermediate detail.
- Tool errors can still yield useful information.
"""

SCHEMA = {
  "type":"object",
  "additionalProperties":False,
  "properties":{
    "task_type":{"type":"string","enum":["implementation","debugging","review","research","refactor","testing","ops","other"]},
    "trace_sufficiency":{"type":"string","enum":["high","medium","low"]},

    "task_completion_status":{"type":"string","enum":["complete","mostly_complete","partial","incomplete","unclear"]},
    "task_completion_evidence":{"type":"array","maxItems":6,"items":{
      "type":"object","additionalProperties":False,
      "properties":{
        "requirement":{"type":"string"},
        "status":{"type":"string","enum":["satisfied","partially_satisfied","unsatisfied","unclear"]},
        "evidence_event_idxs":{"type":"array","items":{"type":"integer"},"maxItems":5}
      },
      "required":["requirement","status","evidence_event_idxs"]
    }},

    "explicit_plan_present":{"type":"boolean"},
    "plan_obligations":{"type":"array","maxItems":10,"items":{
      "type":"object","additionalProperties":False,
      "properties":{
        "obligation":{"type":"string"},
        "status":{"type":"string","enum":["executed","revised_acknowledged","omitted","substituted_unacknowledged","unclear"]},
        "evidence_event_idxs":{"type":"array","items":{"type":"integer"},"maxItems":5}
      },
      "required":["obligation","status","evidence_event_idxs"]
    }},
    "plan_mismatch_class":{"type":"string","enum":["none","omission","substitution","premature_completion","stale_plan","multiple","not_applicable","unclear"]},
    "plan_fidelity_score":{"anyOf":[{"type":"number","minimum":0,"maximum":1},{"type":"null"}]},

    "meaningful_progress":{"type":"string","enum":["strong","moderate","weak","none","unclear"]},
    "progress_events":{"type":"array","maxItems":12,"items":{
      "type":"object","additionalProperties":False,
      "properties":{
        "event_idx":{"type":"integer"},
        "kind":{"type":"string","enum":["discovery","hypothesis_eliminated","relevant_location_found","state_change","validation_improved","obligation_completed","other"]},
        "description":{"type":"string"}
      },
      "required":["event_idx","kind","description"]
    }},

    "stagnation_detected":{"type":"boolean"},
    "stagnation_severity":{"type":"string","enum":["none","low","medium","high","unclear"]},
    "stagnation_windows":{"type":"array","maxItems":5,"items":{
      "type":"object","additionalProperties":False,
      "properties":{
        "start_event_idx":{"type":"integer"},
        "end_event_idx":{"type":"integer"},
        "why":{"type":"string"}
      },
      "required":["start_event_idx","end_event_idx","why"]
    }},

    "thrashing_detected":{"type":"boolean"},
    "thrashing_type":{"type":"array","maxItems":6,"items":{
      "type":"string",
      "enum":["repeated_command","repeated_test_without_relevant_change","repeated_read_search_without_information_gain","edit_revert_cycle","repeated_failed_hypothesis","polling_wait_loop","other"]
    }},

    "root_causes":{"type":"array","maxItems":6,"items":{
      "type":"string",
      "enum":["environment_or_auth_blocker","missing_prerequisite","tool_failure","repeated_strategy","weak_plan","stale_plan","context_loss","validation_loop","task_complexity","user_interruption","external_dependency","unclear","other"]
    }},

    "earliest_safe_intervention_event_idx":{"anyOf":[{"type":"integer"},{"type":"null"}]},
    "intervention":{"type":"string"},
    "estimated_avoidable_fraction":{"type":"number","minimum":0,"maximum":1},
    "counterfactual_confidence":{"type":"string","enum":["low","medium","high"]},

    "evidence":{"type":"array","minItems":1,"maxItems":8,"items":{
      "type":"object","additionalProperties":False,
      "properties":{"event_idx":{"type":"integer"},"finding":{"type":"string"}},
      "required":["event_idx","finding"]
    }},
    "summary":{"type":"string"}
  },
  "required":[
    "task_type","trace_sufficiency",
    "task_completion_status","task_completion_evidence",
    "explicit_plan_present","plan_obligations","plan_mismatch_class","plan_fidelity_score",
    "meaningful_progress","progress_events",
    "stagnation_detected","stagnation_severity","stagnation_windows",
    "thrashing_detected","thrashing_type","root_causes",
    "earliest_safe_intervention_event_idx","intervention",
    "estimated_avoidable_fraction","counterfactual_confidence",
    "evidence","summary"
  ]
}

def make_user(item):
    payload = {
        "audit_id": item["audit_id"],
        "repository": item.get("repository"),
        "harness": item.get("harness"),
        "user_request": item.get("user_request",""),
        "final_agent_text": item.get("final_agent_text",""),
        "execution_trace": item.get("trace",[]),
    }
    return "Evaluate this coding-agent task segment. Return only the required structured result.\n\n" + json.dumps(payload, ensure_ascii=False)

def call(api_key, model, item, timeout=240):
    body = {
      "model": model,
      "messages":[
        {"role":"system","content":SYSTEM},
        {"role":"user","content":make_user(item)}
      ],
      "temperature":0,
      "response_format":{
        "type":"json_schema",
        "json_schema":{"name":"agent_execution_audit_v2","strict":True,"schema":SCHEMA}
      }
    }
    r = requests.post(API_URL, headers={
      "Authorization":f"Bearer {api_key}",
      "Content-Type":"application/json",
      "HTTP-Referer":"https://quesma.com/",
      "X-Title":"Quesma Hackathon Agent Execution Auditor v2"
    }, json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    content = data["choices"][0]["message"]["content"]
    parsed = json.loads(content) if isinstance(content,str) else content
    return parsed, data.get("usage",{})

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",type=Path,required=True)
    ap.add_argument("--output",type=Path,required=True)
    ap.add_argument("--model",default="anthropic/claude-sonnet-5")
    ap.add_argument("--limit",type=int,default=0,help="0 = all")
    ap.add_argument("--sleep",type=float,default=0.2)
    args = ap.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("Set OPENROUTER_API_KEY first")

    items = [json.loads(x) for x in args.input.read_text(encoding="utf-8").splitlines() if x.strip()]
    if args.limit:
        items = items[:args.limit]

    done = set()
    if args.output.exists():
        for line in args.output.read_text(encoding="utf-8",errors="ignore").splitlines():
            try:
                done.add(json.loads(line)["audit_id"])
            except Exception:
                pass

    total_in = total_out = 0
    total_cost = 0.0

    with args.output.open("a",encoding="utf-8") as f:
        for i,item in enumerate(items,1):
            aid = item["audit_id"]
            if aid in done:
                print(f"[{i}/{len(items)}] skip {aid}")
                continue
            try:
                result,usage = call(key,args.model,item)
                record = {"audit_id":aid,"model":args.model,"audit":result,"usage":usage}
                f.write(json.dumps(record,ensure_ascii=False)+"\n")
                f.flush()

                total_in += int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                total_out += int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
                try:
                    total_cost += float(usage.get("cost") or 0)
                except Exception:
                    pass

                print(f"[{i}/{len(items)}] ok {aid} in={total_in:,} out={total_out:,} cost=${total_cost:.4f}")
            except Exception as e:
                print(f"[{i}/{len(items)}] ERROR {aid}: {e}",file=sys.stderr)
            time.sleep(args.sleep)

    print(f"usage this run: input={total_in:,} output={total_out:,} reported_cost=${total_cost:.4f}")

if __name__=="__main__":
    main()

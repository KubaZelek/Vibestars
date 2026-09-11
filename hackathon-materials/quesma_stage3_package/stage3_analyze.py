#!/usr/bin/env python3
"""
Quesma Hackathon - Stage 3
Normalize Codex / Claude-like / OpenCode transcripts into task-level segments,
then compute deterministic execution features and emit compact LLM-judge inputs.

Important: deterministic metrics here are candidate signals, not semantic ground truth.
"""
from __future__ import annotations

import argparse, ast, csv, hashlib, json, math, re
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

PLAN_WORD_RE = re.compile(r"(?i)\b(plan|approach|steps?|checklist)\b")
PLAN_SEQUENCE_RE = re.compile(r"(?i)\b(first|firstly|then|next|after that|finally)\b")
PLAN_BULLET_RE = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s+\S+")
COMPLETION_RE = re.compile(r"(?i)\b(done|fixed|implemented|completed|resolved|finished|ready|all set|works now|passed)\b")
ERROR_RE = re.compile(r"(?i)(traceback|panic|fatal|timed out|timeout|assertion(?:error)?|not authenticated|permission denied)\b")
NONZERO_EXIT_RE = re.compile(r"(?i)(?:process exited with code|exit code:?)\s*([1-9]\d*)")
TEST_FAILURE_RE = re.compile(r"(?i)(?:\bFAILED\b|tests? failed|failures?:\s*[1-9]\d*)")
TEST_CMD_RE = re.compile(
    r"(?i)(?:^|[;&|\s])(?:pytest|py\.test|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+(?:run\s+)?test|bun\s+test|cargo\s+test|go\s+test|vitest|jest|rspec|mvn\s+test|gradle\s+test|dotnet\s+test|ctest)(?:\s|$)"
)

READ_NAMES = {"read", "read_file", "cat", "view", "open_file"}
SEARCH_NAMES = {"grep", "glob", "search", "find", "ripgrep", "rg", "list", "ls", "semantic_search"}
EDIT_NAMES = {"edit", "write", "write_file", "apply_patch", "patch", "str_replace", "multiedit", "create_file"}
SHELL_NAMES = {"bash", "shell", "terminal", "exec", "run", "command", "run_command"}


def s(v: Any, limit: int = 3000) -> str:
    if v is None: return ""
    if isinstance(v, str): return v[:limit]
    try: return json.dumps(v, ensure_ascii=False, sort_keys=True)[:limit]
    except Exception: return str(v)[:limit]


def canonical(v: Any) -> str:
    if isinstance(v, str):
        return re.sub(r"\s+", " ", v.strip())[:1500]
    if isinstance(v, dict):
        drop = {"timestamp","time","id","call_id","uuid","sessionID","messageID"}
        return json.dumps({k:v[k] for k in sorted(v) if k not in drop}, ensure_ascii=False, sort_keys=True, separators=(",",":"))[:2000]
    return s(v, 2000)


def hash_sig(tool: str, args: Any) -> str:
    raw = f"{tool.lower()}|{canonical(args)}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def extract_command(args: Any) -> str:
    if isinstance(args, dict):
        for k in ("command","cmd","script","input"):
            if isinstance(args.get(k), str): return args[k]
    if isinstance(args, str): return args
    return ""


def extract_target(args: Any) -> str:
    if isinstance(args, dict):
        for k in ("file_path","filePath","path","filename","target","query"):
            if isinstance(args.get(k), str): return args[k][:500]
    return ""


def classify_tool(name: str, args: Any) -> str:
    n = (name or "").lower().replace("-","_")
    cmd = extract_command(args)
    if n in ("update_plan","exitplanmode","exit_plan_mode"): return "PLAN_UPDATE"
    if n in ("agent","spawn_agent","wait_agent","send_input","close_agent"): return "SUBAGENT"
    if TEST_CMD_RE.search(cmd): return "TEST"
    if n in READ_NAMES or n.endswith("read") or "read_file" in n: return "READ"
    if n in SEARCH_NAMES or any(x in n for x in ("grep","glob","search")): return "SEARCH"
    if n in EDIT_NAMES or any(x in n for x in ("apply_patch","edit","write_file")): return "EDIT"
    if n in SHELL_NAMES or any(x in n for x in ("shell","bash","exec","terminal")): return "COMMAND"
    if "web" in n and "search" in n: return "SEARCH"
    return "TOOL"


def result_status(text: str, explicit: Any = None, tool_class: str = "") -> str:
    if explicit in (False, "failed", "error"): return "error"
    # Shell/test transport can be "completed" while the command itself exited non-zero.
    if tool_class in ("COMMAND","TEST"):
        if NONZERO_EXIT_RE.search(text or "") or TEST_FAILURE_RE.search(text or ""):
            return "error"
    if explicit in (True, "completed", "success", "ok"): return "ok"
    if ERROR_RE.search(text or ""): return "error"
    return "ok"


def load_json_any(path: Path) -> tuple[list[dict], str]:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and "info" in obj and "messages" in obj:
            return [obj], "opencode"
        if isinstance(obj, list): return [x for x in obj if isinstance(x, dict)], "json_array"
        if isinstance(obj, dict): return [obj], "json_object"
    except Exception:
        pass
    rows=[]
    for line in text.splitlines():
        try:
            o=json.loads(line)
            if isinstance(o,dict): rows.append(o)
        except Exception: pass
    types=Counter(o.get("type") for o in rows)
    if types["session_meta"] or types["event_msg"] or types["response_item"]: return rows,"codex"
    return rows,"claude"


def text_from_blocks(content: Any) -> str:
    if isinstance(content, str): return content
    out=[]
    if isinstance(content,list):
        for b in content:
            if not isinstance(b,dict): continue
            if b.get("type") in ("text","input_text","output_text") and isinstance(b.get("text"),str): out.append(b["text"])
    return "\n".join(out)


def is_real_claude_user(o: dict) -> bool:
    if o.get("type") != "user": return False
    c=(o.get("message") or {}).get("content")
    if isinstance(c,str): return bool(c.strip())
    if isinstance(c,list):
        has_text=any(isinstance(x,dict) and x.get("type") in ("text","input_text") and str(x.get("text","")).strip() for x in c)
        has_tool_result=any(isinstance(x,dict) and x.get("type")=="tool_result" for x in c)
        return has_text and not has_tool_result
    return False


def plan_candidate(text: str) -> bool:
    if not text or len(text.strip()) < 40:
        return False
    bullets = len(PLAN_BULLET_RE.findall(text))
    seq = len(set(x.lower() for x in PLAN_SEQUENCE_RE.findall(text)))
    has_plan_word = bool(PLAN_WORD_RE.search(text))
    # Explicit enough to be judged against execution. Avoid treating every "I'll check X" as a plan.
    return (has_plan_word and (bullets >= 2 or seq >= 2)) or bullets >= 3 or seq >= 3


def ev(kind: str, idx: int, **kw) -> dict:
    d={"kind":kind,"idx":idx,"text":"","tool":"","tool_class":"","signature":"","target":"","status":"","timestamp":"","args":"","result":""}
    d.update(kw)
    return d


def normalize_codex(rows: list[dict]) -> list[dict]:
    out=[]; pending={}
    for idx,o in enumerate(rows):
        typ=o.get("type"); p=o.get("payload") or {}; ts=o.get("timestamp","")
        if typ=="event_msg":
            st=p.get("type")
            if st=="task_started": out.append(ev("SEGMENT_START",idx,timestamp=ts,text=str(p.get("turn_id",""))))
            elif st=="user_message": out.append(ev("USER_REQUEST",idx,timestamp=ts,text=s(p.get("message") or p.get("text") or p.get("content"),10000)))
            elif st=="task_complete": out.append(ev("COMPLETION",idx,timestamp=ts,text=s(p.get("last_agent_message"),10000)))
            elif st=="turn_aborted": out.append(ev("ABORT",idx,timestamp=ts,text=s(p.get("reason"))))
            elif st=="context_compacted": out.append(ev("CONTEXT_COMPACTION",idx,timestamp=ts))
            elif st=="patch_apply_end":
                ok=p.get("success") is not False
                out.append(ev("EDIT_RESULT",idx,timestamp=ts,tool="apply_patch",tool_class="EDIT",status="ok" if ok else "error",result=s(p.get("stdout") or p.get("stderr"),5000),target=s(list((p.get("changes") or {}).keys()),1000)))
        elif typ=="compacted": out.append(ev("CONTEXT_COMPACTION",idx,timestamp=ts))
        elif typ=="response_item":
            rt=p.get("type")
            if rt=="message":
                role=p.get("role"); txt=text_from_blocks(p.get("content"))
                if role=="assistant" and txt.strip(): out.append(ev("AGENT_TEXT",idx,timestamp=ts,text=txt))
                elif role=="user" and txt.strip(): out.append(ev("USER_REQUEST",idx,timestamp=ts,text=txt))
            elif rt in ("function_call","custom_tool_call"):
                name=p.get("name") or p.get("tool") or "tool"
                args=p.get("arguments") if "arguments" in p else p.get("input")
                if isinstance(args,str):
                    try: args=json.loads(args)
                    except: pass
                tc=classify_tool(name,args); sig=hash_sig(name,args)
                pending[p.get("call_id")]=(name,tc,sig,args)
                out.append(ev("TOOL_CALL",idx,timestamp=ts,tool=name,tool_class=tc,signature=sig,target=extract_target(args),args=s(args)))
            elif rt in ("function_call_output","custom_tool_call_output"):
                cid=p.get("call_id"); name,tc,sig,args=pending.get(cid,("tool","TOOL","",{}))
                result=s(p.get("output") or p.get("result"),10000); status=result_status(result,p.get("status"),tc)
                out.append(ev("TOOL_RESULT",idx,timestamp=ts,tool=name,tool_class=tc,signature=sig,target=extract_target(args),status=status,result=result))
            elif rt=="web_search_call":
                args=p.get("action") or p
                out.append(ev("TOOL_CALL",idx,timestamp=ts,tool="web_search",tool_class="SEARCH",signature=hash_sig("web_search",args),args=s(args),target=s(args.get("query") if isinstance(args,dict) else "")))
            elif rt=="reasoning":
                # often encrypted/empty; ignore as semantic evidence
                pass
    return out


def normalize_claude(rows: list[dict]) -> list[dict]:
    out=[]; pending={}
    for idx,o in enumerate(rows):
        typ=o.get("type"); ts=o.get("timestamp","")
        if is_real_claude_user(o):
            out.append(ev("USER_REQUEST",idx,timestamp=ts,text=text_from_blocks((o.get("message") or {}).get("content"))))
            continue
        if typ=="assistant":
            content=(o.get("message") or {}).get("content")
            if isinstance(content,str):
                if content.strip(): out.append(ev("AGENT_TEXT",idx,timestamp=ts,text=content))
            elif isinstance(content,list):
                for b in content:
                    if not isinstance(b,dict): continue
                    bt=b.get("type")
                    if bt=="text" and str(b.get("text","")).strip(): out.append(ev("AGENT_TEXT",idx,timestamp=ts,text=b.get("text","")))
                    elif bt=="tool_use":
                        name=b.get("name") or "tool"; args=b.get("input") or {}; tc=classify_tool(name,args); sig=hash_sig(name,args)
                        pending[b.get("id")]=(name,tc,sig,args)
                        out.append(ev("TOOL_CALL",idx,timestamp=ts,tool=name,tool_class=tc,signature=sig,target=extract_target(args),args=s(args)))
        elif typ=="user":
            content=(o.get("message") or {}).get("content")
            if isinstance(content,list):
                for b in content:
                    if not isinstance(b,dict) or b.get("type")!="tool_result": continue
                    tid=b.get("tool_use_id"); name,tc,sig,args=pending.get(tid,("tool","TOOL","",{}))
                    result=s(b.get("content"),10000); status=result_status(result,b.get("is_error") is not True,tc)
                    out.append(ev("TOOL_RESULT",idx,timestamp=ts,tool=name,tool_class=tc,signature=sig,target=extract_target(args),status=status,result=result))
        elif typ=="system" and o.get("subtype") in ("compact_boundary","context_compacted"):
            out.append(ev("CONTEXT_COMPACTION",idx,timestamp=ts))
    return out


def normalize_opencode(obj: dict) -> list[dict]:
    out=[]; idx=0
    for m in obj.get("messages",[]):
        info=m.get("info") or {}; role=info.get("role"); ts=str((info.get("time") or {}).get("created", ""))
        parts=m.get("parts") or []
        if role=="user":
            txt="\n".join(str(p.get("text","")) for p in parts if isinstance(p,dict) and p.get("type")=="text" and not p.get("synthetic") and str(p.get("text","")).strip())
            if txt.strip(): out.append(ev("USER_REQUEST",idx,timestamp=ts,text=txt)); idx+=1
        elif role=="assistant":
            for p in parts:
                if not isinstance(p,dict): continue
                pt=p.get("type")
                if pt=="text" and str(p.get("text","")).strip(): out.append(ev("AGENT_TEXT",idx,timestamp=ts,text=p.get("text",""))); idx+=1
                elif pt=="tool":
                    name=p.get("tool") or "tool"; state=p.get("state") or {}; args=state.get("input") or {}; tc=classify_tool(name,args); sig=hash_sig(name,args)
                    out.append(ev("TOOL_CALL",idx,timestamp=ts,tool=name,tool_class=tc,signature=sig,target=extract_target(args),args=s(args))); idx+=1
                    result=s(state.get("output"),10000); status=result_status(result,state.get("status"),tc)
                    out.append(ev("TOOL_RESULT",idx,timestamp=ts,tool=name,tool_class=tc,signature=sig,target=extract_target(args),status=status,result=result)); idx+=1
    return out


def dedupe_adjacent_user(events: list[dict]) -> list[dict]:
    out=[]; last_user=""
    for e in events:
        if e["kind"]=="USER_REQUEST":
            txt=re.sub(r"\s+"," ",e["text"].strip())
            if txt and txt==last_user: continue
            last_user=txt
        out.append(e)
    return out


def segment_events(events: list[dict], harness: str) -> list[list[dict]]:
    segs=[]; cur=[]
    for e in events:
        boundary = (e["kind"]=="SEGMENT_START") if harness=="codex" else (e["kind"]=="USER_REQUEST")
        if boundary and cur:
            # Codex starts a turn before user message; Claude/OpenCode starts at user request.
            segs.append(cur); cur=[]
        cur.append(e)
        if harness=="codex" and e["kind"] in ("COMPLETION","ABORT"):
            segs.append(cur); cur=[]
    if cur: segs.append(cur)
    # Remove metadata-only/no-request-no-tools chunks
    clean=[]
    for sg in segs:
        if any(x["kind"] in ("USER_REQUEST","TOOL_CALL","AGENT_TEXT") for x in sg): clean.append(sg)
    return clean


def summarize_segment(seg: list[dict], seg_id: str, role: str, repo: str, harness: str, source: str) -> tuple[dict,list[dict]]:
    requests=[e["text"] for e in seg if e["kind"]=="USER_REQUEST" and e["text"].strip()]
    agent_texts=[e["text"] for e in seg if e["kind"]=="AGENT_TEXT" and e["text"].strip()]
    completions=[e["text"] for e in seg if e["kind"]=="COMPLETION" and e["text"].strip()]
    plan_texts=[t for t in agent_texts if plan_candidate(t)]
    calls=[e for e in seg if e["kind"]=="TOOL_CALL"]
    results=[e for e in seg if e["kind"] in ("TOOL_RESULT","EDIT_RESULT")]
    counts=Counter(e["tool_class"] for e in calls)
    errors=[e for e in results if e.get("status")=="error"]
    compactions=sum(e["kind"]=="CONTEXT_COMPACTION" for e in seg)

    # State-aware repeat candidates. State version increments on a successful edit.
    state_version=0; seen_at_state={}; repeated=[]; repeat_score=0.0; last_successful_edit_call_index=-1
    tool_call_index=0
    for e in seg:
        if e["kind"] in ("TOOL_RESULT","EDIT_RESULT") and e.get("tool_class")=="EDIT" and e.get("status")=="ok":
            state_version += 1; seen_at_state={}; last_successful_edit_call_index=tool_call_index
        if e["kind"]!="TOOL_CALL": continue
        tool_call_index += 1
        sig=e.get("signature"); tc=e.get("tool_class")
        key=(state_version,sig)
        if sig and key in seen_at_state:
            weight={"TEST":3.0,"COMMAND":2.0,"READ":1.0,"SEARCH":0.75,"TOOL":1.0,"EDIT":0.5,"PLAN_UPDATE":0.1,"SUBAGENT":0.5}.get(tc,0.5)
            repeated.append({"event_idx":e["idx"],"tool_class":tc,"tool":e["tool"],"signature":sig,"state_version":state_version,"weight":weight,"target":e.get("target","")})
            repeat_score += weight
        elif sig:
            seen_at_state[key]=tool_call_index

    # Observable state/progress proxies. Deliberately conservative.
    progress_positions=[]; failed_test_sigs=set()
    call_pos=0
    for e in seg:
        if e["kind"]=="TOOL_CALL": call_pos+=1
        if e["kind"] in ("TOOL_RESULT","EDIT_RESULT"):
            if e.get("tool_class")=="EDIT" and e.get("status")=="ok": progress_positions.append(call_pos)
            if e.get("tool_class")=="TEST":
                if e.get("status")=="error": failed_test_sigs.add(e.get("signature"))
                elif e.get("status")=="ok" and e.get("signature") in failed_test_sigs: progress_positions.append(call_pos)
    if completions: progress_positions.append(max(call_pos,1))
    progress_positions=sorted(set(x for x in progress_positions if x>=0))
    boundaries=[0]+progress_positions+[call_pos]
    max_no_state_change=max((b-a for a,b in zip(boundaries,boundaries[1:])), default=call_pos)
    progress_velocity=(len(progress_positions)/call_pos) if call_pos else 0.0

    final_text=(completions[-1] if completions else (agent_texts[-1] if agent_texts else ""))
    completion_claim=bool(COMPLETION_RE.search(final_text))

    row={
        "segment_id":seg_id,"sample_role":role,"repository":repo,"harness":harness,"source_file":source,
        "user_request":requests[0][:4000] if requests else "",
        "user_request_count":len(requests),"agent_text_count":len(agent_texts),"plan_candidate_count":len(plan_texts),
        "plan_candidate_text":plan_texts[0][:5000] if plan_texts else "",
        "completion_claim":completion_claim,"final_text":final_text[:5000],
        "tool_calls":call_pos,"read_calls":counts["READ"],"search_calls":counts["SEARCH"],"edit_calls":counts["EDIT"],
        "test_calls":counts["TEST"],"command_calls":counts["COMMAND"],"other_tool_calls":counts["TOOL"],
        "tool_errors":len(errors),"context_compactions":compactions,
        "repeat_without_code_state_change":len(repeated),"state_aware_repeat_score":round(repeat_score,3),
        "observable_progress_events":len(progress_positions),"progress_velocity_proxy":round(progress_velocity,6),
        "max_no_observable_state_change_calls":max_no_state_change,
        "aborted":any(e["kind"]=="ABORT" for e in seg),
    }
    return row,repeated


def compact_trace(seg: list[dict], max_events=180) -> list[dict]:
    chosen=[]
    for e in seg:
        if e["kind"] in ("USER_REQUEST","AGENT_TEXT","TOOL_CALL","TOOL_RESULT","EDIT_RESULT","COMPLETION","ABORT","CONTEXT_COMPACTION"):
            item={k:e.get(k,"") for k in ("kind","idx","tool","tool_class","target","status","signature")}
            if e["kind"] in ("USER_REQUEST","AGENT_TEXT","COMPLETION","ABORT"): item["text"]=e.get("text","")[:1200]
            elif e["kind"] in ("TOOL_RESULT","EDIT_RESULT") and e.get("status")=="error": item["result"]=e.get("result","")[:900]
            chosen.append(item)
    if len(chosen)<=max_events: return chosen
    # preserve start/end and uniformly sample middle
    head=chosen[:45]; tail=chosen[-45:]; mid=chosen[45:-45]
    need=max_events-len(head)-len(tail)
    if need>0 and mid:
        step=len(mid)/need
        sample=[mid[min(int(i*step),len(mid)-1)] for i in range(need)]
    else: sample=[]
    return head+sample+tail


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input",type=Path,required=True,help="Directory containing transcripts/ and review_manifest.csv")
    ap.add_argument("--output",type=Path,default=Path("stage3_output"))
    ap.add_argument("--judge-n",type=int,default=80)
    args=ap.parse_args(); args.output.mkdir(parents=True,exist_ok=True)

    manifest_path=args.input/"review_manifest.csv"
    manifest={}
    if manifest_path.exists():
        import pandas as pd
        mf=pd.read_csv(manifest_path)
        for _,r in mf.iterrows(): manifest[Path(str(r["trajectory"])).name]=r.to_dict()

    all_rows=[]; normalized_out=(args.output/"normalized_events.jsonl").open("w",encoding="utf-8")
    repeat_rows=[]; segment_store={}; format_counts=Counter()
    for path in sorted((args.input/"transcripts").glob("*")):
        rows,fmt=load_json_any(path); format_counts[fmt]+=1
        if fmt=="codex": events=normalize_codex(rows); harness="codex"
        elif fmt=="opencode": events=normalize_opencode(rows[0]); harness="opencode"
        else: events=normalize_claude(rows); harness="claude_like"
        events=dedupe_adjacent_user(events)
        segs=segment_events(events,harness)
        base=path.name.split("__")[-1]
        meta=manifest.get(base,{})
        role=str(meta.get("sample_role") or path.name.split("__",1)[0])
        repo=str(meta.get("source.repository") or "__".join(path.name.split("__")[1:-1]))
        for i,sg in enumerate(segs,1):
            sid=f"{path.stem}::seg{i:04d}"
            row,repeats=summarize_segment(sg,sid,role,repo,harness,path.name)
            all_rows.append(row); segment_store[sid]=sg
            for rr in repeats:
                rr.update({"segment_id":sid,"sample_role":role,"repository":repo,"harness":harness,"source_file":path.name}); repeat_rows.append(rr)
            for e in sg:
                normalized_out.write(json.dumps({"segment_id":sid,"sample_role":role,"repository":repo,"harness":harness,"source_file":path.name,**e},ensure_ascii=False)+"\n")
    normalized_out.close()

    import pandas as pd
    df=pd.DataFrame(all_rows)
    if df.empty: raise SystemExit("No segments parsed")
    # Risk score is ranking for semantic review, not a claim of failure.
    df["review_priority_score"]=(
        np_log1p(df["tool_calls"])*0.7 + np_log1p(df["tool_errors"])*0.8 + np_log1p(df["state_aware_repeat_score"])*1.4 +
        np_log1p(df["max_no_observable_state_change_calls"])*1.1 + df["aborted"].astype(int)*0.8
    )
    # prioritize segments where a plan exists for plan-execution judge
    df["plan_review_priority"] = df["review_priority_score"] + (df["plan_candidate_count"]>0).astype(int)*2.5 + df["completion_claim"].astype(int)*0.5
    df.to_csv(args.output/"segments.csv",index=False)
    pd.DataFrame(repeat_rows).to_csv(args.output/"repeat_candidates.csv",index=False)

    # Summary by sample role + harness
    summary=df.groupby(["sample_role","harness"]).agg(
        segments=("segment_id","count"),tool_calls=("tool_calls","sum"),median_tools=("tool_calls","median"),
        plans=("plan_candidate_count",lambda x:int((x>0).sum())),errors=("tool_errors","sum"),
        repeat_score=("state_aware_repeat_score","sum"),median_max_no_state=("max_no_observable_state_change_calls","median")
    ).reset_index()
    summary.to_csv(args.output/"summary_by_group.csv",index=False)

    # Judge bundle: balanced across prefilter role + harness.
    # The role is kept in metadata, but the semantic judge should NOT be shown it.
    eligible_judge=df[df["tool_calls"]>=3].copy()
    selected=[]; selected_ids=set()
    groups=list(eligible_judge.groupby(["sample_role","harness"], dropna=False))
    quota=max(5, math.ceil(args.judge_n/max(len(groups),1)))
    for key,g in groups:
        # Half driven by plan review, half by stagnation/repeat review.
        a=g.sort_values("plan_review_priority",ascending=False).head(math.ceil(quota/2))
        b=g.sort_values("review_priority_score",ascending=False).head(quota)
        for _,r in pd.concat([a,b]).drop_duplicates("segment_id").iterrows():
            if r["segment_id"] in selected_ids: continue
            selected.append(r); selected_ids.add(r["segment_id"])
            if sum(1 for x in selected if (x["sample_role"],x["harness"])==key) >= quota: break
    # Fill remaining slots globally without changing the blind-judge design.
    if len(selected)<args.judge_n:
        for _,r in eligible_judge.sort_values("review_priority_score",ascending=False).iterrows():
            if r["segment_id"] in selected_ids: continue
            selected.append(r); selected_ids.add(r["segment_id"])
            if len(selected)>=args.judge_n: break
    selected=selected[:args.judge_n]
    judge_path=args.output/"judge_input.jsonl"
    with judge_path.open("w",encoding="utf-8") as f:
        for r in selected:
            sid=r["segment_id"]; sg=segment_store[sid]
            item={
                "segment_id":sid,"repository":r["repository"],"harness":r["harness"],
                "user_request":r["user_request"],"plan_candidate":r["plan_candidate_text"],"final_text":r["final_text"],
                "metrics":{k:(None if pd.isna(r[k]) else r[k]) for k in ["tool_calls","tool_errors","state_aware_repeat_score","max_no_observable_state_change_calls","progress_velocity_proxy","aborted"]},
                "trace":compact_trace(sg)
            }
            f.write(json.dumps(item,ensure_ascii=False)+"\n")

    # Top candidate windows CSV for human inspection
    top=df.sort_values("review_priority_score",ascending=False).head(100)
    top.to_csv(args.output/"top_segment_candidates.csv",index=False)

    print("Formats:",dict(format_counts))
    print("Segments:",len(df),"risk:",int((df.sample_role=='risk').sum()),"control:",int((df.sample_role=='control').sum()))
    print("Segments with plan candidate:",int((df.plan_candidate_count>0).sum()))
    print("Judge segments:",len(selected))
    print("\nBy group:\n",summary.to_string(index=False))
    print("\nTop 15 review candidates:")
    cols=["sample_role","repository","harness","tool_calls","tool_errors","state_aware_repeat_score","max_no_observable_state_change_calls","plan_candidate_count","review_priority_score","segment_id"]
    print(top[cols].head(15).to_string(index=False))
    print("\nOutputs:",args.output)


def np_log1p(series):
    import numpy as np
    return np.log1p(series.astype(float).clip(lower=0))

if __name__=="__main__": main()

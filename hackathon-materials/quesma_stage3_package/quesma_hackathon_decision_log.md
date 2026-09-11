# Quesma Hackathon 2026 - Decision Log

Status: living document
Dataset: swe-chat-enhanced-2026-07-05

## 1. Project objective

Build an **Agent Execution Auditor** for real coding-agent trajectories.

The goal is not merely to rank expensive sessions. The system should detect and explain cases where an agent's execution is inefficient or diverges from its own stated intent, and propose runtime interventions that could reduce wasted tokens, tool calls and time without hurting task quality.

## 2. Error classes we currently investigate

### A. Plan-execution mismatch

The agent states an explicit plan, but execution materially diverges from it.

Important: plan changes are not automatically errors.

We specifically care about **unacknowledged plan drift**, including:

- **Omission**: a planned step is never performed.
- **Substitution**: a materially different action replaces a planned step without updating the plan.
- **Premature completion**: the agent claims completion while planned obligations remain unfinished.
- **Stale plan**: new evidence invalidates the plan, but the agent continues following it without adaptation.

Reason for studying it:
A coding agent should either execute its plan or explicitly revise it when new information appears. Silent divergence makes the agent harder to trust and can lead to skipped validation, incomplete implementations and wasted execution.

### B. Failure to make progress / stagnation

The agent performs substantial work but the task state or knowledge state does not meaningfully improve.

We do NOT define progress only as lines of code.

Possible meaningful progress events include:

- discovering a new root cause,
- eliminating a hypothesis,
- identifying the correct file/function,
- successfully changing code,
- moving a failing test toward passing,
- satisfying a planned obligation,
- obtaining new evidence that materially changes the next action.

Reason for studying it:
Long sessions can spend large amounts of tokens and tool calls while making little progress. We want to identify these low-progress windows and understand their causes.

### C. Thrashing

Thrashing is treated as **one possible cause of stagnation**, not as the entire problem.

Examples:

- re-reading the same files without new information,
- repeating identical or near-identical commands,
- rerunning the same failing test without a relevant intervening change,
- edit -> revert -> edit cycles,
- repeatedly pursuing the same failed hypothesis,
- repeated attempts that do not reduce uncertainty.

Critical methodological decision:
**Repetition alone is NOT thrashing.**
Repeated tests/reads/commands can be legitimate. A repetition becomes suspicious when it occurs without a meaningful state change or information gain.

## 3. Core metrics under development

### Plan Fidelity / Plan Execution Score

Measures whether explicit planned obligations were executed, revised or abandoned with acknowledgement.

Candidate outputs:
- planned steps,
- completed steps,
- revised steps,
- silently omitted steps,
- premature completion flag,
- unacknowledged drift count.

### Progress Velocity

Conceptually:

    meaningful progress events / execution activity

Execution activity may be measured by tool calls, turns or elapsed steps.

We should not rely on one denominator only. Tool-call-normalized and turn-normalized versions can both be useful.

### Max Stagnation Window

Longest contiguous execution segment without a meaningful progress event.

This is expected to be especially useful for timeline visualization.

### Thrash Index

Must be state-aware.

Possible components:
- repeated tool/command after no relevant state change,
- repeated failing validation after no relevant edit,
- repeated read/search with high overlap and no new discovery,
- strategy retry count before strategy revision,
- edit/revert cycles.

Do NOT use raw repeated-call count as the final metric.

### Visible progress proxy from metadata

Stage 2 currently uses:
- `initial_attribution.agent_lines`
- number of `files_touched`
- activity from tool calls and assistant messages

This is only a **prefiltering proxy**, not ground truth.

`agent_lines == 0` does NOT prove failure. A task may be analytical, exploratory, or produce valid progress without surviving code attribution.

## 4. Dataset facts relevant to methodology

Full bundle:
- 9,770 trajectories.
- 344 repositories with retained trajectories.
- Harnesses include Claude Code, Codex, OpenCode, Copilot CLI, Cursor, Gemini CLI, Pi and others.

The dataset is highly imbalanced by repository, therefore global raw averages can be misleading.

Decision:
**Normalize screening metrics within repository where possible.**

We also need to consider harness/model differences.

## 5. Stage 1 screening

Stage 1 screens all trajectories using metadata before any expensive LLM analysis.

Purpose:
- avoid sending 18 GB of transcripts to an LLM,
- identify statistically unusual sessions cheaply,
- create a shortlist for deeper analysis.

Important correction discovered after Stage 1:
`checkpoint_token_usage.api_call_count` was accidentally detected as a token field because of its name.

Decision:
Do not blindly combine all fields containing the word `token`.
Also avoid summing `statistics.*` token accounting with `checkpoint_token_usage.*` until their accounting semantics are verified, because they may overlap.

Stage 1 output should therefore be treated as anomaly screening, not a definitive waste ranking.

## 6. Stage 2: risk sample and control group

Stage 2 produced:
- 30 risk sessions,
- 15 control sessions,
- 45 transcripts total.

Risk selection prioritizes:
- high execution activity,
- low visible-output proxy,
- anomalies relative to the repository,
- repository/agent diversity.

### Why we use a control group

A suspicious session alone cannot show what "better" execution would look like.

Controls are selected where possible from:
- the same repository,
- the same agent/harness,
- a roughly comparable activity scale,
- substantially stronger visible-progress proxy.

This makes comparisons more defensible.

Desired presentation pattern:

> In a comparable environment, one trajectory makes consistent progress while another spends a long execution window without improving state.

Control sessions are not assumed to be perfect. They are comparative baselines.

## 7. Important Stage 2 findings

### Finding A: session-level analysis is too coarse

Example from `fogodev/ars-ui`:

Risk trajectory:
- 3,746 tool calls,
- 1 attributed agent line,
- 1 file touched in metadata,
- transcript contains roughly 118 user messages,
- around 100 task-complete events,
- 21 context compactions.

Matched control:
- 1,515 tool calls,
- 291 attributed agent lines,
- 3 files touched,
- only a few user/task-complete events.

Conclusion:
A raw session can contain many distinct tasks/turns. Comparing whole sessions can conflate task count with execution quality.

Decision:
**Stage 3 must segment trajectories into task/turn-level units before computing plan fidelity, progress or thrashing.**

### Finding B: raw repetition count is insufficient

In the inspected `fogodev/ars-ui` pair, the control trajectory contained many exact repeated calls as well.

Conclusion:
Repeated actions can be legitimate, e.g. validation loops.

Decision:
**Thrashing requires repeated activity + no meaningful state change / information gain.**

### Finding C: attribution is useful but not ground truth

Some risk trajectories have `agent_lines = 0` while `total_committed` is large.

Possible interpretations include:
- agent work did not survive,
- commits are attributed elsewhere,
- the task was mainly analytical,
- other actors modified the repo.

Decision:
Use attribution as a candidate-generation signal and an outcome feature, not as the sole definition of success.

## 8. Normalization requirement

The raw transcript formats differ substantially across harnesses.

Examples already observed:
- Codex uses JSONL envelopes such as `session_meta`, `event_msg`, `response_item`, `turn_context`.
- Claude Code uses event records with `user`, `assistant`, tool-use/result content and bookkeeping events.
- OpenCode exports can be structured as a JSON object even when stored under a `.jsonl` filename.

Decision:
Create a harness-specific parser layer that maps raw transcripts into a common event schema.

Proposed normalized events:

- USER_REQUEST
- AGENT_TEXT
- PLAN
- READ
- SEARCH
- EDIT
- COMMAND
- TEST
- TOOL_RESULT
- ERROR
- STATE_CHANGE
- PROGRESS_EVENT
- COMPLETION
- CONTEXT_COMPACTION

Raw transcripts remain the source of truth.

## 9. Stage 3 unit of analysis

Primary unit:
**task/turn segment**, not whole trajectory.

Each segment should contain:
- initiating user request,
- explicit plan if present,
- ordered actions,
- tool results/errors,
- code/state changes,
- validation actions,
- final agent claim,
- next user correction if available.

This gives us a natural basis for:
- plan-execution comparison,
- progress-event detection,
- stagnation windows,
- thrash detection.

## 10. Proposed analysis pipeline

    9,770 trajectories
          |
          v
    metadata screening
          |
          v
    candidate trajectories
          |
          v
    harness-specific transcript normalization
          |
          v
    task/turn segmentation
          |
          v
    deterministic event features
          |
          v
    candidate stagnation / plan-drift windows
          |
          v
    LLM judge on selected windows
          |
          v
    manual review of strongest cases
          |
          v
    dashboard + runtime intervention proposal

LLM calls should be focused on selected windows rather than complete raw bundles whenever possible.

## 11. Relationship to SWE-chat style analysis

Our methodology intentionally follows a similar high-level scientific principle:
- derive deterministic facts first,
- sample/stratify,
- inspect selected trajectories,
- use model judgement only where semantic interpretation is required.

Our added focus is not just descriptive analytics.

We want to identify **execution pathologies** and propose a runtime mechanism that prevents them.

## 12. Product / intervention hypothesis

Possible final product:
**Agent Execution Auditor + runtime guard**

Runtime guard can detect:
- prolonged stagnation,
- repeated failed strategy,
- plan obligations being silently abandoned,
- repeated validation with no relevant intervening change.

Possible intervention:

> You appear to be repeating a strategy without measurable progress. Summarize what changed since the previous attempt, identify the current blocker, and choose a materially different next action or explicitly revise the plan.

Other interventions:
- retry budgets,
- state-aware tool-result cache,
- failure memory,
- plan checkpoint/reconciliation,
- forced strategy revision after stagnation threshold,
- context compression that preserves failed approaches and unresolved obligations.

## 13. Presentation principles

For later presentation we should preserve:

1. **Problem**
   Coding agents can spend many actions without proportional progress or silently drift from their own plans.

2. **Evidence**
   Real public coding-agent trajectories, not synthetic benchmark conversations.

3. **Methodology**
   Cheap global screening -> matched controls -> task-level normalization -> deterministic signals -> LLM semantic judge -> manual validation.

4. **Scientific caution**
   High activity + low code attribution is a risk signal, not proof of failure.
   Repetition is not automatically thrashing.
   Plan changes are not automatically mismatches.

5. **Differentiator**
   We do not merely report inefficient sessions. We try to detect the point at which the agent stops making progress and propose an intervention.

6. **Visualization**
   Timeline showing:
   - plan obligations,
   - progress events,
   - errors,
   - repeated strategies,
   - stagnation windows,
   - plan drift,
   - possible intervention point.

7. **Outcome**
   Estimate avoidable tool calls/tokens/turns after the earliest safe intervention point.

## 14. Open questions

Still to validate empirically:

- exact definition of a semantic progress event,
- how to detect near-duplicate actions across tools,
- whether to use an LLM or deterministic rules for plan extraction,
- how to estimate counterfactual token savings without overstating causality,
- whether matched controls should additionally match task type/complexity,
- how much plan-execution mismatch correlates with stagnation,
- differences between harnesses/models after controlling for repository/task.

## 15. Stage 3 findings and decisions

### Transcript formats confirmed in the Stage 2 review sample

Across 45 sampled transcripts:
- 18 Codex-format transcripts,
- 23 Claude-like transcripts,
- 4 OpenCode object-format transcripts.

Decision: support these three parser families first. Do not generalize to every harness until the core methodology works.

### Task-level segmentation is required

The 45 trajectories normalize into roughly 1.9k task/turn-level segments. Long trajectories often contain many separate user requests and completed tasks.

Decision: trajectory-level `risk/control` labels are candidate-generation labels only. They are NOT ground-truth labels for every segment inside a trajectory.

This means a control trajectory can contain poor segments and a risk trajectory can contain healthy segments.

### Blind semantic judging

Decision: the semantic LLM judge must NOT see:
- `risk/control` label,
- Stage 2 review score,
- deterministic thrash/stagnation risk score.

The judge receives only task context, possible explicit plan, final response, and execution trace. Labels and heuristic scores are joined back after judging.

Reason: avoid confirmation bias and circular evaluation.

### Deterministic metrics are candidate signals

Stage 3 computes observable features before semantic judging:
- tool calls by class,
- command/test errors,
- exact action repetitions without an intervening successful code edit,
- conservative observable progress proxies,
- longest run of calls without an observable state-change proxy,
- explicit-plan candidates,
- completion/abort signals.

These are used to prioritize review. They are not final proof of plan mismatch, stagnation or thrashing.

### Important falsification from controls

Some control segments score highly on raw repetition and execution length.

Decision: do not define failure from activity volume or repeated calls. Semantic evaluation must determine whether the repeated work produced information/state improvement.

### Plan detection

Deterministic plan extraction should only identify explicit plan candidates. It should not decide whether each obligation was fulfilled.

The LLM judge evaluates:
- explicit obligations,
- executed obligations,
- acknowledged revisions,
- silently omitted/substituted obligations,
- premature completion,
- stale-plan behavior.

### Counterfactual savings

Any estimate of avoidable work must be conservative and attached to an `earliest_safe_intervention` point.

Decision: never claim that all actions after the first suspicious repetition were avoidable. The judge should estimate an avoidable fraction and confidence, and we should later validate strong cases manually.

### Initial semantic-judge model

For the first pass, use Claude Sonnet 5 through OpenRouter with structured JSON output. Run a small validation batch first, manually inspect labels, then scale to the balanced sample.

The final methodology should not depend on one judge model. If time/budget permits, adjudicate a subset with a second independent model and report agreement.

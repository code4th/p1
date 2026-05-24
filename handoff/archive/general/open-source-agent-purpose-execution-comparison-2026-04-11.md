# Open-Source Agent Purpose Execution Comparison

Date: 2026-04-11

Scope: open-source AI agent implementations inspected at code level, focusing on how they turn a user request into execution toward task completion.

Baseline question:

- A user gives a request.
- Where is that request stored?
- What context is sent to the model?
- What loop actually executes work?
- How are tool/action results fed back into the next step?
- What counts as "done"?

This note complements the prior `claw-code` inspection and uses the same lens.

## Executive Summary

The implementations cluster into five distinct execution styles.

- `claw-code`: session loop
  - User request + session messages + tool results are fed back into the same conversation until the model stops asking for tools.
- `OpenHands`: event/state-machine loop
  - User request becomes an event, actions and observations flow through an event stream, and completion is a state transition.
- `Letta`: memory-backed step loop
  - The agent reconstructs a strong system prompt from memory, runs `_step()`, appends tool results as messages, and repeats until a stop reason is reached.
- `AutoGen`: orchestration framework loop
  - Single agents do tool iterations, but the more goal-like path is Magentic-One, where an orchestrator maintains task facts/plan/ledger and re-plans on stagnation.
- `SWE-agent`: trajectory loop
  - Problem statement -> prompt templates -> model query -> parse action -> environment observation -> append to history -> repeat until submit/exit.
- `opencode`: session processor loop
  - User messages, reasoning parts, tool calls, tool results, permissions, compaction, and subagents are all managed as session artifacts. This is one of the closest references to a Codex-like coding agent runtime.
- `AutoGPT`: split personality
  - Platform is primarily a graph/block workflow engine.
  - Classic/original AutoGPT is the closer agent loop: task -> propose action -> execute -> record result -> propose next action.

For P2 design, the strongest direct references are:

- `opencode` for session/message/tool-result handling
- `claw-code` for minimal session/tool loop
- `OpenHands` for explicit event/state management
- `SWE-agent` for auditable thought-action-observation traces

## 1. OpenHands

Important nuance:

- The main `OpenHands/OpenHands` repo still contains an inspectable legacy V0 loop.
- The current V1 agentic core has moved to `OpenHands/software-agent-sdk`.
- For that reason, the main repo is useful for understanding the older event-stream design, and the SDK is useful for the current conversation/tool design.

### Main repo: legacy V0

Primary files:

- [/tmp/agent-purpose-study/OpenHands/openhands/server/services/conversation_service.py](/tmp/agent-purpose-study/OpenHands/openhands/server/services/conversation_service.py)
- [/tmp/agent-purpose-study/OpenHands/openhands/server/session/agent_session.py](/tmp/agent-purpose-study/OpenHands/openhands/server/session/agent_session.py)
- [/tmp/agent-purpose-study/OpenHands/openhands/controller/agent_controller.py](/tmp/agent-purpose-study/OpenHands/openhands/controller/agent_controller.py)
- [/tmp/agent-purpose-study/OpenHands/openhands/agenthub/codeact_agent/codeact_agent.py](/tmp/agent-purpose-study/OpenHands/openhands/agenthub/codeact_agent/codeact_agent.py)
- [/tmp/agent-purpose-study/OpenHands/openhands/memory/conversation_memory.py](/tmp/agent-purpose-study/OpenHands/openhands/memory/conversation_memory.py)

How it solves purpose execution:

- User request entry
  - The request is wrapped as `MessageAction(content=...)` and injected into the `EventStream` as a user event.
- Context sent to model
  - System prompt comes from `Agent.get_system_message()` and prompt templates.
  - Context is rebuilt from event history by `ConversationMemory`.
- Core execution loop
  - `AgentController._on_event()` decides whether to step.
  - `AgentController._step()` calls `self.agent.step(self.state)`.
  - Standard behavior is implemented by `CodeActAgent.step()`.
- Result feedback
  - LLM tool call -> `Action`
  - runtime executes action -> emits `Observation`
  - observation returns to `EventStream`
  - `ConversationMemory` rebuilds message history for the next LLM call
- Stop condition
  - Completion is not a separate semantic goal checker.
  - It is effectively "the model emitted `finish`, producing `AgentFinishAction`, and controller moved to `AgentState.FINISHED`".

### Current SDK: V1 style

Primary files:

- [/tmp/agent-purpose-study/software-agent-sdk/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py](/tmp/agent-purpose-study/software-agent-sdk/openhands-sdk/openhands/sdk/conversation/impl/local_conversation.py)
- [/tmp/agent-purpose-study/software-agent-sdk/openhands-sdk/openhands/sdk/agent/agent.py](/tmp/agent-purpose-study/software-agent-sdk/openhands-sdk/openhands/sdk/agent/agent.py)
- [/tmp/agent-purpose-study/software-agent-sdk/openhands-sdk/openhands/sdk/tool/builtins/finish.py](/tmp/agent-purpose-study/software-agent-sdk/openhands-sdk/openhands/sdk/tool/builtins/finish.py)

How it solves purpose execution:

- User request entry
  - `LocalConversation.send_message()` converts text into a user `Message` and emits a `MessageEvent(source="user")`.
- Context sent to model
  - `Agent.step()` calls `prepare_llm_messages(state.events, ...)`.
  - So the event log is the source of LLM context.
- Core execution loop
  - `LocalConversation.run()` repeatedly calls `self.agent.step(...)`.
  - `Agent.step()` either executes pending actions or samples the LLM.
- Result feedback
  - If the LLM returns tool calls, `Agent.step()` converts them to `ActionEvent`s and executes them.
  - Observations are appended as events and become part of `prepare_llm_messages(...)` on the next step.
- Stop condition
  - A single `FinishAction` is the formal completion tool.
  - Message-only replies with user-facing content also set execution status to `FINISHED`.

Bottom line:

- OpenHands is the clearest event/state-machine reference.
- The current SDK is much closer to a reusable kernel than the old repo.

## 2. Letta

Primary files:

- [/tmp/agent-purpose-study/letta/letta/server/rest_api/routers/v1/agents.py](/tmp/agent-purpose-study/letta/letta/server/rest_api/routers/v1/agents.py)
- [/tmp/agent-purpose-study/letta/letta/agents/letta_agent_v3.py](/tmp/agent-purpose-study/letta/letta/agents/letta_agent_v3.py)
- [/tmp/agent-purpose-study/letta/letta/prompts/prompt_generator.py](/tmp/agent-purpose-study/letta/letta/prompts/prompt_generator.py)
- [/tmp/agent-purpose-study/letta/letta/services/tool_executor/tool_execution_manager.py](/tmp/agent-purpose-study/letta/letta/services/tool_executor/tool_execution_manager.py)

How it solves purpose execution:

- User request entry
  - API accepts `LettaRequest.messages`.
  - These are normalized into internal `Message` objects.
- Context sent to model
  - Strong system prompt generated from:
    - base system template
    - compiled memory
    - memory metadata
    - optional client skills
  - In-context messages are restored from stored message IDs.
- Core execution loop
  - Outer loop is `step()`.
  - One LLM turn is `_step()`.
  - `_step()` handles request build, LLM call, tool parsing, tool execution, persistence, and continuation decision.
- Result feedback
  - Tool results become internal `Message(role="tool", ...)`.
  - Those are appended to the in-context message list and converted back into provider-format messages on the next call.
- Stop condition
  - Controlled by `stop_reason`.
  - Normal completion is effectively "no more tool work is required" or a terminal tool/end-turn rule is hit.

Bottom line:

- Letta is the strongest memory/system-prompt reconstruction reference.
- It is less about raw coding-runtime control than `OpenHands` or `opencode`, but stronger on persistent state and memory-conditioned execution.

## 3. AutoGen

Primary files:

- [/tmp/agent-purpose-study/autogen/python/packages/magentic-one-cli/src/magentic_one_cli/_m1.py](/tmp/agent-purpose-study/autogen/python/packages/magentic-one-cli/src/magentic_one_cli/_m1.py)
- [/tmp/agent-purpose-study/autogen/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py](/tmp/agent-purpose-study/autogen/python/packages/autogen-agentchat/src/autogen_agentchat/agents/_assistant_agent.py)
- [/tmp/agent-purpose-study/autogen/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_magentic_one/_magentic_one_orchestrator.py](/tmp/agent-purpose-study/autogen/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/_magentic_one/_magentic_one_orchestrator.py)

How it solves purpose execution:

- User request entry
  - Tasks are normalized into `TextMessage(source="user")`.
  - Single-agent context stores them in `ChatCompletionContext`.
  - Magentic-One also stores them in orchestrator state such as `_task` and `_message_thread`.
- Context sent to model
  - Single agent:
    - system messages
    - model context history
    - optional memory
    - tools/handoffs
  - Magentic-One orchestrator:
    - task
    - team description
    - facts
    - plan
    - message thread
- Core execution loop
  - Single agent:
    - `AssistantAgent.on_messages_stream()` and `_process_model_result()`
  - Goal-oriented team path:
    - `MagenticOneOrchestrator._orchestrate_step()`
- Result feedback
  - Tool execution results are inserted into model context as `FunctionExecutionResultMessage`.
  - Team responses are appended to the orchestrator thread and used in the next orchestration step.
- Stop condition
  - Single agent stops when text is returned, handoff occurs, or tool iteration cap is reached.
  - Team stops on `TerminationCondition` or max turns.
  - Magentic-One ends when the progress ledger says `is_request_satisfied == True`; stagnation triggers re-planning instead of immediate stop.

Bottom line:

- AutoGen is best viewed as an orchestration framework, not one fixed agent runtime.
- Magentic-One is the most relevant part if the question is "how does it explicitly reason about goal completion and re-planning?"

## 4. SWE-agent

Primary files:

- [/tmp/agent-purpose-study/SWE-agent/sweagent/run/run_single.py](/tmp/agent-purpose-study/SWE-agent/sweagent/run/run_single.py)
- [/tmp/agent-purpose-study/SWE-agent/sweagent/agent/agents.py](/tmp/agent-purpose-study/SWE-agent/sweagent/agent/agents.py)
- [/tmp/agent-purpose-study/SWE-agent/sweagent/agent/models.py](/tmp/agent-purpose-study/SWE-agent/sweagent/agent/models.py)
- [/tmp/agent-purpose-study/SWE-agent/config/default.yaml](/tmp/agent-purpose-study/SWE-agent/config/default.yaml)

How it solves purpose execution:

- User request entry
  - Entry is `problem_statement`.
  - This is usually an issue text or task description, stored as structured problem statement config and then inside agent state.
- Context sent to model
  - `setup()` writes:
    - system template
    - instance template
    - problem statement
    - command docs
    - repo/tool state
  - Final model input is processed history.
- Core execution loop
  - Outer loop: `DefaultAgent.run()`
  - Per-step loop: `step()` -> `forward_with_handling()` -> `forward()` -> `handle_action()`
- Result feedback
  - Model output is parsed into action.
  - Environment executes action and yields observation.
  - `add_step_to_history()` appends assistant output plus observation back into history.
- Stop condition
  - `step.done == True`
  - Usually because of `submit`, `exit`, or autosubmit on failure/limit.
  - There is no intrinsic "the issue is definitely solved" validator in the main loop.

Bottom line:

- SWE-agent is the cleanest thought-action-observation reference.
- It is especially useful if P2 needs more explicit trace artifacts and error/retry handling around coding actions.

## 5. AutoGPT

Important nuance:

- `AutoGPT` today is two different things:
  - `autogpt_platform`: a block/graph automation platform
  - `classic/original_autogpt`: the older autonomous agent loop
- For "how does a single agent pursue a goal step by step", `classic/original_autogpt` is the relevant code.

### Classic / original AutoGPT

Primary files:

- [/tmp/agent-purpose-study/AutoGPT/classic/original_autogpt/autogpt/app/agent_protocol_server.py](/tmp/agent-purpose-study/AutoGPT/classic/original_autogpt/autogpt/app/agent_protocol_server.py)
- [/tmp/agent-purpose-study/AutoGPT/classic/original_autogpt/autogpt/agent_factory/default_factory.py](/tmp/agent-purpose-study/AutoGPT/classic/original_autogpt/autogpt/agent_factory/default_factory.py)
- [/tmp/agent-purpose-study/AutoGPT/classic/direct_benchmark/direct_benchmark/runner.py](/tmp/agent-purpose-study/AutoGPT/classic/direct_benchmark/direct_benchmark/runner.py)

How it solves purpose execution:

- User request entry
  - `create_task()` stores `task.input`.
  - An agent instance is created from that task text.
- Context sent to model
  - Agent state includes task, AI profile, directives, config, and history.
  - The exact prompt building is deeper in the classic stack, but the execution API clearly revolves around stored task + agent state.
- Core execution loop
  - Protocol server version:
    - `execute_step()` restores agent state, optionally executes prior proposal, then calls `agent.propose_action()`
  - Benchmark loop:
    - `proposal = await agent.propose_action()`
    - if `finish`, stop
    - else `result = await agent.execute(proposal)`
    - repeat
- Result feedback
  - Result of executed action is registered into agent event history/state, then the next proposal is generated from the updated agent state.
- Stop condition
  - Explicit `finish` command is the main completion path.
  - The benchmark runner treats `finish` as normal completion.

### Platform

What matters for comparison:

- Platform is primarily graph/block execution, not a single conversational agent loop.
- It is useful if the goal is workflow automation or orchestrator composition, but it is not the best direct reference for a Codex-like agent runtime.

Bottom line:

- AutoGPT classic is useful as historical reference for `propose_action -> execute -> repeat until finish`.
- AutoGPT platform is a weaker reference for P2 because it is workflow/block-centric rather than session/tool-loop-centric.

## 6. opencode

Primary files:

- [/tmp/agent-purpose-study/opencode/packages/opencode/src/session/prompt.ts](/tmp/agent-purpose-study/opencode/packages/opencode/src/session/prompt.ts)
- [/tmp/agent-purpose-study/opencode/packages/opencode/src/session/processor.ts](/tmp/agent-purpose-study/opencode/packages/opencode/src/session/processor.ts)
- [/tmp/agent-purpose-study/opencode/packages/opencode/src/session/system.ts](/tmp/agent-purpose-study/opencode/packages/opencode/src/session/system.ts)
- [/tmp/agent-purpose-study/opencode/packages/opencode/src/session/llm.ts](/tmp/agent-purpose-study/opencode/packages/opencode/src/session/llm.ts)
- [/tmp/agent-purpose-study/opencode/packages/opencode/src/agent/agent.ts](/tmp/agent-purpose-study/opencode/packages/opencode/src/agent/agent.ts)

How it solves purpose execution:

- User request entry
  - User messages are stored as session messages.
  - The main loop in `session/prompt.ts` walks message history, finds the last user message, creates a new assistant message, and continues execution from there.
- Context sent to model
  - `session/system.ts` builds:
    - provider-specific system prompt
    - environment block
    - skills listing
  - `session/prompt.ts` converts session history into `modelMsgs`, resolves tools, and sends `[...modelMsgs]` plus system sections to the LLM.
- Core execution loop
  - The loop in `session/prompt.ts` repeatedly:
    - inspects session history
    - handles subtask/compaction if present
    - creates assistant message
    - resolves tools
    - calls `SessionProcessor.create(...).process(...)`
  - `SessionProcessor.process(...)` is the event-driven stream consumer for the actual LLM output.
- Result feedback
  - `session/llm.ts` calls `streamText(...)`.
  - `SessionProcessor` consumes events such as:
    - `reasoning-start`
    - `tool-call`
    - `tool-result`
    - `tool-error`
  - These are persisted as message parts in the session.
  - The next loop turn converts the full session back into model messages.
- Stop condition
  - The loop exits when the last assistant message has a finish reason other than `tool-calls`, there are no residual tool calls, and it is logically after the last user message.
  - If overflow occurs, compaction is inserted instead of hard failure.

Bottom line:

- `opencode` is one of the strongest references for a Codex-like runtime.
- It combines:
  - session persistence
  - tool streaming
  - reasoning/tool/message parts
  - agent modes
  - compaction
  - subtask support
- Compared to `claw-code`, it is heavier but much closer to the kind of runtime kernel P2 has been circling around.

## Comparison Table

| Project | Main unit of execution | Request representation | Feedback path | Completion rule |
| --- | --- | --- | --- | --- |
| claw-code | session turn | user message in session | tool result back into session messages | model stops asking for tools |
| OpenHands | event/state machine | `MessageAction` / `MessageEvent` | observation back into event stream | `FINISHED` state / `FinishAction` |
| Letta | memory-backed step | internal `Message` list | tool result as `role=tool` message | `stop_reason` |
| AutoGen | agent/team orchestration | `TextMessage` / task thread | tool results into model context or team thread | termination condition / ledger satisfied |
| SWE-agent | trajectory step | `problem_statement` + templates | observation appended to history | `submit` / `exit` / `done` |
| AutoGPT classic | propose-execute step | `task.input` | execution result into agent state/history | `finish` command |
| opencode | session processor loop | session user message | tool/result/reasoning parts persisted into session | assistant finish reason, no pending tool loop |

## Implications for P2

Three design choices are especially clear after comparing these systems.

### 1. Pick one runtime abstraction

The biggest mistake would be mixing all styles at once.

- If P2 wants minimality, choose `claw-code` / `opencode` style:
  - session + tool results + repeat
- If P2 wants explicit state transitions, choose `OpenHands` style:
  - event stream + terminal state machine
- If P2 wants explicit audit trajectory, choose `SWE-agent` style:
  - thought/action/observation log

Mixing session loop, generation system, validation loop, recursive frames, and governance all at once is exactly what makes the design hard to control.

### 2. `opencode` is probably the most relevant new reference

If the question is:

- "What open-source system is closest to a modern coding agent with session history, tools, permissions, subagents, and compaction?"

the answer from this pass is:

- `opencode`

### 3. Goal completion is often much simpler than it looks

Most of these systems do not have a rich semantic "goal manager".
They usually stop because one of the following happens:

- model emits `finish`
- model emits no more tool work
- explicit termination condition fires
- outer runner hits submit/exit

That suggests P2 should not over-design goal semantics early. A minimal kernel can treat "done" as a pragmatic loop completion signal and add richer self-improvement criteria later.

## Recommended reference stack for P2

If we want the smallest useful hybrid:

- `claw-code`: minimal session/tool loop
- `opencode`: session processor, tool/result streaming, compaction, agent modes
- `OpenHands`: event/state discipline if explicit execution states are needed
- `SWE-agent`: clean audit trajectory for coding/validation loops

If we want one strongest additional repo to study next in depth:

- `opencode`

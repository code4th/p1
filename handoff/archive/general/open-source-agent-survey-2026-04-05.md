# Open-Source Agent Survey

Date: 2026-04-05

Scope: open-source agent codebases that are closest to a long-running, self-improving, tool-using agent loop.
Sources used: official GitHub repositories and their README / repo metadata only.

## Criteria used

- Can think, plan, act, and reflect in a loop.
- Can run code or use tools.
- Has some notion of state, memory, or long-lived continuity.
- Is not just a toy demo.
- Is close to the desired P1 shape: self-directed, conservative, auditable, and extensible.

## Shortlist

### OpenHands

- Repository: `OpenHands/OpenHands`
- What it is: AI-driven development platform with a software agent SDK, CLI, local GUI, cloud deployment, and enterprise mode.
- Observable strengths:
  - SDK is described as the engine behind the rest of the system.
  - CLI and Local GUI are first-class, with REST API support for the local GUI.
  - Intended for coding / tool use / agentic development workflows.
- Observable weakness for this project:
  - It is primarily an AI-driven development platform, not an explicit self-improving memory substrate.
  - The repo README emphasizes agentic development and product surfaces more than internal learning/governance semantics.

### Letta

- Repository: `letta-ai/letta`
- What it is: a platform for building stateful agents with advanced memory that can learn and self-improve over time.
- Observable strengths:
  - Strong memory/state story.
  - Explicitly frames the agent as stateful and self-improving.
  - Offers local CLI plus API/SDK.
- Observable weakness for this project:
  - The public README emphasizes memory and statefulness more than the tool-execution / software-engineering loop.
  - It is less obviously a hands-on coding/runtime environment than OpenHands.

### AutoGen

- Repository: `microsoft/autogen`
- What it is: a programming framework for agentic AI with layered APIs.
- Observable strengths:
  - Clear layering: Core API, AgentChat API, Extensions API.
  - Explicit support for message passing, event-driven agents, local/distributed runtime, and code execution.
  - Good for composing multi-agent workflows.
- Observable weakness for this project:
  - It is a framework and orchestration layer, not a full self-directed P1-shaped system by itself.
  - The repo README centers on multi-agent workflow construction, not one long-lived self-improving agent with a single identity.

### SWE-agent

- Repository: `SWE-agent/SWE-agent`
- What it is: a GitHub-issue-driven software agent that tries to automatically fix issues with an LM of your choice.
- Observable strengths:
  - Very close to the software-fixing loop.
  - Useful as a benchmark for code-editing and repo repair.
- Observable weakness for this project:
  - Narrower than the desired P1 loop.
  - Focused on issue fixing rather than broader autonomous self-improvement, governance, and long-lived state.

### AutoGPT

- Repository: `Significant-Gravitas/AutoGPT`
- What it is: a platform for continuous AI agents that automate complex workflows.
- Observable strengths:
  - Explicitly continuous agents.
  - Platform includes frontend, server, workflow blocks, deployment controls, monitoring.
- Observable weakness for this project:
  - Public-facing README reads more like a workflow automation platform than a compact self-improving core.
  - The architecture appears heavier and more platform-oriented than the minimal core-loop shape desired for P1.

## Best fit for the current P1 goal

If we pick one codebase as the closest reference for the current P1 objective, the best single fit is:

- **OpenHands** for the action / tool / code-execution loop

Why:

- It is explicitly built around AI-driven development.
- It ships an SDK, CLI, local GUI, and REST API.
- It is designed for local agent execution and scalable cloud execution.
- It is closer to a practical “think-plan-act” loop than a pure orchestration library.

## Important nuance

For the P1 goals described in this workspace, a hybrid reference is likely better than any single repo:

- Use **OpenHands** as the action/runtime reference.
- Use **Letta** as the memory/statefulness reference.
- Use **AutoGen** as the orchestration / multi-agent composition reference.
- Use **SWE-agent** as the code-fixing benchmark reference.

That said, if one repo must be chosen as the closest operational match to “a Codex-like agent that can actually do work,” OpenHands is the closest fit from the visible documentation.

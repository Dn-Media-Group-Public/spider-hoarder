# Project Rules

## Development workflow

### Plan-first rule

All non-trivial work requires a written plan before any code is touched. Exceptions: bugfixes and very small UI changes.

### Plan-writing session

Triggered when the user asks to build or design anything. Requests like "build X", "implement X", or "let's add X" are plan-writing requests — not requests to write code immediately. The agent:

1. Asks clarifying questions and raises architectural concerns — performance, modularization, API signatures, data model, component boundaries, tradeoffs — **without assuming answers**. Proposes options; waits for user input. When reading context to inform questions, prefer docs (`docs/architecture/`, `docs/api/`, `docs/implemented-plans/`, `docs/plans/`) over source code; read source code only if docs do not answer the specific detail.
2. Continues the discussion, one topic at a time, until the user has explicitly agreed on every key decision. The agent must not proceed to writing while open questions remain.
3. Only after the user signals agreement (e.g. "looks good", "yes", "write the plan", "let's build it", "do it"), writes the plan as `docs/plans/<feature-name>.md`.

**The agent never makes unilateral architecture decisions. It suggests; the user decides.**

The plan document is the human's decision gate. After the agent writes it, the session ends. The user reviews, commits the plan, and starts a new session to implement it.

**The agent never commits to git. Only the user does.**

### Plan format

Plans follow this structure (adapt as needed):

- **Problem / Goal** — why this is being built
- **Design** — key decisions, data model, API contracts, component structure
- **Implementation order** — numbered steps
- **Files to change** — table of file → what changes
- **Docs to update** — which `docs/architecture/` or `docs/api/` files need updating

### Implementation session

When implementing a plan:

1. Read the plan in full before starting.
2. Implement all code changes.
3. Update all affected architecture and API docs under `docs/`. In each updated doc section, add a "See also" link to the implemented plan (e.g. `See also: [Plan: Feature Name](../implemented-plans/<name>.md)`). Also update `docs/implemented-plans/index.md` to link the newly moved plan.
4. Move the plan file from `docs/plans/<name>.md` to `docs/implemented-plans/<name>.md`.

The user then commits everything — code, moved plan, and doc updates — as a single unit.


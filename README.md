# Iterize IDE

> Most AI coding tools optimise the next prompt. Iterize maintains the integrity of the whole project — especially complex, multi-module ones — across many prompts and amendments.

---

## What Iterize Is

Iterize is an open source, self-contained Python development environment that combines a conventional code editor with structured AI-assisted planning, building, review and verification. It carries the project's intent from planning through implementation to review and verification, so iterations stay coherent and consistent.

The central idea is that software development should not be treated as a sequence of disconnected prompts. A project begins with an explicit mission, develops through controlled iterations, and ends with a comparison between what was intended and what the current code actually does. The same specification connects those stages throughout.

Iterize has three principal working centres:

- **Main page** — planning and editing. Where the project is discussed, its specification is formalised, files are edited, and programs are run.
- **Agents Build tab** — implementation. A Mission agent coordinates a Builder and Reviewer while retaining the larger project context.
- **Verify tab** — final comparison. Reconstructs the intended project from the mission and later scope changes, then checks the current source against it.

The application is written primarily with Python's standard library and Tkinter. Projects remain ordinary folders containing ordinary source files; Iterize adds structured project records alongside them rather than locking the code into a proprietary format.

---

## Workflow

Plan and discuss, then commit a mission spec. The mission agent coordinates work from that spec. The builder applies targeted changes. The reviewer judges those changes in isolation — if repair is needed, control returns to the mission agent; if accepted, the changes are promoted to the active file. Verification then runs an independent end-to-end comparison between the committed spec and the actual project state.

The division is deliberate. Planning defines what should exist. Building changes code. Review judges the specific delta. Verification sees the whole — not the last change, but whether the project as it stands matches what was intended from the start.

---

## Major Areas

| Area | Purpose |
|---|---|
| **Main editor** | File editing, tab management, project navigation, running code with output inspection. |
| **Planning chat** | Discuss ideas with the language model and refine them into the canonical mission specification. |
| **Designations** | Break the codebase into stable identifiers — project, modules, classes, and functions are each tagged so work can be aimed at precise targets. |
| **Agents Build tab** | Coordinate the Mission, Builder, and Reviewer roles, staging changes before approval. |
| **Live View** | Show the Builder's evolving source in real time with newly changed code highlighted. |
| **Knowledge Graph** | Represent project structure, imports, function calls, and functional mappings as both a visual graph and machine-readable memory. |
| **Verify tab** | Aggregate intended behaviour from the spec and compare it against the current registered project source. |
| **Evolution history** | Record decisions, attempts, failures, repairs, and scope changes, with persistent undo and redo. |

---

## How to Use Iterize

### 1. Open or Create a Project

Use **File → New Project** or **File → Open Project**. The project tree appears on the left. Opening and saving Python files registers their structure in `designations.json`, which powers designations, the Structure panel, the Knowledge Graph, and targeted agent work.

The editor supports Python, JavaScript, CSS, HTML and SQL highlighting. Python and HTML files can be run or previewed from the Run controls. Tabs can be closed with their visible cross, middle-click, or the right-click tab menu.

### 2. Configure the Language Model

Open **Tools → API Settings**.

Choose a provider preset or a custom OpenAI-compatible endpoint. Presets are supplied for Anthropic, OpenAI, Mistral, DeepSeek, Qwen, Z.ai and a local/custom server. The preset fills in a suitable endpoint, default model and conventional environment-variable name.

The API key field contains the **name of an environment variable**, not the secret key itself — for example, `OPENAI_API_KEY`. The actual key remains in the operating-system environment. Plaintext keys are deliberately excluded from project files and agent specifications.

Multiple model names can share the same provider and endpoint, making different models available to different agent roles. The Verify tab uses the same default model as the main page.

### 3. Discuss the Project on the Main Page

Open the chat pane using the narrow chat control on the right side of the main window. Use it as a planning conversation: describe the program, ask questions, explore alternatives and refine the scope.

**Cumulative mode** preserves a running sequence of instruction blocks — useful when a project grows in stages, as earlier confirmed requirements remain in view while new ones are added. Without it, the current instruction is treated as the immediate request.

When the discussion is mature, use **Draft / update from discussion**. The model converts the conversation into a structured Markdown specification containing the mission, intended outcome, current scope, stable requirements, constraints, known-good areas, remaining work and verification criteria.

Review and edit the draft directly, then press **Confirm → Mission + Verify**. This writes the single canonical project contract:

```
agents/instructions_mission.md
```

There is only one mission document. It can be edited from Planning, Tools → Instructions Setup, or the Mission side of the Agents workspace. Each confirmed scope change is recorded in the evolution log.

### 4. Prepare the Agents Workspace

Open **Orchestrate Agents → Open Orchestrator**.

In the Agents roster, add or update the available agents and select their provider and model. Assign one agent to each role:

- **Mission**
- **Builder**
- **Reviewer**

Only Mission receives project-wide files through the Project files list. This gives the coordinator a broad view of the system while keeping Builder focused on the module or code segment it is currently changing.

Choose how the build begins:

- **Import Active File** — copies the active editor file into the Agents workspace as an in-memory product.
- **From Scratch** — starts with an empty product and allows Builder to create a new file.
- **Delete** — discards the staged Agents product without changing the file on the main page.

The main source file is not altered while the agents work. Changes remain staged until approval.

### 5. Run the Agent Cycle

Press **Run**. Before code or project context is sent, Iterize displays an API confirmation showing each role's provider, model, endpoint and key-environment name.

The normal cycle:

1. Mission reads the canonical specification, relevant project context, previous reviewer feedback and known evolution history.
2. Mission updates the objectives and delegates a precise instruction to Builder.
3. Builder changes the relevant code and runs compile, execution and optional test-command gates.
4. Reviewer compares Builder's result with Mission's objectives and reports what is sound, what remains and whether the result is satisfactory.
5. If necessary, the feedback returns to Mission for another bounded repair cycle.

Mission maintains continuing conversational context in `mission_session.json`. Builder and Reviewer are intentionally refreshed between tasks so they concentrate on the current delegation rather than accumulating unrelated context.

Each in-tab workspace has a small **+** button near its heading that opens a larger popout. Only Builder's popout has an approval control; Reviewer's popout is report-only. The Mission token counter gives an approximate view of its staged or retained context.

### 6. Watch Changes in Live View

Open **Live View** before or during a run. It displays the current staged source. When a new version arrives, inserted or replaced lines are highlighted in green and the view scrolls to the first changed area.

Live View is observational — it shows the product being revised but does not itself write changes to the project.

### 7. Approve the Result

Review the Builder output, Reviewer report, gate status and Live View. When satisfied, press **Approve → Active File**.

Approval writes the staged product back to the exact project file that was imported. It refuses frozen files and detects base drift — the case where the original file changed after being imported into Agents — asking before overwriting newer work. A failed gate requires an explicit override.

For a from-scratch product with no existing target file, approval opens a new unsaved editor tab so the user can choose its filename.

### 8. Perform Final Verification

Open the **Verify tab** and press **Build Spec + Verify Code**.

Verify collects:

- `agents/instructions_mission.md`
- later scope changes recorded in `evolution.json`
- the current source of all registered, non-deleted project modules

Using the default model, it first aggregates these inputs into one concise intended specification, then checks every extracted requirement against the supplied source and classifies it as:

- **Correct** — the required behaviour is evidenced in the code
- **Incorrect** — the behaviour is absent or contradicted
- **Uncertain** — runtime evidence or source outside the supplied context would be required

The Verify tab displays the overall percentage, counts, requirement-level evidence and a summary. It saves the aggregated specification and timestamped reports under `agents/verification/`.

### 9. Repair Runtime Errors

Run a Python file normally. If it produces a traceback, use **Run → Send Last Error to Agents** or **Fix Error → Agents**.

Iterize extracts the last traceback, finds the deepest project frame, imports the implicated file, parses the line number to identify the smallest enclosing class or function, stages the traceback as evidence and focuses the repair cycle on that confined area. A traceback in one function should not cause the rest of a working file to be rewritten.

---

## How the Different Parts Work

### The Main IDE and Editor

The main window combines a project tree, whole-project Structure sidebar, tabbed editor, console and collapsible planning chat.

The console uses a single managed subprocess. While a program is running, console input is sent to that program's standard input; while idle, the same line can run shell commands. Output is returned to the Tkinter interface through a queue so background work does not update widgets directly.

The editor has three aligned columns:

1. Ordinary line numbers
2. The designation assigned to each line
3. Editable source code

The Structure sidebar shows the full project hierarchy beginning with `P1`, followed by each module, class and function. Selecting an item opens its module and navigates to the corresponding line. Structure checkboxes control how much detail is included in Markdown snapshots.

### Planning and the Canonical Mission

Planning is deliberately separate from building. The main chat is where the user and model decide what the program should become. The resulting mission document is the durable interface between human intent and automated work.

When cumulative mode is used, newly supplied properties extend the running scope instead of displacing earlier requirements. When a draft is confirmed, the previous and new mission are compared and the change is written into `evolution.json` as a project-level scope change.

This makes the mission both a before-state specification for Verify and a live instruction source for Mission during development.

### The Three Agent Roles

#### Mission — Coordinator and Persistent Project Memory

Mission is Agent 1. It understands the overall project, preserves still-valid requirements and known-good areas, maintains checkable objectives and decides what Builder should do next.

Mission is the only agent with a persistent API conversation. It can receive project-wide context files and user messages. When the user asks for a change, Mission updates the canonical scope where appropriate, identifies the required work and delegates a focused instruction.

#### Builder — Focused Implementation

Builder is Agent 2. It works on the current product rather than maintaining the full project conversation.

For a new file, Builder may return a complete source. For an imported existing file, Builder works through a change set — replacing functions with complete definitions, inserting additions at the correct scope, and adding required imports without discarding unrelated code.

Large files are not sent or replaced wholesale. Builder first plans targets from the module skeleton, receives only the relevant code segments, graph memory and caller code, then applies changes back to the complete in-memory source using AST-aware splicing. The candidate must pass syntax compilation and can also be checked with designation harnesses, a user-supplied test command and a short run gate.

#### Reviewer — Independent Assessment

Reviewer is Agent 3. It receives the objectives, Builder summary, changed code or unified diff, and gate evidence. It checks whether the build conforms to Mission's instruction and reports a verdict back to Mission.

The report can include an estimated working percentage, evidence for individual code areas, sound sections and confined remaining repairs. Reviewer is intentionally lighter-weight and does not maintain a continuous conversation.

When Reviewer identifies a code chain as sound with a score of at least 90, that chain can be protected from later Builder changes. Subsequent iterations concentrate on the remaining problematic areas instead of repeatedly rewriting successful code.

### Designation Mechanics

Designations provide stable addresses for code:

| Prefix | Refers to |
|---|---|
| `P1` | The project |
| `M1`, `M2`, … | Modules |
| `C1`, `C2`, … | Classes within a module |
| `F1`, `F2`, … | Functions or methods within their scope |

A complete designation can look like `P1M2C1F4(a)`, where the revision suffix records that the entity changed while preserving its identity. Deleted numbers are retired rather than reused. Restored entities regain their identity, and every structural action is recorded in the designation log.

Every source line receives its deepest applicable designation. This line map supports precise navigation, targeted changes, harness selection, review reporting and traceback repair.

### Targeted Iteration and Review Mechanics

Iterize treats existing code as a collection of individually addressable regions rather than one replaceable blob.

When a change is requested, Builder receives the relevant designation, function body, structural context, graph relationships and known failure history. It returns complete replacements for only the functions that need to change. AST parsing confirms that a replacement matches the intended chain before it is spliced into the full source.

Reviewer then scores the result. High-confidence sound chains are protected. Remaining issues are returned as confined repair instructions. Evolution history supplies deduplicated previous failure reasons so later attempts do not repeat the same dead ends.

This is why review is not merely a final pass/fail step: it progressively separates established working code from the smaller area that still requires iteration.

### Knowledge Graph Mechanics

The Knowledge Graph is both a visual map and an operational memory system.

**Where nodes come from** — the graph reads `designations.json`: each module becomes a large outer circle; classes become circles inside their module; methods become smaller circles inside their class; module-level functions sit inside the module but outside classes.

**How connections are discovered** — Iterize parses project Python files into abstract syntax trees, creating dashed module-to-module import links and solid function/method call links. The resolver recognises direct calls, `self.method()` calls, imported symbols, aliased module calls, local variables holding class instances, and chained calls such as `self.manager.run()`. External library calls are not treated as internal project nodes.

**How the graph becomes memory** — `KGMemory` turns graph relationships into compact context cards. For a target function it can recall its designation, signature and description; which project functions call it; which project functions it calls; functional mapping rows that mention it; and the actual source segments of its callers, even when they are in other modules. Before Builder changes a function, it can therefore inspect the seams that must continue working.

**Layout and interaction** — the initial nested-circle layout is deterministic. Users can pan by dragging empty canvas space, zoom with the mouse wheel, drag individual nodes or entire module groups, reset to the computed layout, and save a PNG snapshot (or EPS if Pillow is unavailable). Node positions are stored in `kg_layout.json`.

**Functional mappings** — the lower graph panel records human-readable relationships between functionality and code locations. These rows can be edited manually or generated through the configured API, and become additional context for agent work.

### Verify Mechanics

Verify is intentionally independent from the Builder's internal reasoning. It does not ask whether the agent said it completed the task — it asks whether the current registered source provides evidence for the accumulated intended behaviour.

It reconstructs the intended end state from the original mission plus recorded scope changes, sends that specification and current project source to the default model, normalises the returned findings and calculates the score as the proportion of requirements marked correct. If source has to be truncated because of the context limit, requirements dependent on unseen code are reported as uncertain rather than falsely passed or failed.

### Evolution History, Undo and Recovery

`agents/evolution.json` is the development record. It stores scope changes, delegations, code changes, gate results, review outcomes and failure reasons.

Its history is mirrored in `agents/evolution_history.json`, with a cursor over recent states. **Undo evolution** and **Redo evolution** restore complete prior states. Writes are atomic: new content is written to a temporary file and then replaces the old file. Damaged JSON is preserved under a backup name rather than silently overwritten.

### Versions, Originals and Iterations

**Mark as Original** archives a file and freezes it as read-only — a protected reference point. **Unfreeze** returns it to editable status. **Save as Iteration** creates a timestamped iteration and records the corresponding designation state.

The editor can optionally make a Git checkpoint on save. These mechanisms complement agent staging: the Agents workspace protects the current file until approval, while originals, iterations and Git provide longer-term recovery points.

### Snapshots

The left sidebar contains the code structure. Where checkboxes are ticked, those parts of the code are snapshotted in an open state — the full function is expanded. If unchecked, only the function heading is included. This allows a compressed overview of a module, or specific parts of it, without including the full source. You can snapshot important focal points, or just the overall skeleton of the code you are working on.

### API and Security Model

The main configuration supplies the default provider, endpoint and model used by Planning, annotation and final verification. Individual agent records can override provider, endpoint, model and key-environment reference.

Before an agent cycle begins, Iterize shows the destinations that will receive role prompts and relevant code. Project-level agent configuration is scrubbed of plaintext API keys. A local custom endpoint on localhost can operate without a key; remote endpoints require the configured environment variable to exist.

---

## Important Project Records

| File or Folder | Role |
|---|---|
| `designations.json` | Stable project/module/class/function identities and structural history. |
| `agents/instructions_mission.md` | The single canonical project specification shared by Planning, Mission and Verify. |
| `agents/spec.json` | Agent roster, role assignments, model overrides and orchestration settings. |
| `agents/mission_session.json` | Mission's persistent conversation context. |
| `agents/objectives.json` | Current checkable objectives produced by Mission. |
| `agents/evolution.json` | Live development and scope-change record. |
| `agents/evolution_history.json` | Persistent undo/redo states for the evolution log. |
| `agents/workspace/` | Staged agent products and run records. |
| `agents/verification/` | Aggregated specification and final verification reports. |
| `kg_layout.json` | Persistent Knowledge Graph node coordinates. |
| `kg_mappings.json` | Functional mapping rows used by people and graph memory. |
| `Snapshots/` | Structure and Knowledge Graph snapshots. |
| `chats/` | Growing Markdown transcripts, one file per planning conversation. |
| `originals/` and `iterations/` | Protected baselines and timestamped development iterations. |

---

## A Practical Example

Suppose the user says: *"Change the background colour from red to blue."*

1. Planning or Mission incorporates the change into the current project scope.
2. Mission identifies the relevant requirement and delegates it to Builder.
3. Designations and the Knowledge Graph locate the responsible function and show its callers.
4. Builder replaces only that function or the smallest necessary code region.
5. Compile and execution gates check the candidate.
6. Live View highlights the changed lines in green.
7. Reviewer confirms that the colour requirement is satisfied and reports whether surrounding behaviour remains sound.
8. The user approves the staged result into the active file.
9. Verify later includes the blue-background requirement in the aggregated specification and checks that the finished project still implements it.

The same mechanism applies to a traceback. Instead of treating the whole program as broken, Iterize resolves the failing line to its designation, supplies the exact error as evidence and concentrates the cycle on the smallest implicated function while protecting already working areas.

---

## In Summary

Iterize is designed around continuity, division of labour and controlled change. Human intent is formalised once and carried forward. Mission manages the project, Builder performs focused implementation, Reviewer narrows what remains, the Knowledge Graph supplies structural memory, and Verify compares the finished code with the accumulated specification.

The result is an environment intended not merely to generate code, but to help a project retain its purpose while it evolves.

---

*8 August 2026 — JL Kosev-Lex — [iterize.org](https://iterize.org)*

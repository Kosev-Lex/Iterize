# Iterize — Quick User Guide

## 1. Open or create a project

Use **File → New Project** or **File → Open Project**.

Project files appear in the file tree. Open a file by selecting it from the tree.

## 2. Make a file active

The **active file** is the file the Agents workspace will work on.

Open the file you want to change in the main editor, then use **Import Active File** in the Agents workspace.

This copies the current file into the agent staging area. The original project file is not changed until you approve the result.

## 3. Plan the work

Use the main **Planning Chat** to describe the project, feature or change you want.

When the requirements are clear:

1. Select **Draft / update from discussion**.
2. Review the generated mission.
3. Select **Confirm → Mission + Verify**.

This creates or updates the project's canonical mission specification.

## 4. Configure agents

Open:

**Orchestrate Agents → Open Orchestrator**

Assign models to:

* **Mission** — coordinates the work.
* **Builder** — makes the code changes.
* **Reviewer** — checks the result.

Mission can also be given additional project files for broader context.

## 5. Build

Import the active file and press **Run**.

The normal cycle is:

**Mission → Builder → Reviewer → repair if necessary**

Builder works on a staged copy rather than directly modifying your source file.

Use **Live View** to inspect changes as they are produced.

## 6. Approve changes

When the staged version is satisfactory, select:

**Approve → Active File**

The staged code is then written back to the original project file.

If the original file changed after it was imported, Iterize warns about the conflict before overwriting it.

## 7. Save an iteration

Use **Save as Iteration** when you reach a useful development point that you may want to return to.

Iterize creates a timestamped copy of the file and records its designation state.

Use this regularly after significant working changes.

For an important baseline, use **Mark as Original**. This archives and freezes the file until it is deliberately unfrozen.

## 8. Run and debug

Run Python files using the normal **Run** controls.

If a traceback occurs, use:

**Run → Send Last Error to Agents**

Iterize identifies the failing project file and the smallest relevant function or class, then stages the error for a focused repair cycle.

## 9. Use the Knowledge Graph

Open the **Knowledge Graph** to inspect:

* project modules;
* classes and functions;
* imports;
* function calls;
* functional mappings.

The graph is also used internally to give agents relevant structural context when modifying code.

## 10. Verify the project

When development is complete, open **Verify** and select:

**Build Spec + Verify Code**

Verify compares the current project source with the accumulated mission and scope changes.

Requirements are classified as:

* **Correct**
* **Incorrect**
* **Uncertain**

Use the report to identify any remaining work.

## Recommended workflow

For most changes:

**Open project → open target file → plan the change → confirm mission → Import Active File → Run Agents → inspect Live View/Reviewer → Approve → test program → Save as Iteration**

For a finished release:

**Save Iteration → run tests → Verify project → correct remaining issues → create final iteration/original checkpoint.**

### Important

Agent work is staged. **Import Active File does not change your source, and Builder changes do not become permanent until you select Approve → Active File.**

Use **Save as Iteration** frequently when the project reaches a known-good state.

"""
agents.py — agent workspace for pyedit (v5, "product cycle").

Design (the v4 objectives loop, now centred on one visible product):

  INSTRUCTIONS          Authored in pyedit (Tools ▸ Instructions Setup…) →
                        agents/instructions_mission.md. The Mission box in this
                        window auto-populates from that file.

  ACTIVE FILE           The agents iterate the active file imported from
                        the main page (or build from scratch). There is
                        no separate product file: the iteration lives in
                        memory and in the builder's workspace window
                        until Approve writes it into the active file on
                        the main page. Delete discards the import here
                        without touching the main page.

  WORKSPACES            Each agent has its own live workspace window that
                        opens when it is given instructions: mission shows
                        the objectives + the prompts it delegates to the
                        builder; builder shows the file it is building;
                        reviewer shows its assessment.

  CYCLE (≤ MAX_CYCLES)  Run: mission → agents/objectives.json (structured,
                        modulable goals) + builder directive → builder →
                        the complete file (compile-gated, one repair
                        attempt) → reviewer judges file vs objectives;
                        unsatisfied feedback loops to mission, which
                        revises the directive. Every step is also operable
                        MANUALLY through the role's chat pane — your
                        message is guidance for that agent's step.

Kept from v4: designation store, agents/evolution.json, spec.json,
role roster with per-agent provider/model dropdowns, Live View,
inter-agent chat routing.

v6 convergence (this build): the dead module-scope pipeline is gone.
The builder never round-trips whole files for an imported base — it
returns a CHANGE SET (function replacements via AST splice, additions,
ensured imports) against the full base held in memory, planned from the
module skeleton for large files, so no file size can be truncated.
Whole-file mode survives only for from-scratch builds. Gate 3
(designation harnesses + test_cmd, sandboxed against the CANDIDATE via
override_src — the disk is never touched pre-Approve) runs inside the
cycle, and Approve refuses a failed gate without explicit override.
The Verify tab from verify.py IS mounted as the second notebook tab.
One orchestration at a time: manual role steps and automated runs
share a busy lock.

v7 circulation (this build): the organs are wired to each other. The
knowledge graph acts as MEMORY, not picture — the builder reads each
change target's graph card (callers/callees/mappings) and the CALLER
CODE from wherever it lives before amending anything, ending
single-file blindness. The mission mints testable requirements that
seed the Verify rubric BEFORE code exists; a rejecting reviewer must
mint new requirements or cite lines. Distilled pitfalls from the
evolution log ride into every repair. The run gate refuses to launch
candidates importing network/SMS/mail modules (file isolation is not
network isolation) unless spec.run_gate_force. Approve detects base
drift (active file edited after import). LLM calls are counted into
every run record. tkinter is OPTIONAL: without it this module still
exports the full Orchestrator for kernel.py. Pure standard library.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import textwrap
import threading
import time

try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except ImportError:          # headless kernel: Orchestrator only
    tk = ttk = messagebox = filedialog = None

from common import (atomic_write, atomic_write_json, attach_context_menu,
                    backup_damaged, extract_json, gate_compile,
                    locate_chain, now_iso, persist_geometry,
                    run_harness_sandbox, segment_for, UIState, resolve_python_interpreter)

_extract_json = extract_json
_attach_menu = attach_context_menu

AGENTS_DIR = "agents"
SPEC_FILE = "spec.json"
ROLES = ("mission", "builder", "reviewer")
MAX_REPAIRS = 2
MAX_INLINE_SRC = 24000   # base at/below this rides whole into the builder;
                         # above it the builder plans from the skeleton and
                         # receives only the targeted segments — NOTHING is
                         # ever silently truncated at any size
MAX_DIFF_CTX = 30000     # reviewer sees the unified diff, not the raw file
INSTRUCTIONS_FILE = "instructions_mission.md"  # one project contract, shared by Planning, Mission and Verify
LEGACY_INSTRUCTIONS_FILE = "instructions.md"
OBJECTIVES_FILE = "objectives.json"     # under agents/ — mission agent output
MAX_CYCLES = 3                          # design → build → review loops per run
PROVIDERS = ("anthropic", "openai", "custom")   # api_chat protocols; models
# come from the main-page API Settings (api_cfg["models"]), never hardcoded
HOME_CFG_PATH = os.path.join(os.path.expanduser("~"), ".pyedit_config.json")
PROVIDER_URLS = {                       # canonical endpoint per protocol,
    "anthropic": "https://api.anthropic.com/v1/messages",      # used only
    "openai": "https://api.openai.com/v1/chat/completions",    # when the
    "custom": "http://127.0.0.1:8080/v1/chat/completions",     # provider
}                                       # differs from the home settings
SNAPSHOTS_DIR = "Snapshots"
ITERATIONS_DIR = "iterations"


def mission_spec_path(project_dir: str) -> str:
    """The single project contract shared by Planning, Build and Verify."""
    return os.path.join(project_dir, AGENTS_DIR, INSTRUCTIONS_FILE)


def load_mission_spec(project_dir: str, fallback: str = "",
                      migrate: bool = True) -> str:
    """Load the canonical mission and migrate the former instructions.md.

    Older builds also generated an ``instructions_mission.md`` role stub.
    When both files exist, the old ``instructions.md`` project contract wins
    over that generated stub. After a successful migration only the canonical
    filename remains.
    """
    canonical = mission_spec_path(project_dir)
    legacy = os.path.join(project_dir, AGENTS_DIR, LEGACY_INSTRUCTIONS_FILE)

    def read(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            return ""

    current = read(canonical)
    old = read(legacy)
    generated_stub = current.lstrip().startswith(
        "# Mission instructions\n_agent:")
    chosen = (old if old and (not current.strip() or generated_stub)
              else current) or fallback
    if migrate and chosen.strip():
        try:
            atomic_write(canonical, chosen.rstrip() + "\n")
            if os.path.isfile(legacy):
                os.remove(legacy)
        except OSError:
            pass
    return chosen.strip()

DEFAULT_SYSTEM = {
    "mission": "You are the mission agent: you turn instruction documents "
               "into structured objectives and concrete directives for the "
               "builder. Respond ONLY with JSON.",
    "builder": "You amend Python code by CHANGE SET: complete replacement "
               "functions, additions, and required imports — never a "
               "rewrite of the whole file when a base exists. Change only "
               "what the directive requires. Respond ONLY with JSON.",
    "reviewer": "You are the reviewer agent: you judge this cycle's code "
                "changes against the objectives. You wrote neither. "
                "Respond ONLY with JSON.",
}

OBJECTIVES_USER = """You are Agent 1, the persistent Mission coordinator.
Update the canonical mission document only when feedback or user guidance
changes the project scope. Preserve every still-valid requirement and every
known-good area. Then turn the resulting mission into a structured,
modulable set of objectives and a directive for the builder agent this
cycle. Objectives must be concrete and individually checkable — they are
adjusted across cycles to deal with bugs. Respond ONLY with JSON:
{"mission_markdown": "the complete updated canonical mission specification",
 "objectives": [{"id": "O1", "goal": "concrete checkable goal",
  "status": "open", "notes": ""}],
 "requirements": ["R1. testable requirement the finished code must
 satisfy (these seed the Verify rubric — write them as if a harness
 will execute them)", "R2. ..."],
 "builder_directive": "the prompt that will be delegated to the builder:
 exactly what to build or change this cycle"}
INSTRUCTIONS (verbatim):
%(instructions)s
BASE FILE (%(base_name)s) — STRUCTURE SKELETON (the builder receives the
actual code; nothing is truncated at build time):
%(base)s
PROJECT FILES AVAILABLE TO MISSION (project-wide context):
%(project_context)s
%(feedback)s
%(guidance)s
"""

BUILD_FILE_USER = """You are the builder, building FROM SCRATCH. Produce
the COMPLETE file this cycle — full source, not a fragment and not a diff.
DIRECTIVE FROM THE MISSION AGENT:
%(directive)s
OBJECTIVES:
%(objectives)s
%(evidence)s%(guidance)s
Respond ONLY with JSON:
{"filename": "suggested file name with extension",
 "file": "the complete source",
 "summary": "one paragraph: what you built or changed"}
"""

PLAN_TARGETS_USER = """You are the builder, planning a CHANGE SET against
an existing file that is too large to show whole. From the skeleton,
list which existing functions you must SEE AND CHANGE, and which you
must only SEE for context. Use dotted chains exactly as the skeleton
shows them (e.g. "ClassName.method" or "top_level_function").
DIRECTIVE FROM THE MISSION AGENT:
%(directive)s
OBJECTIVES:
%(objectives)s
FILE SKELETON (%(base_name)s):
%(skeleton)s
GRAPH MEMORY (call structure — plan seams, not just sites):
%(kg)s
%(evidence)s%(guidance)s
Respond ONLY with JSON:
{"change_targets": ["Chain.one", "chain_two"],
 "context_targets": ["Chain.related"],
 "reasoning": "one paragraph"}
"""

BUILD_CHANGESET_USER = """You are the builder. Amend the existing file by
CHANGE SET — do NOT return the whole file. Every value in "changes" must
be ONE complete function definition replacing the function at that
dotted chain. New top-level or class-scope definitions go in
"additions". Import lines the changes need go in "imports" (existing
imports are never removed).
DIRECTIVE FROM THE MISSION AGENT:
%(directive)s
OBJECTIVES:
%(objectives)s
FILE SKELETON (%(base_name)s):
%(skeleton)s
GRAPH MEMORY (who calls / is called by what you are touching — every
caller listed is a contract your change must keep working):
%(kg)s
%(caller_code)s
CURRENT CODE%(code_label)s:
%(code)s
%(evidence)s%(pitfalls)s%(repair)s
%(guidance)s
Respond ONLY with JSON:
{"changes": {"ClassName.method": "complete amended def", "func": "..."},
 "additions": [{"scope": "" , "code": "complete new def or class"},
               {"scope": "ClassName", "code": "complete new method def"}],
 "imports": ["import x", "from y import z"],
 "summary": "one paragraph: what changed and why"}
If no change is needed: {"changes": {}, "additions": [], "imports": [],
 "summary": "..."}
"""

BUILD_FIX_USER = """Your file failed to byte-compile. Fix it and return
the COMPLETE corrected source. Respond ONLY with JSON:
{"filename": "...", "file": "the complete corrected source",
 "summary": "what you fixed"}
COMPILE ERROR:
%(error)s
YOUR FILE:
%(file)s
"""

REVIEW_PRODUCT_USER = """Judge this cycle's product against the
objectives. If unsatisfactory, your message goes to the mission agent,
which will revise the objectives and re-direct the builder. Respond ONLY
with JSON:
{"satisfactory": true_or_false,
 "objectives_status": {"O1": "done" or "open" or "blocked"},
 "notes": "your assessment — every criticism must cite a diff line or a
 gate result; vibes are not findings",
 "new_requirements": ["R?. if not satisfactory: NEW testable requirements
 that would have caught what is wrong — these flow into the rubric",
 "..."],
 "message_to_mission": "if not satisfactory: what must be revised and why;
 else empty"}
OBJECTIVES:
%(objectives)s
%(guidance)s
ORACLE GATES: %(gates)s
BUILDER SUMMARY: %(summary)s
PRODUCT (%(name)s) — %(view_label)s:
%(file)s
"""


def resolve_agent_cfg(api_cfg: dict, spec: dict, role: str) -> dict:
    """Merge the role's agent endpoint override over the home config.
    SECURITY: any api_key found in spec data is discarded; keys resolve
    only from ~/.pyedit_config.json — directly, or via key_ref against
    its optional "keys": {name: key} map. Empty-string overrides are
    skipped so a blank roster field never blanks the home config."""
    cfg = dict(api_cfg)
    ov = dict(spec.get("agents", {}).get(
        spec.get("roles", {}).get(role, {}).get("agent", ""), {}))
    ov.pop("api_key", None)
    ref = ov.pop("key_ref", "")
    cfg.update({k: v for k, v in ov.items() if v})
    if ref:
        cfg["api_key"] = (api_cfg.get("keys", {}).get(ref)
                          or os.environ.get(ref, "")
                          or cfg.get("api_key", ""))
    return cfg


# ──────────────────────────────────────────────────────────────────────────
# Splicing (AST-located, indent-preserving, parse-gated)
# ──────────────────────────────────────────────────────────────────────────

def splice(src: str, chain: list, code: str) -> str:
    """Replace the function at `chain` in source text with `code`.
    Raises ValueError on any guard; result is parse-validated."""
    tree = ast.parse(src)
    node = locate_chain(tree, chain)
    if node is None or not isinstance(node, (ast.FunctionDef,
                                             ast.AsyncFunctionDef)):
        raise ValueError(f"{'.'.join(n for _k, n in chain)} not found")
    new_code = textwrap.dedent(code).strip("\n")
    ptree = ast.parse(new_code)          # raises SyntaxError → caller handles
    defs = [n for n in ptree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(ptree.body) != 1 or len(defs) != 1:
        raise ValueError("proposal must be exactly one function definition")
    if defs[0].name != chain[-1][1]:
        raise ValueError(f"proposal defines '{defs[0].name}', "
                         f"expected '{chain[-1][1]}'")
    lines = src.split("\n")
    start = min([d.lineno for d in node.decorator_list] + [node.lineno])
    end = node.end_lineno
    indent = re.match(r"[ \t]*", lines[node.lineno - 1]).group()
    repl = [(indent + ln) if ln.strip() else ""
            for ln in new_code.split("\n")]
    out = "\n".join(lines[:start - 1] + repl + lines[end:])
    ast.parse(out)                       # whole-module gate
    return out


def module_skeleton(src: str) -> str:
    """Compact structural skeleton of a module: dotted chains with
    signatures — the builder's map for planning a change set. Never
    lossy about STRUCTURE regardless of file size."""
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return f"(unparseable: {e})"
    lines = src.split("\n")
    out = []

    def sig(n):
        s = lines[n.lineno - 1].strip()
        return s[:-1] if s.endswith(":") else s

    def walk(node, prefix):
        for ch in getattr(node, "body", []):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(f"{prefix}{ch.name}  ·  {sig(ch)}  "
                           f"[lines {ch.lineno}-{ch.end_lineno}]")
            elif isinstance(ch, ast.ClassDef):
                out.append(f"{prefix}{ch.name}  ·  {sig(ch)}  "
                           f"[lines {ch.lineno}-{ch.end_lineno}]")
                walk(ch, prefix + ch.name + ".")
    walk(tree, "")
    return "\n".join(out) or "(no definitions)"


def chain_of(dotted: str) -> list:
    """"ClassName.method" → [("?", "ClassName"), ("F", "method")] — the
    locate walk matches by name, so kinds need not be pre-known."""
    parts = [p for p in dotted.strip().split(".") if p]
    return [("?", p) for p in parts]


def add_definition(src: str, scope: str, code: str) -> str:
    """Append one new def/class at module scope (scope == "") or at the
    end of the named class's body. Parse-gated like splice()."""
    new_code = textwrap.dedent(code).strip("\n")
    ptree = ast.parse(new_code)
    if len(ptree.body) != 1 or not isinstance(
            ptree.body[0], (ast.FunctionDef, ast.AsyncFunctionDef,
                            ast.ClassDef)):
        raise ValueError("addition must be exactly one def or class")
    name = ptree.body[0].name
    tree = ast.parse(src)
    lines = src.split("\n")
    if not scope:
        if locate_chain(tree, [("?", name)]) is not None:
            raise ValueError(f"'{name}' already exists at module scope — "
                             "use changes to replace it")
        out = src.rstrip("\n") + "\n\n\n" + new_code + "\n"
    else:
        node = locate_chain(tree, chain_of(scope))
        if node is None or not isinstance(node, ast.ClassDef):
            raise ValueError(f"class '{scope}' not found")
        if any(getattr(ch, "name", None) == name for ch in node.body):
            raise ValueError(f"'{scope}.{name}' already exists — use "
                             "changes to replace it")
        body_indent = re.match(
            r"[ \t]*", lines[node.body[0].lineno - 1]).group()
        repl = [(body_indent + ln) if ln.strip() else ""
                for ln in new_code.split("\n")]
        end = node.end_lineno
        out = "\n".join(lines[:end] + [""] + repl + lines[end:])
    ast.parse(out)                       # whole-module gate
    return out


def ensure_imports(src: str, wanted: list) -> tuple:
    """Insert import lines that are not already present, after the last
    existing top-level import (or after the module docstring). Existing
    imports are never removed. → (new_src, added_lines)."""
    tree = ast.parse(src)
    lines = src.split("\n")
    have = {ln.strip() for ln in lines}
    add = []
    for w in wanted or []:
        w = str(w).strip()
        if not w or w in have:
            continue
        try:
            stmt = ast.parse(w).body
        except SyntaxError:
            continue
        if len(stmt) == 1 and isinstance(stmt[0], (ast.Import,
                                                   ast.ImportFrom)):
            add.append(w)
    if not add:
        return src, []
    at = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            at = node.end_lineno
        elif at == 0 and isinstance(node, ast.Expr) and isinstance(
                getattr(node, "value", None), ast.Constant):
            at = node.end_lineno          # module docstring
    out = "\n".join(lines[:at] + add + lines[at:])
    ast.parse(out)
    return out, add


# ──────────────────────────────────────────────────────────────────────────
# Stores: spec, evolution log, pass-cache
# ──────────────────────────────────────────────────────────────────────────

class AgentSpecStore:
    SCHEMA = "pyedit.agents/3"

    def __init__(self, project_dir: str):
        self.root = os.path.abspath(project_dir)
        self.dir = os.path.join(self.root, AGENTS_DIR)
        self.path = os.path.join(self.dir, SPEC_FILE)
        self.spec = self.load()
        self.spec["mission"] = load_mission_spec(
            self.root, self.spec.get("mission", ""), migrate=True)

    def default(self) -> dict:
        return {"_schema": self.SCHEMA, "updated": now_iso(),
                "mission": "",
                "roles": {r: {"agent": "", "instructions": "", "files": []}
                          for r in ROLES},
                "agents": {}, "test_cmd": "", "interpreter": resolve_python_interpreter()}

    def _scrub_keys(self, spec: dict) -> int:
        """SECURITY: spec.json lives in project scope and can be injected
        into prompts / shared with the project — key material must never
        be stored here. Strip any plaintext api_key found."""
        n = 0
        for a in spec.get("agents", {}).values():
            if isinstance(a, dict) and a.pop("api_key", None):
                a.setdefault("key_ref", "")
                n += 1
        return n

    def load(self) -> dict:
        spec = self.default()
        self.scrubbed = 0
        self.damaged = None
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    old = json.load(f)
            except json.JSONDecodeError:
                self.damaged = backup_damaged(self.path)
                return spec
            except OSError:
                return spec
            if old.get("_schema") == self.SCHEMA:
                self.scrubbed = self._scrub_keys(old)
                if self.scrubbed:
                    try:                      # remove keys from disk NOW
                        atomic_write_json(self.path, old)
                    except OSError:
                        pass
                return old
            # migrate v1/v2: keep roster; map project→mission, class→builder
            spec["agents"] = old.get("agents", {})
            self.scrubbed = self._scrub_keys(spec)
            lv = old.get("levels", {})
            spec["roles"]["mission"]["agent"] = lv.get("project", {}).get("agent", "")
            spec["roles"]["builder"]["agent"] = (lv.get("class", {}).get("agent", "")
                                                 or lv.get("function", {}).get("agent", ""))
            spec["mission"] = lv.get("project", {}).get("instructions", "")
            spec["roles"]["builder"]["instructions"] = "\n".join(
                lv.get(k, {}).get("instructions", "")
                for k in ("class", "function")).strip()
        return spec

    def save(self):
        os.makedirs(self.dir, exist_ok=True)
        self.spec["updated"] = now_iso()
        atomic_write_json(self.path, self.spec)
        # instructions_mission.md is the project contract, not a generated
        # role-system-prompt file. Builder/reviewer prompts remain useful as
        # readable sidecars; Mission's system prompt stays in spec.json.
        mission = (self.spec.get("mission") or "").strip()
        if mission:
            atomic_write(mission_spec_path(self.root), mission + "\n")
        legacy = os.path.join(self.dir, LEGACY_INSTRUCTIONS_FILE)
        if os.path.isfile(legacy):
            try:
                os.remove(legacy)
            except OSError:
                pass
        for r in ("builder", "reviewer"):
            rec = self.spec["roles"][r]
            atomic_write(
                os.path.join(self.dir, f"instructions_{r}.md"),
                f"# {r.capitalize()} instructions\n"
                f"_agent: {rec.get('agent') or '(unassigned)'} — "
                f"updated {self.spec['updated']}_\n\n"
                f"{rec.get('instructions', '') or '(empty)'}\n")


_EVO_LOCKS: dict = {}
_EVO_LOCKS_GUARD = threading.Lock()


def _evo_lock(path: str) -> threading.Lock:
    with _EVO_LOCKS_GUARD:
        return _EVO_LOCKS.setdefault(os.path.abspath(path),
                                     threading.Lock())


class EvolutionLog:
    """Concurrent-safe within the process: every append re-reads the
    file under a per-path lock before writing (atomically), so parallel
    verifier/orchestrator activity can no longer lose entries."""

    def __init__(self, project_dir: str):
        self.path = os.path.join(project_dir, AGENTS_DIR, "evolution.json")
        self._lock = _evo_lock(self.path)
        self.entries = self._read()

    def _read(self) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def pitfalls(self, designation: str, k: int = 6) -> str:
        """Distilled failure memory: the DEDUPLICATED reasons this
        designation's past attempts failed, newest last. This is what
        'learning from the evolution log' means operationally — every
        repair prompt reads it, so the same dead end is not walked
        twice across runs."""
        seen, out = set(), []
        for e in self.entries:
            if e.get("designation") != designation:
                continue
            if e.get("verdict") not in ("fail", "repair", "discarded"):
                continue
            r = (e.get("verdict_reason") or "").strip()[:160]
            if r and r not in seen:
                seen.add(r)
                out.append(f"- {r}")
        return "\n".join(out[-k:]) or "(no recorded pitfalls)"

    def compact(self, keep: int = 1500) -> str | None:
        """Bound the append-only log: archive the oldest overflow to
        evolution_archive_<ts>.json. Distillation (pitfalls) still sees
        only the live file — archives are for the human record."""
        with self._lock:
            self.entries = self._read()
            if len(self.entries) <= keep:
                return None
            cut = self.entries[:-keep]
            self.entries = self.entries[-keep:]
            arc = self.path.replace(
                "evolution.json",
                f"evolution_archive_{time.strftime('%Y%m%d-%H%M%S')}.json")
            try:
                atomic_write_json(arc, cut)
                atomic_write_json(self.path, self.entries)
            except OSError:
                return None
            return arc

    def append(self, **entry):
        entry.setdefault("ts", now_iso())
        with self._lock:
            self.entries = self._read()      # merge concurrent appends
            self.entries.append(entry)
            try:
                atomic_write_json(self.path, self.entries)
            except OSError:
                pass


class OrchestratorStopped(Exception):
    pass


# ──────────────────────────────────────────────────────────────────────────
# Orchestrator v3 — build flat, gate with execution, repair on evidence
# ──────────────────────────────────────────────────────────────────────────

class Orchestrator:
    def __init__(self, project_dir: str, desig, spec: dict, api_cfg: dict,
                 chat_fn, emit, product: dict | None = None):
        self.root = os.path.abspath(project_dir)
        self.desig = desig
        self.spec = spec
        self.api_cfg = api_cfg
        self.chat_fn = chat_fn
        self.emit = emit
        # the product: one mutable dict, shared with the window so manual
        # steps and automated runs work on the same artifact
        self.product = product if product is not None else {}
        self.evo = EvolutionLog(project_dir)
        self._stop = False
        self._kg = None                  # lazy KGMemory (graph AS memory)
        self.llm_calls = 0               # budget accounting
        self.mission_session_path = os.path.join(
            self.root, AGENTS_DIR, "mission_session.json")
        self.mission_messages = self._load_mission_session()

    def _load_mission_session(self) -> list:
        try:
            with open(self.mission_session_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rows = data.get("messages", []) if isinstance(data, dict) else []
            return [m for m in rows if isinstance(m, dict)
                    and m.get("role") in ("user", "assistant")
                    and isinstance(m.get("content"), str)][-24:]
        except (OSError, json.JSONDecodeError, TypeError):
            return []

    def _save_mission_session(self):
        """Agent 1 retains continuity; Agents 2 and 3 are deliberately fresh."""
        try:
            atomic_write_json(self.mission_session_path, {
                "updated": now_iso(),
                "messages": self.mission_messages[-24:],
            })
        except OSError:
            pass

    def kg(self):
        """The knowledge graph as compressed, rapidly accessible memory
        of the codebase — built once per orchestrator, read by the
        builder before it touches anything."""
        if self._kg is None:
            try:
                from kgraph import KGMemory
                self._kg = KGMemory(self.root, self.desig.data
                                    if self.desig else {"modules": {}})
            except Exception as e:  # noqa: BLE001 — memory is an aid,
                self._kg = False    # never a crash
                self.emit(f"  [kg memory unavailable: {e}]\n", "meta")
        return self._kg or None

    def stop(self):
        self._stop = True

    def _check(self):
        if self._stop:
            raise OrchestratorStopped()

    # ── agents ──
    def _assigned(self, role: str) -> bool:
        return bool(self.spec["roles"].get(role, {}).get("agent"))

    def _agent_cfg(self, role: str) -> dict:
        return resolve_agent_cfg(self.api_cfg, self.spec, role)

    def _agent_name(self, role: str) -> str:
        return self.spec["roles"].get(role, {}).get("agent", role)

    def _call(self, role: str, user: str) -> dict:
        """Runs the request on a worker and polls the stop flag every 0.5s,
        so Stop interrupts even a hung HTTP call (the abandoned request
        thread dies with its socket timeout)."""
        self._check()
        if not self._assigned(role):
            return {}
        system = (self.spec["roles"][role].get("instructions", "")
                  or DEFAULT_SYSTEM[role])
        box: dict = {}

        if role == "mission":
            history = list(self.mission_messages[-24:])
            while history and sum(len(m.get("content", ""))
                                  for m in history) + len(user) > 180000:
                history.pop(0)
            messages = [*history, {"role": "user", "content": user}]
            request_tokens = (len(system) + sum(
                len(m.get("content", "")) for m in messages) + 3) // 4
            retained_tokens = (len(system) + sum(
                len(m.get("content", "")) for m in history) + 3) // 4
            self.emit((request_tokens, retained_tokens), "_mission_tokens")
        else:
            messages = [{"role": "user", "content": user}]

        def w():
            try:
                box["r"] = self.chat_fn(self._agent_cfg(role),
                                        messages,
                                        system=system)
            except Exception as e:  # noqa: BLE001
                box["e"] = e
        t = threading.Thread(target=w, daemon=True)
        t.start()
        while t.is_alive():
            t.join(0.5)
            if self._stop:
                raise OrchestratorStopped()
        if "e" in box:
            raise box["e"]
        self.llm_calls += 1
        if role == "mission":
            self.mission_messages = (messages + [{
                "role": "assistant", "content": box["r"]
            }])[-24:]
            self._save_mission_session()
            retained_tokens = (len(system) + sum(
                len(m.get("content", ""))
                for m in self.mission_messages) + 3) // 4
            self.emit((request_tokens, retained_tokens), "_mission_tokens")
        return _extract_json(box["r"])

    # ── context ──
    def _read(self, path, cap=None):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                s = f.read()
            return s if cap is None else s[:cap]
        except OSError:
            return ""

    def _role_context(self, role: str) -> str:
        out, used = [], 0
        for fp in self.spec["roles"].get(role, {}).get("files", []):
            p = fp if os.path.isabs(fp) else os.path.join(self.root, fp)
            c = self._read(p, cap=MAX_INLINE_SRC)
            if c:
                out.append(f"--- {os.path.basename(fp)} ---\n{c}")
                used += len(c)
                if used >= 160000:
                    out.append("(project context cap reached; module skeletons "
                               "remain available through designations/KG)")
                    break
        return "\n".join(out) or "(none)"

    # ── oracle gate 3 (gates 1–2 live in splice / common.gate_compile) ──
    def _touched_designations(self, chains: list) -> list:
        """Dotted chains → bare designation ids (revision suffixes
        stripped: harness files are named by bare ids). Empty when the
        product has no registered rel (scratch builds)."""
        rel = self.product.get("rel")
        if not (rel and self.desig):
            return []
        out = []
        for dotted in chains:
            d = self.desig.designation(rel, chain_of(dotted))
            if d:
                out.append(re.sub(r"\([a-z]+\)", "", d[0]))
        return out

    def _gate_exec(self, candidate: str, touched: list) -> tuple:
        """Converged gate 3, run against the CANDIDATE — the real file is
        never touched before Approve. Every touched designation's
        Verify-tab harness (agents/harness/<id>_test.py) runs via the
        shared sandbox runner with the candidate spliced in through
        override_src; then the optional spec.test_cmd runs in a sandbox
        copy of the registered modules (also with the candidate in
        place). With no harnesses and no test_cmd the gate is skipped."""
        outs, ran = [], False
        rel = self.product.get("rel")
        hdir = os.path.join(self.root, AGENTS_DIR, "harness")
        mods = [r for r, m in self.desig.data.get("modules", {}).items()
                if not m.get("deleted")] if self.desig else []
        cp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "common.py")
        extra = (cp,) if os.path.isfile(cp) else ()
        for did in touched:
            hp = os.path.join(hdir,
                              re.sub(r"[^\w().\-]", "_", did) + "_test.py")
            if not os.path.isfile(hp):
                continue
            ran = True
            res, err = run_harness_sandbox(
                self.root, mods, hp,
                interpreter=self.spec.get("interpreter"),
                target_rel=rel, override_src=candidate,
                extra_files=extra)
            failed = [r for r, v in res.items() if not v["pass"]]
            if err or failed:
                detail = err or "; ".join(
                    f"{r}: {res[r]['detail'][:120]}" for r in failed)
                return False, f"harness {did}: {detail}"[:1500]
            outs.append(f"harness {did}: {len(res)} req ok")
        cmd = (self.spec.get("test_cmd") or "").strip()
        if cmd and rel and mods:
            ran = True
            import tempfile
            tmp = tempfile.mkdtemp(prefix="pyedit_gate3_")
            try:
                for m in mods:
                    srcp = os.path.join(self.root, m.replace("/", os.sep))
                    dstp = os.path.join(tmp, m.replace("/", os.sep))
                    os.makedirs(os.path.dirname(dstp) or tmp,
                                exist_ok=True)
                    if m == rel:
                        with open(dstp, "w", encoding="utf-8") as f:
                            f.write(candidate)
                    elif os.path.isfile(srcp):
                        import shutil as _sh
                        _sh.copy2(srcp, dstp)
                try:
                    p = subprocess.run(cmd, shell=True, cwd=tmp,
                                       capture_output=True, text=True,
                                       timeout=300)
                except (OSError, subprocess.TimeoutExpired) as e:
                    return False, str(e)
                out = ((p.stdout or "") + (p.stderr or "")).strip()[-1500:]
                if p.returncode != 0:
                    return False, out
                outs.append(out or "test_cmd ok")
            finally:
                import shutil as _sh
                _sh.rmtree(tmp, ignore_errors=True)
        if self.spec.get("run_gate", True):
            ok, out = self._gate_launch(candidate)
            ran = True
            if not ok:
                return False, out
            outs.append(out)
        if not ran:
            return True, "(no harnesses, test_cmd, or run gate)"
        return True, " | ".join(outs)[-1500:]

    RUN_GATE_SECS = 8
    TB_MARKS = ("Traceback (most recent call last):",
                "Exception in Tkinter callback")
    SIDE_EFFECT_MODULES = frozenset((
        "twilio", "requests", "httpx", "smtplib", "serial", "paramiko",
        "boto3", "stripe", "aiohttp"))

    def _side_effect_imports(self, source: str) -> list:
        """Top-level module names the candidate imports that talk to the
        world (network/SMS/mail/hardware). The run gate refuses to
        launch these autonomously: 'file isolation' must never quietly
        become 'sent a real SMS while gating'."""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        hit = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.split(".")[0] in self.SIDE_EFFECT_MODULES:
                        hit.add(a.name.split(".")[0])
            elif isinstance(n, ast.ImportFrom) and n.module:
                if n.module.split(".")[0] in self.SIDE_EFFECT_MODULES:
                    hit.add(n.module.split(".")[0])
        return sorted(hit)

    def _gate_launch(self, candidate: str) -> tuple:
        """Run gate: LAUNCH the candidate as a program in a sandbox copy
        and watch stderr/stdout for tracebacks. Catches import-time and
        startup crashes autonomously. A GUI mainloop that survives
        RUN_GATE_SECS without a traceback passes (interaction-driven
        errors can't be provoked here — they arrive via 'Send Last Error
        to Agents' instead). File isolation only: the program runs with
        normal user privileges and, for GUI code, needs a display."""
        import tempfile
        import shutil as _sh
        rel = self.product.get("rel") or ""
        entry = rel or (self.product.get("name") or "product.py")
        if not entry.endswith((".py", ".pyw")):
            return True, "(run gate skipped — not a python file)"
        risky = self._side_effect_imports(candidate)
        if risky and not self.spec.get("run_gate_force"):
            return True, (f"run gate SKIPPED: candidate imports "
                          f"side-effecting module(s) {', '.join(risky)} — "
                          f"the sandbox isolates files, NOT the network; "
                          f"set run_gate_force in spec.json to launch "
                          f"anyway")
        mods = [r for r, m in self.desig.data.get("modules", {}).items()
                if not m.get("deleted")] if (self.desig and rel) else []
        tmp = tempfile.mkdtemp(prefix="pyedit_rungate_")
        try:
            for m in mods:
                srcp = os.path.join(self.root, m.replace("/", os.sep))
                dstp = os.path.join(tmp, m.replace("/", os.sep))
                os.makedirs(os.path.dirname(dstp) or tmp, exist_ok=True)
                if m != rel and os.path.isfile(srcp):
                    _sh.copy2(srcp, dstp)
            ep = os.path.join(tmp, entry.replace("/", os.sep))
            os.makedirs(os.path.dirname(ep) or tmp, exist_ok=True)
            with open(ep, "w", encoding="utf-8") as f:
                f.write(candidate)
            interp = resolve_python_interpreter(
                self.spec.get("interpreter")
            )

            if not interp:
                return False, "run gate could not find a Python interpreter"

            timed_out, out = False, ""
            try:
                p = subprocess.run([interp, "-u", ep], cwd=tmp,
                                   capture_output=True, text=True,
                                   timeout=self.RUN_GATE_SECS)
                out = ((p.stdout or "") + (p.stderr or ""))
                rc = p.returncode
            except subprocess.TimeoutExpired as e:
                timed_out, rc = True, 0
                out = (((e.stdout or b"") if isinstance(e.stdout, bytes)
                        else (e.stdout or ""))
                       + ((e.stderr or b"") if isinstance(e.stderr, bytes)
                          else (e.stderr or "")))
                if isinstance(out, bytes):
                    out = out.decode("utf-8", "replace")
            except OSError as e:
                return False, f"run gate could not launch: {e}"
            tb_at = min((i for i in (out.find(m) for m in self.TB_MARKS)
                         if i != -1), default=-1)
            if tb_at != -1:
                return False, ("run gate: traceback while running:\n"
                               + out[tb_at:tb_at + 2500])
            if not timed_out and rc != 0:
                return False, (f"run gate: exited {rc}:\n" + out[-1500:])
            return True, (f"run gate: "
                          + (f"alive after {self.RUN_GATE_SECS}s "
                             f"(mainloop assumed)" if timed_out
                             else f"clean exit {rc}"))
        finally:
            _sh.rmtree(tmp, ignore_errors=True)

    # ── instructions / objectives / inter-agent messaging ──
    def _instructions(self) -> str:
        txt = load_mission_spec(
            self.root, self.spec.get("mission", ""), migrate=True)[:20000]
        return (txt or self.spec.get("mission", "").strip()
                or "(no instructions — Tools ▸ Instructions Setup…)")

    def _save_objectives(self, obj: dict):
        """agents/objectives.json + a rendered .md — the modulable goal
        set the agents adjust as they respond to each other."""
        p = os.path.join(self.root, AGENTS_DIR, OBJECTIVES_FILE)
        obj["updated"] = now_iso()
        atomic_write_json(p, obj)
        md = ["# Objectives",
              f"_{obj['updated']} · cycle {obj.get('cycle', '?')}_", ""]
        for o in obj.get("objectives", []):
            md.append(f"- **{o.get('id', '?')}** "
                      f"[{o.get('status', 'open')}] {o.get('goal', '')}"
                      + (f" — {o['notes']}" if o.get("notes") else ""))
        atomic_write(p[:-5] + ".md", "\n".join(md) + "\n")

    def _seed_spec(self, reqs: list):
        """Spec precedes code: the mission's testable requirements are
        written as a DRAFT instructions file for the product module's
        designation before the builder runs — so the Verify rubric is
        authored from intent, not reverse-engineered from whatever the
        builder happened to produce. A confirmed spec is never touched."""
        rel = self.product.get("rel")
        if not (rel and self.desig):
            return
        mod = self.desig.data.get("modules", {}).get(rel)
        if not mod:
            return
        did = "P1" + mod.get("id", "")
        try:
            from verify import safe_name, INSTR_DIR
        except ImportError:
            return
        p = os.path.join(self.root, INSTR_DIR, safe_name(did) + ".md")
        v = mod.get("verified") or {}
        if v.get("spec_state") == "confirmed":
            return                       # human truth outranks the agent
        body = (f"# {did} — {rel}\n\n## Purpose\n"
                f"{(self.product.get('directive') or '')[:400]}\n\n"
                f"## Requirements\n" + "\n".join(reqs) + "\n")
        try:
            atomic_write(p, body)
            mod.setdefault("verified", {})["spec_state"] = "draft"
            self.desig.save()
            self.emit(f"  spec seeded (draft) → instructions/"
                      f"{safe_name(did)}.md — confirm it in Verify\n",
                      "meta")
        except OSError:
            pass

    def _say(self, frm: str, to: str, text: str):
        """Inter-agent message: routed to both roles' chat panes and the
        main log."""
        line = f"{frm} → {to}: {text.strip()[:400]}\n"
        self.emit((frm, line), "_chat")
        self.emit((to, line), "_chat")
        self.emit("  " + line, "meta")

    # ── the product and the per-role workspaces ──
    def _ws(self, role: str, text: str, mode: str = "set"):
        """Route tangible output into the role's live workspace window."""
        self.emit((role, mode, text), "_ws")

    def _role_status(self, role: str, text: str):
        """Publish the compact result shown inside Roles and hand-offs."""
        self.emit((role, text), "_role_status")

    def _persist_product(self):
        """There is NO separate product file: the iteration of the active
        file lives in memory (this shared dict + the builder's workspace
        window) until Approve writes it into the active file on the main
        page. This just refreshes the tab's status label."""
        self.emit((self.product.get("name", ""),
                   self.product.get("source", "")), "_product")

    def _compile_src(self, src: str) -> tuple:
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".py", prefix="pyedit_prod_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(src)
            return gate_compile(p, self.spec.get("interpreter") or None)
        finally:
            try:
                os.unlink(p)
            except OSError:
                pass

    # ── pipeline steps: callable manually (role chats) or by run() ──
    def step_mission(self, guidance: str = "", feedback: str = "") -> str:
        """Mission agent: instructions (+ feedback/guidance) → objectives
        + the prompt delegated to the builder. Shown in its workspace."""
        self._ws("mission", "working: objectives + builder prompt…",
                 "working")
        fb = ""
        if feedback:
            fb = ("FEEDBACK (reviewer verdict or runtime error evidence) "
                  "— revise the objectives and the directive to address "
                  "it:\n" + feedback
                  + "\nPREVIOUS OBJECTIVES:\n"
                  + json.dumps(self.product.get("objectives", []),
                               indent=1))
        base = self.product.get("source", "")
        resp = self._call("mission", OBJECTIVES_USER % {
            "instructions": self._instructions(),
            "base_name": self.product.get("base_name") or "(scratch)",
            "base": (module_skeleton(base) if base
                     else "(none — building from scratch)"),
            "project_context": self._role_context("mission"),
            "feedback": fb,
            "guidance": ("USER GUIDANCE:\n" + guidance) if guidance
                        else ""})
        previous_mission = self._instructions()
        mission_md = str(resp.get("mission_markdown") or "").strip()
        if mission_md and mission_md != previous_mission.strip():
            mission_md = re.sub(
                r"^```(?:markdown|md)?\s*|\s*```$", "", mission_md,
                flags=re.I | re.S).strip()
            atomic_write(mission_spec_path(self.root), mission_md + "\n")
            self.spec["mission"] = mission_md
            udiff = "\n".join(difflib.unified_diff(
                previous_mission.splitlines(), mission_md.splitlines(),
                "mission-before", "mission-after", lineterm=""))
            self.evo.append(
                run=self.product.get("run_ts", ""),
                agent=self._agent_name("mission"), level="project",
                designation="P1", job="mission specification updated",
                reason=(guidance or feedback or "mission coordination")[:500],
                diff=udiff[:8000], verdict="scope_change",
                verdict_by="mission")
        latest_mission = mission_md or previous_mission
        self.emit(latest_mission, "_mission_spec")
        objs = resp.get("objectives") or []
        directive = str(resp.get("builder_directive") or "").strip()
        self.product["objectives"] = objs
        self.product["directive"] = directive
        reqs = [str(r).strip() for r in (resp.get("requirements") or [])
                if str(r).strip()]
        if reqs:
            self.product["requirements"] = reqs
            self._seed_spec(reqs)
        self._save_objectives({"objectives": objs,
                               "cycle": self.product.get("cycle", 0)})
        self._persist_product()
        self._ws("mission",
                 f"── {now_iso()} ──\nCANONICAL MISSION\n"
                 + latest_mission
                 + "\n\nOBJECTIVES\n"
                 + "\n".join(f"  {o.get('id', '?')} "
                             f"[{o.get('status', 'open')}] "
                             f"{o.get('goal', '')}" for o in objs)
                 + "\n\nPROMPT DELEGATED TO BUILDER\n" + directive
                 + "\n")
        self._ws("builder",
                 f"── DELEGATION FROM MISSION · {now_iso()} ──\n"
                 f"{directive}\n\nOBJECTIVES\n"
                 + "\n".join(f"- {o.get('id', '?')}: {o.get('goal', '')}"
                              for o in objs))
        self._role_status(
            "builder", "ASSIGNED BY MISSION\n\n" +
            (directive or "No build directive was returned."))
        self.evo.append(
            run=self.product.get("run_ts", ""),
            agent=self._agent_name("mission"), level="project",
            designation="P1", job="delegated build to Agent 2",
            reason=directive[:500], verdict="delegated",
            verdict_by="mission")
        return directive

    def _evidence(self) -> str:
        ev = (self.product.get("evidence") or "").strip()
        return (f"RUNTIME ERROR EVIDENCE (a real traceback from running "
                f"this code — the fix must make it impossible; file and "
                f"line numbers map onto the skeleton's line ranges):\n"
                f"{ev}\n") if ev else ""

    def step_build(self, guidance: str = "") -> str:
        """Builder agent. From scratch: the COMPLETE file, compile-gated
        with one repair attempt. Against an imported base: a CHANGE SET
        (splice replacements + additions + ensured imports) applied to
        the FULL in-memory base — planned from the skeleton when the file
        exceeds MAX_INLINE_SRC, so no size is ever truncated. Both paths
        end at the compile gate and the sandboxed execution gate (touched
        harnesses + test_cmd) against the candidate. Shown live in the
        builder's workspace; the disk is untouched until Approve."""
        directive = self.product.get("directive", "").strip()
        if not (directive or guidance):
            raise RuntimeError("no directive yet — run the mission agent "
                               "first, or type guidance into the builder "
                               "chat")
        base = self.product.get("source", "")
        if base:
            return self._build_changeset(directive, guidance, base)
        return self._build_scratch(directive, guidance)

    def _build_scratch(self, directive: str, guidance: str) -> str:
        self._ws("builder", "working: building the file from scratch…",
                 "working")
        resp = self._call("builder", BUILD_FILE_USER % {
            "directive": directive or "(none — follow the user guidance)",
            "objectives": json.dumps(self.product.get("objectives", []),
                                     indent=1),
            "evidence": self._evidence(),
            "guidance": ("USER GUIDANCE:\n" + guidance) if guidance
                        else ""})
        name = str(resp.get("filename") or "").strip()
        src = resp.get("file")
        if not isinstance(src, str) or not src.strip():
            raise RuntimeError("builder returned no file")
        if self.product.get("name"):
            name = self.product["name"]
        name = name or "product.py"
        self.product["gate"], self.product["gate_detail"] = "n/a", ""
        if name.endswith(".py"):
            ok, err = self._compile_src(src)
            if not ok:
                self._ws("builder",
                         f"\n[compile failed — one repair attempt]\n"
                         f"{err}\n", "append")
                fix = self._call("builder", BUILD_FIX_USER % {
                    "error": err, "file": src})
                src = (fix.get("file") if isinstance(fix.get("file"), str)
                       and fix.get("file").strip() else src)
                ok, err = self._compile_src(src)
            self.product["gate"] = "pass" if ok else "fail"
            self.product["gate_detail"] = "" if ok else err
            if not ok:
                self._ws("builder", f"\n[gate FAIL]\n{err}\n", "append")
            else:
                exec_ok, exec_out = self._gate_exec(src, [])
                self.product["gate"] = "pass" if exec_ok else "fail"
                self.product["gate_detail"] = exec_out[:1500]
                self._ws("builder",
                         f"\n[exec gate] {'ok' if exec_ok else 'FAIL'} — "
                         f"{exec_out[:400]}\n", "append")
        self.product["name"] = name
        self.product["source"] = src
        self.product["summary"] = str(resp.get("summary", ""))[:1500]
        self.product["diff"] = ""
        self.product["touched"] = []
        self._persist_product()
        self._ws("builder",
                 f"── {name} · gate {self.product['gate']} · {now_iso()} "
                 f"──\n{src}")
        self._role_status(
            "builder",
            f"COMPLETED · {name} · gate {self.product['gate']}\n\n"
            f"{self.product['summary'] or 'Built the delegated file.'}")
        return src

    def _build_changeset(self, directive: str, guidance: str,
                         base: str) -> str:
        name = self.product.get("name") or "product.py"
        skeleton = module_skeleton(base)
        # plan phase only when the base is too large to show whole
        code, code_label = base, " (complete file)"
        if len(base) > MAX_INLINE_SRC:
            self._ws("builder", "working: planning targets from the "
                                "skeleton…", "working")
            kg = self.kg()
            rel = self.product.get("rel") or ""
            kg_plan = "(no graph memory)"
            if kg and rel:
                allnames = ([f"{c}.{f}" for c, cd in kg.graph.get(
                                rel, {}).get("classes", {}).items()
                             for f in cd["functions"]]
                            + list(kg.graph.get(rel, {}).get(
                                "functions", {})))
                kg_plan = kg.context_for(rel, allnames[:30])
            plan = self._call("builder", PLAN_TARGETS_USER % {
                "directive": directive or "(follow the user guidance)",
                "objectives": json.dumps(
                    self.product.get("objectives", []), indent=1),
                "base_name": name, "skeleton": skeleton, "kg": kg_plan,
                "evidence": self._evidence(),
                "guidance": ("USER GUIDANCE:\n" + guidance)
                            if guidance else ""})
            targets = ([str(t) for t in plan.get("change_targets") or []]
                       + [str(t) for t in plan.get("context_targets")
                          or []])
            segs = []
            for t in targets[:40]:
                seg = segment_for(base, chain_of(t))
                if seg:
                    segs.append(f"### {t}\n{seg}")
            code = ("\n\n".join(segs)
                    or "(no targets resolved — plan from the skeleton)")
            code_label = " (targeted segments only — the change set is " \
                         "applied to the full file)"
            self._ws("builder",
                     f"\n[plan] targets: {', '.join(targets) or '(none)'}"
                     f"\n", "append")
        # graph memory + caller code for the change targets: the seams
        # the change set must keep working, fetched from wherever the
        # callers live — the single-file blindness fix
        kg = self.kg()
        rel = self.product.get("rel") or ""
        kg_ctx, caller_code = "(no graph memory)", ""
        target_names = []
        if len(base) > MAX_INLINE_SRC:
            target_names = [str(t) for t in
                            (plan.get("change_targets") or [])]
        if kg and rel:
            if not target_names:         # small file: whole-module memory
                target_names = ([f"{c}.{f}" for c, cd in kg.graph.get(
                                    rel, {}).get("classes", {}).items()
                                 for f in cd["functions"]]
                                + list(kg.graph.get(rel, {}).get(
                                    "functions", {})))[:30]
            kg_ctx = kg.context_for(rel, target_names)
            segs = kg.caller_segments(rel, target_names[:12])
            if segs:
                caller_code = ("CALLER CODE (graph-adjacent, read-only — "
                               "do NOT modify these; your change must "
                               "keep them working):\n" + segs + "\n")
        did_hint = ""
        if self.desig and rel:
            mod = self.desig.data.get("modules", {}).get(rel)
            if mod:
                did_hint = "P1" + mod.get("id", "")
        pitfalls = self.evo.pitfalls(name) if name else ""
        if did_hint:
            more = self.evo.pitfalls(did_hint)
            if more != "(no recorded pitfalls)":
                pitfalls = (pitfalls + "\n" + more).strip()
        pit_block = (f"KNOWN PITFALLS (distilled from this file's "
                     f"evolution log — do not repeat them):\n"
                     f"{pitfalls}\n"
                     if pitfalls and pitfalls != "(no recorded pitfalls)"
                     else "")
        self._ws("builder", "working: building the change set…", "working")
        repair_note = ""
        candidate = base
        for attempt in range(MAX_REPAIRS + 1):
            self._check()
            resp = self._call("builder", BUILD_CHANGESET_USER % {
                "directive": directive or "(follow the user guidance)",
                "objectives": json.dumps(
                    self.product.get("objectives", []), indent=1),
                "base_name": name, "skeleton": skeleton,
                "kg": kg_ctx, "caller_code": caller_code,
                "code_label": code_label, "code": code,
                "evidence": self._evidence(), "pitfalls": pit_block,
                "repair": (f"PREVIOUS ATTEMPT FAILED THE ORACLE — fix "
                           f"this exact error:\n{repair_note}"
                           if repair_note else ""),
                "guidance": ("USER GUIDANCE:\n" + guidance)
                            if guidance else ""})
            changes = resp.get("changes") or {}
            additions = resp.get("additions") or []
            imports = resp.get("imports") or []
            candidate, applied, errs = base, [], []
            try:
                candidate, added_imports = ensure_imports(candidate,
                                                          imports)
            except (SyntaxError, ValueError) as e:
                errs.append(f"imports: {e}")
                added_imports = []
            for dotted, fcode in changes.items():
                try:
                    candidate = splice(candidate, chain_of(str(dotted)),
                                       str(fcode))
                    applied.append(str(dotted))
                except (ValueError, SyntaxError) as e:
                    errs.append(f"{dotted}: {e}")
            for add in additions:
                scope = str((add or {}).get("scope", ""))
                acode = str((add or {}).get("code", ""))
                try:
                    candidate = add_definition(candidate, scope, acode)
                    applied.append((scope + "." if scope else "")
                                   + "(addition)")
                except (ValueError, SyntaxError) as e:
                    errs.append(f"addition[{scope or 'module'}]: {e}")
            ok, cerr = (self._compile_src(candidate)
                        if name.endswith(".py") else (True, ""))
            if ok and not errs:
                if not (applied or added_imports):
                    self._ws("builder", "\n[no changes proposed this "
                                        "cycle]\n", "append")
                break
            repair_note = ("; ".join(errs)
                           + ("\n" + cerr if cerr else ""))[:2000]
            self._ws("builder",
                     f"\n[oracle rejected attempt {attempt + 1}] "
                     f"{repair_note[:300]}\n", "append")
            candidate = base
        else:
            ok, cerr = False, repair_note
        gate_ok = ok
        detail = "" if ok else (cerr or repair_note)
        touched_chains = [c for c in applied if not
                          c.endswith("(addition)")]
        if gate_ok and (applied or candidate != base):
            exec_ok, exec_out = self._gate_exec(
                candidate, self._touched_designations(touched_chains))
            gate_ok = gate_ok and exec_ok
            detail = exec_out if not exec_ok else \
                (detail + " | " + exec_out).strip(" |")
            self._ws("builder",
                     f"\n[exec gate] {'ok' if exec_ok else 'FAIL'} — "
                     f"{exec_out[:400]}\n", "append")
        self.product["gate"] = "pass" if gate_ok else "fail"
        self.product["gate_detail"] = detail[:1500]
        self.product["source"] = candidate if gate_ok else base
        self.product["name"] = name
        self.product["summary"] = str(resp.get("summary", ""))[:1500]
        self.product["touched"] = applied
        udiff = "\n".join(difflib.unified_diff(
            base.split("\n"),
            self.product["source"].split("\n"),
            "before", "after", lineterm=""))
        self.product["diff"] = udiff[:MAX_DIFF_CTX]
        self._persist_product()
        self._ws("builder",
                 f"── {name} · gate {self.product['gate']} · "
                 f"{len(applied)} change(s) · {now_iso()} ──\n"
                 f"{self.product['source']}")
        self._role_status(
            "builder",
            f"COMPLETED · {name} · gate {self.product['gate']} · "
            f"{len(applied)} targeted change(s)\n\n"
            f"{self.product['summary'] or 'Applied the delegated changes.'}"
            + (("\n\nTouched: " + ", ".join(applied)) if applied else ""))
        self.evo.append(run=self.product.get("run_ts", ""),
                        agent=self._agent_name("builder"),
                        level="file", designation=name,
                        job=f"{len(applied)} change(s) applied",
                        reason=(directive or guidance)[:300],
                        solution_reasoning=self.product["summary"][:400],
                        diff=udiff[:4000],
                        verdict=self.product["gate"], verdict_by="oracle",
                        verdict_reason=detail[:300])
        return self.product["source"]

    def step_review(self, guidance: str = "") -> tuple:
        """Reviewer agent: product vs objectives → verdict + message to
        the mission agent. Shown in its workspace. → (ok, message)."""
        if not self.product.get("source"):
            raise RuntimeError("nothing to review — no product built yet")
        self._role_status(
            "reviewer",
            "CHECKING — Builder result against Mission instructions…",
        )
        self._ws("reviewer", "working: judging product vs objectives…",
                 "working")
        diff = self.product.get("diff") or ""
        if diff:
            view_label = ("unified diff of this cycle's change set "
                          "(applied to the full file)")
            view = diff
        else:
            view_label = "complete file"
            view = self.product["source"][:MAX_DIFF_CTX]
            if len(self.product["source"]) > MAX_DIFF_CTX:
                view_label = (f"first {MAX_DIFF_CTX} chars of "
                              f"{len(self.product['source'])} — "
                              f"judge structure from the gates")
        rev = self._call("reviewer", REVIEW_PRODUCT_USER % {
            "objectives": json.dumps(self.product.get("objectives", []),
                                     indent=1),
            "guidance": ("USER GUIDANCE:\n" + guidance) if guidance
                        else "",
            "gates": (f"compile+exec {self.product.get('gate', 'n/a')}"
                      + (f" — {self.product.get('gate_detail', '')[:300]}"
                         if self.product.get("gate_detail") else "")),
            "summary": self.product.get("summary", "") or "(none)",
            "name": self.product.get("name", "product"),
            "view_label": view_label,
            "file": view})
        status = rev.get("objectives_status") or {}
        for o in self.product.get("objectives", []):
            if o.get("id") in status:
                o["status"] = status[o["id"]]
        self._save_objectives({"objectives":
                               self.product.get("objectives", []),
                               "cycle": self.product.get("cycle", 0)})
        gate_fail = self.product.get("gate") == "fail"
        ok = bool(rev.get("satisfactory")) and not gate_fail
        msg = str(rev.get("message_to_mission", "")).strip()
        notes = str(rev.get("notes", "")).strip()
        newreqs = [str(r).strip() for r in
                   (rev.get("new_requirements") or []) if str(r).strip()]
        if newreqs and not ok:
            # a rejection that mints requirements is worth more than one
            # that mints prose: fold them into the rubric seed and the
            # feedback the mission acts on
            allreqs = (self.product.get("requirements") or []) + newreqs
            self.product["requirements"] = allreqs
            self._seed_spec(allreqs)
            msg = (msg + "\nNEW REQUIREMENTS FROM REVIEW:\n"
                   + "\n".join(newreqs)).strip()
        self._ws("reviewer",
                 f"── {now_iso()} ──\nVERDICT: "
                 + ("satisfactory" if ok else "NOT satisfactory")
                 + (" (compile gate failed)" if gate_fail else "")
                 + f"\n\n{notes}\n"
                 + (("\nNEW REQUIREMENTS\n" + "\n".join(newreqs) + "\n")
                    if newreqs else "")
                 + (f"\nMESSAGE TO MISSION\n{msg}\n" if msg else ""))
        working = self.product.get("working_percentage")
        percentage = (f" · {working}% working"
                      if isinstance(working, (int, float)) else "")
        sound = ", ".join(self.product.get("protected_chains") or [])
        remaining = self.product.get("remaining") or []
        reviewer_report = (
            "COMPLETED · " + ("ALL CLEAR" if ok else "ISSUES FOUND")
            + percentage
            + f"\n\n{msg or notes or self.product.get('gate_detail') or 'No additional detail.'}"
            + (f"\n\nSOUND: {sound}" if sound else "")
            + ("\nREMAINING:\n" + "\n".join(
                f"- {item}" for item in remaining) if remaining else "")
        )
        self._role_status("reviewer", reviewer_report)
        self._ws("mission",
                 f"\n── REPORT FROM REVIEWER · {now_iso()} ──\n"
                 + ("ALL CLEAR" if ok else
                    (msg or notes or self.product.get("gate_detail")
                     or "The build does not yet conform."))
                 + "\n", "append")
        self.evo.append(
            run=self.product.get("run_ts", ""),
            agent=self._agent_name("reviewer"), level="file",
            designation=self.product.get("name", "product"),
            job="Agent 3 checked Builder output",
            reason=(notes or msg)[:500],
            verdict="pass" if ok else "fail",
            verdict_by="reviewer",
            verdict_reason=(msg or notes or
                            self.product.get("gate_detail", ""))[:500])
        return ok, msg

    # ── run ──
    def run(self, seed_feedback: str = "",
            mission_guidance: str = "") -> dict:
        """v5 product cycle over the single workspace product (imported
        base template or scratch): mission → objectives + builder prompt
        (mission workspace) → builder → the complete file (builder
        workspace, compile-gated) → reviewer → verdict (reviewer
        workspace); unsatisfied feedback loops back to the mission agent.
        Bounded by MAX_CYCLES. The product persists at
        agents/workspace/<name>; Approve imports it into the active
        editor on the main page."""
        self._stop = False
        ts = time.strftime("%Y%m%d-%H%M%S")
        self.product["run_ts"] = ts
        feedback, verdict = seed_feedback, "fail"
        cycles = []
        for cycle in range(1, MAX_CYCLES + 1):
            self._check()
            self.product["cycle"] = cycle
            self.emit(f"CYCLE {cycle}\n", "meta")
            if feedback:
                self._say("reviewer", "mission", feedback)
            directive = self.step_mission(
                guidance=mission_guidance if cycle == 1 else "",
                feedback=feedback)
            self._say("mission", "builder", directive or "(no directive)")
            self._check()
            self.step_build()
            self._say("builder", "reviewer",
                      f"{self.product.get('name')} built (gate "
                      f"{self.product.get('gate')}) — over to review")
            self._ws("reviewer",
                     f"── CHECK REQUEST FROM BUILDER · {now_iso()} ──\n"
                     f"File: {self.product.get('name')}\n"
                     f"Gate: {self.product.get('gate')}\n"
                     f"Touched: {', '.join(self.product.get('touched') or []) or '(from scratch)'}\n"
                     "Check syntax/gates and conformity with Mission's objectives.\n")
            self._check()
            if not self._assigned("reviewer"):
                verdict = ("fail" if self.product.get("gate") == "fail"
                           else "pass")
                cycles.append({"cycle": cycle,
                               "gate": self.product.get("gate"),
                               "review": "(reviewer unassigned)"})
                break
            ok, msg = self.step_review()
            cycles.append({"cycle": cycle,
                           "gate": self.product.get("gate"),
                           "satisfactory": ok, "review": msg})
            if ok:
                self._say("reviewer", "mission",
                          "satisfactory — objectives met")
                verdict = "pass"
                self.product["evidence"] = ""   # the error is answered
                break
            feedback = msg or f"gate: {self.product.get('gate')}"
        if verdict == "fail" and self.product.get("gate") == "pass":
            verdict = "pass_with_warnings"   # gate ok, reviewer unsatisfied

        record = {"started": ts, "product": self.product.get("name", ""),
                  "cycles": cycles, "verdict": verdict,
                  "llm_calls": self.llm_calls, "finished": now_iso()}
        arc = self.evo.compact()
        if arc:
            self.emit(f"evolution log compacted → "
                      f"{os.path.basename(arc)}\n", "meta")
        out = os.path.join(self.root, AGENTS_DIR, f"run_{ts}.json")
        atomic_write_json(out, record)
        self.emit(f"RUN ↑ {verdict} after {len(cycles)} cycle(s) · "
                  f"{self.llm_calls} LLM call(s) — "
                  f"iteration of {self.product.get('name') or '(scratch)'} "
                  f"ready (Approve to accept) · "
                  f"{os.path.relpath(out, self.root)}\n",
                  "ok" if verdict != "fail" else "err")
        return record

# ──────────────────────────────────────────────────────────────────────────
# OrchestratorWindow — the agent workspace
# ──────────────────────────────────────────────────────────────────────────

class OrchestratorWindow(tk.Toplevel if tk else object):
    def __init__(self, master, project_dir, desig, api_cfg, chat_fn,
                 theme=None, store=None, on_change=None, open_at=None,
                 get_active=None, apply_active=None):
        super().__init__(master)
        self.title(f"Agent Workspace — {os.path.basename(project_dir)}")
        t = theme or {}
        self.bg = t.get("panel", "#2b2d30")
        self.fg = t.get("panel_fg", "#bbbbbb")
        self.tbg = t.get("bg", "#1e1f22")
        self.tfg = t.get("fg", "#dcdcdc")
        self.accent = t.get("accent", "#3574f0")
        self.configure(bg=self.bg)

        self.project_dir = project_dir
        self.desig = desig
        self.api_cfg = api_cfg
        self.chat_fn = chat_fn
        self.store = store or AgentSpecStore(project_dir)
        if self.store.spec.get("_schema") != AgentSpecStore.SCHEMA:
            self.store.spec = AgentSpecStore(project_dir).spec   # migrated
        self.on_change = on_change
        self.orch: Orchestrator | None = None
        self.live = None
        self.open_at = open_at
        self.get_active = get_active      # () → (name, content[, rel])
        self.apply_active = apply_active  # (name, content) → editor
        self.product: dict = {}           # shared with every Orchestrator
        self.ws: dict = {}                # role → RoleWorkspace
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False                # ONE orchestration at a time:
                                          # run OR a manual role step
        self.ui = UIState()
        persist_geometry(self, self.ui, "agents.window", "1400x900")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.build_tab = tk.Frame(self.nb, bg=self.bg)
        self.nb.add(self.build_tab, text="  Build  ")
        try:                              # Verify IS mounted (converged)
            from verify import VerifyTab
            self.verify_tab = VerifyTab(
                self.nb, project_dir, desig, api_cfg, chat_fn,
                self.store.spec, theme=theme, open_at=open_at)
            self.nb.add(self.verify_tab, text="  Verify  ")
        except Exception as e:  # noqa: BLE001 — Build must survive alone
            self.verify_tab = None
            self.after(200, lambda m=str(e): self._out(
                f"Verify tab unavailable: {m}\n", "err"))

        self._build_mission()
        self._build_roles()
        self._build_roster()
        self._build_controls()
        self._build_log()
        self._load()
        self.after_idle(self._prime_mission_workspace)
        self._poll()
        if getattr(self.store, "scrubbed", 0):
            self._out(f"SECURITY: removed {self.store.scrubbed} plaintext "
                      f"api_key value(s) from spec.json — ROTATE those keys. "
                      f"Keys now live only in ~/.pyedit_config.json "
                      f"(optionally under a \"keys\" map, selected per "
                      f"agent by 'key ref').\n", "err")

    def _lbl(self, parent, text, bold=False):
        return tk.Label(parent, text=text, bg=self.bg, fg=self.fg,
                        font=("Segoe UI", 9, "bold" if bold else "normal"))

    # ── UI ──
    def _build_mission(self):
        frm = tk.LabelFrame(
            self.build_tab,
            text=(
                "Mission (auto-populated from agents/"
                "instructions_mission.md — Tools ▸ Instructions "
                "Setup…; Save Spec writes it back)"
            ),
            bg=self.bg,
            fg=self.fg,
        )
        frm.pack(fill="x", padx=8, pady=(8, 4))

        self.mission_tok_lbl = self._lbl(
            frm,
            "Mission tokens: calculating…",
        )
        self.mission_tok_lbl.pack(
            anchor="w",
            padx=4,
            pady=(4, 0),
        )

        self.mission = tk.Text(
            frm,
            height=4,
            bg=self.tbg,
            fg=self.tfg,
            insertbackground=self.tfg,
            wrap="word",
        )
        self.mission.pack(fill="x", padx=4, pady=4)
        _attach_menu(self.mission)

    def _build_roles(self):
        frm = tk.LabelFrame(
            self.build_tab,
            text="Roles and hand-offs",
            bg=self.bg,
            fg=self.fg,
        )
        frm.pack(fill="x", padx=8, pady=4)

        self.role_agent, self.role_text = {}, {}
        self.role_boxes, self.role_files = {}, {}
        self.role_chat, self.role_entry = {}, {}
        self.role_msgs = {r: [] for r in ROLES}

        for r in ROLES:
            band = tk.Frame(frm, bg=self.bg)
            band.pack(fill="x", padx=4, pady=2)

            left = tk.Frame(band, bg=self.bg)
            left.pack(side="left", fill="both", expand=True)

            row = tk.Frame(left, bg=self.bg)
            row.pack(fill="x")

            self._lbl(
                row,
                r.capitalize(),
                bold=True,
            ).pack(side="left")
            if r == "mission":
                self.mission_tok_lbl = self._lbl(
                    row,
                    "Mission tokens: calculating…",
                )
                self.mission_tok_lbl.pack(
                    side="left",
                    padx=6,
                )
            var = tk.StringVar()

            box = ttk.Combobox(
                row,
                textvariable=var,
                width=24,
                state="readonly",
            )
            box.pack(side="right", padx=4)

            self._lbl(row, "agent:").pack(side="right")
            self.role_agent[r], self.role_boxes[r] = var, box

            workspace_title = {
                "mission": (
                    "Mission workspace — latest instructions_mission.md"
                ),
                "builder": (
                    "Builder workspace — latest delegated result"
                ),
                "reviewer": (
                    "Reviewer workspace — latest check report"
                ),
            }[r]

            workspace_row = tk.Frame(left, bg=self.bg)
            workspace_row.pack(fill="x")

            tk.Button(
                workspace_row,
                text=f"Pop out {r.capitalize()}",
                command=lambda role=r: self._open_role_workspace(role),
                relief="raised",
                bd=1,
                padx=8,
            ).pack(side="right", padx=(6, 0))

            self._lbl(
                workspace_row,
                workspace_title,
            ).pack(side="left", anchor="w")

            height = {
                "mission": 9,
                "builder": 3,
                "reviewer": 4,
            }[r]

            txt = tk.Text(
                left,
                height=height,
                bg=self.tbg,
                fg=self.tfg,
                insertbackground=self.tfg,
                wrap="word",
                state="disabled",
                font=("Consolas", 9),
            )
            txt.pack(fill="x", pady=(0, 2))
            _attach_menu(txt, read_only=True)
            self.role_text[r] = txt

            if r == "mission":
                right = tk.Frame(band, bg=self.bg)
                right.pack(
                    side="right",
                    fill="y",
                    padx=(6, 0),
                )

                self._lbl(
                    right,
                    "project files (Mission context)",
                ).pack(anchor="w")

                lb = tk.Listbox(
                    right,
                    height=4,
                    width=32,
                    bg=self.tbg,
                    fg=self.tfg,
                    exportselection=False,
                )
                lb.pack()

                fb = tk.Frame(right, bg=self.bg)
                fb.pack(fill="x")

                tk.Button(
                    fb,
                    text="+ file",
                    relief="flat",
                    padx=6,
                    command=lambda: self._file_add("mission"),
                ).pack(side="left")

                tk.Button(
                    fb,
                    text="−",
                    relief="flat",
                    padx=6,
                    command=lambda: self._file_remove("mission"),
                ).pack(side="left", padx=4)

                self.role_files[r] = lb

                chatf = tk.Frame(band, bg=self.bg)
                chatf.pack(
                    side="right",
                    fill="y",
                    padx=(6, 0),
                )

                self._lbl(
                    chatf,
                    "talk to Mission",
                ).pack(anchor="w")

                ct = tk.Text(
                    chatf,
                    height=4,
                    width=46,
                    bg=self.tbg,
                    fg=self.tfg,
                    wrap="word",
                    state="disabled",
                    font=("Consolas", 8),
                )
                ct.pack()
                _attach_menu(ct, read_only=True)

                crow = tk.Frame(chatf, bg=self.bg)
                crow.pack(fill="x")

                ce = tk.Entry(
                    crow,
                    bg=self.tbg,
                    fg=self.tfg,
                    insertbackground=self.tfg,
                )
                ce.pack(
                    side="left",
                    fill="x",
                    expand=True,
                )
                _attach_menu(ce)

                ce.bind(
                    "<Return>",
                    lambda _event: self._role_chat_send("mission"),
                )

                tk.Button(
                    crow,
                    text="Send",
                    relief="flat",
                    padx=6,
                    command=lambda: self._role_chat_send("mission"),
                ).pack(side="right", padx=(4, 0))

                self.role_chat[r], self.role_entry[r] = ct, ce

    def _open_role_workspace(self, role):
        """Open the selected agent workspace when requested."""

        w = self._workspace(role)
        if not w:
            return

        if role == "mission":
            mission = self.mission.get(
                "1.0",
                "end-1c",
            ).strip()

            content = (
                    "CANONICAL MISSION — "
                    "agents/instructions_mission.md\n\n"
                    + (mission or "(mission is empty)")
                    + "\n"
            )

        elif role == "builder" and self.product.get("source"):
            content = self.product["source"]

        else:
            box = self.role_text.get(role)

            content = (
                box.get("1.0", "end-1c").strip()
                if box
                else ""
            )

            if not content:
                content = (
                    f"({role} has no workspace output yet)"
                )

        w.set_text(content)
        w.lift()
        w.focus_force()

    def _build_roster(self):
        frm = tk.LabelFrame(self.build_tab, text="Agents", bg=self.bg, fg=self.fg)
        frm.pack(fill="x", padx=8, pady=4)
        self.roster = ttk.Treeview(frm, columns=("provider", "model"),
                                   height=3, selectmode="browse")
        self.roster.heading("#0", text="Name")
        self.roster.heading("provider", text="Provider")
        self.roster.heading("model", text="Model")
        self.roster.column("#0", width=180)
        self.roster.column("provider", width=110)
        self.roster.column("model", width=220)
        self.roster.pack(fill="x", padx=4, pady=4)
        self.roster.bind("<<TreeviewSelect>>", self._roster_select)
        rmenu = tk.Menu(self.roster, tearoff=0)
        rmenu.add_command(label="New agent", command=self._form_reset)
        rmenu.add_command(label="Remove agent",
                          command=self._roster_remove)

        def roster_popup(e):
            iid = self.roster.identify_row(e.y)
            if iid:
                self.roster.selection_set(iid)
                self.roster.focus(iid)
            rmenu.tk_popup(e.x_root, e.y_root)
        self.roster.bind("<Button-3>", roster_popup)
        fields = tk.Frame(frm, bg=self.bg); fields.pack(fill="x", padx=4)
        self.f_name = tk.StringVar(); self.f_provider = tk.StringVar()
        self.f_url = tk.StringVar(); self.f_model = tk.StringVar()
        self.f_key = tk.StringVar()
        self._lbl(fields, "name").pack(side="left", padx=(4, 2))
        e_name = tk.Entry(fields, textvariable=self.f_name, width=14)
        e_name.pack(side="left")
        _attach_menu(e_name)
        self._lbl(fields, "provider").pack(side="left", padx=(6, 2))
        pbox = ttk.Combobox(fields, textvariable=self.f_provider, width=9,
                            values=list(PROVIDERS), state="readonly")
        pbox.pack(side="left")
        pbox.bind("<<ComboboxSelected>>", self._provider_models)
        self._lbl(fields, "base url").pack(side="left", padx=(6, 2))
        e_url = tk.Entry(fields, textvariable=self.f_url, width=24)
        e_url.pack(side="left")
        _attach_menu(e_url)
        self._lbl(fields, "model").pack(side="left", padx=(6, 2))
        self.model_box = ttk.Combobox(fields, textvariable=self.f_model,
                                      width=20,
                                      values=self._cfg_models())
        self.model_box.pack(side="left")   # editable: any model string
        self._lbl(fields, "key ref").pack(side="left", padx=(6, 2))
        e_key = tk.Entry(fields, textvariable=self.f_key, width=10)
        e_key.pack(side="left")
        _attach_menu(e_key)
        brow = tk.Frame(frm, bg=self.bg); brow.pack(fill="x", padx=4, pady=4)
        tk.Button(brow, text="New", command=self._form_reset,
                  relief="flat", padx=8).pack(side="left")
        tk.Button(brow, text="Add / Update", command=self._roster_save,
                  relief="flat", padx=8).pack(side="left", padx=6)
        tk.Button(brow, text="Remove", command=self._roster_remove,
                  relief="flat", padx=8).pack(side="left")
        self._form_reset()   # form always starts as the home API settings

    def _build_controls(self):
        oracle = tk.Frame(self.build_tab, bg=self.bg)
        oracle.pack(fill="x", padx=8, pady=(0, 2))
        self._lbl(oracle, "test cmd (oracle gate 3):").pack(side="left")
        self.test_cmd = tk.StringVar()
        e = tk.Entry(oracle, textvariable=self.test_cmd, width=44)
        e.pack(side="left", padx=4)
        _attach_menu(e)
        self._lbl(oracle, "interpreter:").pack(side="left", padx=(8, 2))
        self.interp = tk.StringVar()
        e_i = tk.Entry(oracle, textvariable=self.interp, width=30)
        e_i.pack(side="left")
        _attach_menu(e_i)
        self.run_gate = tk.BooleanVar(value=True)
        tk.Checkbutton(oracle, text=f"run gate (launch candidate "
                                    f"{Orchestrator.RUN_GATE_SECS}s)",
                       variable=self.run_gate, bg=self.bg, fg=self.fg,
                       selectcolor=self.tbg, activebackground=self.bg
                       ).pack(side="left", padx=(10, 0))

        row = tk.Frame(self.build_tab, bg=self.bg)
        row.pack(fill="x", padx=8, pady=4)
        tk.Button(row, text="Save Spec", command=self._save, relief="flat",
                  padx=8).pack(side="left", padx=6)
        tk.Button(row, text="Live View", command=self._open_live,
                  relief="flat", padx=8).pack(side="left")
        tk.Button(
            row,
            text="↶ Undo evolution",
            command=lambda: self._evolution_move("undo"),
            relief="flat",
            padx=8,
        ).pack(side="left", padx=(6, 0))

        tk.Button(
            row,
            text="↷ Redo evolution",
            command=lambda: self._evolution_move("redo"),
            relief="flat",
            padx=8,
        ).pack(side="left", padx=(2, 0))
        tk.Button(row, text="■ Stop", command=self._stop_run, relief="flat",
                  padx=8).pack(side="right")
        self.run_btn = tk.Button(row, text="▶ Run", command=self._run,
                                 bg=self.accent, fg="white", relief="flat",
                                 padx=10)
        self.run_btn.pack(side="right", padx=6)

        pf = tk.LabelFrame(self.build_tab,
                           text="Active file — imported here and iterated "
                                "in memory; nothing on the main page "
                                "changes until Approve", bg=self.bg,
                           fg=self.fg)
        pf.pack(fill="x", padx=8, pady=(0, 4))
        tk.Button(pf, text="Import Active File", relief="flat", padx=8,
                  command=self._import_active).pack(side="left", padx=4,
                                                    pady=4)
        tk.Button(pf, text="Delete", relief="flat", padx=8,
                  command=self._delete_import).pack(side="left", pady=4)
        tk.Button(pf, text="From Scratch", relief="flat", padx=8,
                  command=self._from_scratch).pack(side="left", padx=4,
                                                   pady=4)
        self.product_lbl = tk.Label(pf, text="(no product)", bg=self.bg,
                                    fg=self.accent, anchor="w")
        self.product_lbl.pack(side="left", padx=10)
        tk.Button(pf, text="Approve → Active File", bg=self.accent,
                  fg="white", relief="flat", padx=10,
                  command=self._approve).pack(side="right", padx=4, pady=4)

    def _build_log(self):
        self.log = tk.Text(self.build_tab, bg=self.tbg, fg=self.tfg,
                           wrap="word", state="disabled", padx=6)
        self.log.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self.log.tag_configure("meta", foreground=self.accent)
        self.log.tag_configure("ok", foreground="#6aab73")
        self.log.tag_configure("err", foreground="#f75464")
        _attach_menu(self.log, read_only=True)

    # ── form ⇄ store ──
    def _load(self):
        s = self.store.spec
        self.mission.delete("1.0", "end")
        txt = load_mission_spec(
            self.project_dir, s.get("mission", ""), migrate=True)
        self.mission.insert("1.0", txt or s.get("mission", ""))
        for r in ROLES:
            rec = s["roles"].get(r, {})
            self.role_agent[r].set(rec.get("agent", ""))
            if r in self.role_files:
                self.role_files[r].delete(0, "end")
                for fp in rec.get("files", []):
                    self.role_files[r].insert("end", fp)
        # Agent 1 owns whole-project continuity. Registered modules are
        # always available in its file box; Agents 2 and 3 get task context
        # only through their delegated workspace payloads.
        mission_files = self.role_files.get("mission")
        if mission_files is not None and self.desig:
            present = set(mission_files.get(0, "end"))
            for rel, mod in self.desig.data.get("modules", {}).items():
                if not mod.get("deleted") and rel not in present:
                    mission_files.insert("end", rel)
        self._set_role_workspace("mission", txt or s.get("mission", ""))
        self._set_role_workspace(
            "builder", "WAITING — Mission has not delegated a build yet.")
        self._set_role_workspace(
            "reviewer", "WAITING — no Builder result has been submitted.")
        self._refresh_mission_token_label()
        self.test_cmd.set(s.get("test_cmd", ""))
        self.interp.set(
            resolve_python_interpreter(s.get("interpreter"))
        )
        self.run_gate.set(bool(s.get("run_gate", True)))
        self._refresh_roster()

    def _collect(self):
        s = self.store.spec
        s["mission"] = self.mission.get("1.0", "end-1c").strip()
        for r in ROLES:
            files = (list(self.role_files[r].get(0, "end"))
                     if r in self.role_files else [])
            previous = s["roles"].get(r, {})
            s["roles"][r] = {
                "agent": self.role_agent[r].get(),
                # The visible boxes are role workspaces, not system-prompt
                # editors. Preserve any existing per-role system prompt.
                "instructions": previous.get("instructions", ""),
                "files": files,
            }
        s["test_cmd"] = self.test_cmd.get().strip()
        s["interpreter"] = resolve_python_interpreter(
            self.interp.get().strip()
        )
        s["run_gate"] = bool(self.run_gate.get())

    def _prime_mission_workspace(self):
        mission = self.mission.get("1.0", "end-1c").strip()
        if mission:
            self._set_role_workspace("mission", mission)
        self._refresh_mission_token_label()

    def _open_mission_workspace(self):
        """Open Mission's readable popout only when the user requests it."""
        mission = self.mission.get("1.0", "end-1c").strip()
        w = self._workspace("mission")
        if w:
            w.set_text(
                "CANONICAL MISSION — agents/instructions_mission.md\n\n"
                + (mission or "(mission is empty)") + "\n")
            w.lift()

    def _refresh_mission_token_label(self, request=None, retained=None):
        """Approximate tokens using the same four-characters rule as Chat."""
        label = getattr(self, "mission_tok_lbl", None)
        if not label:
            return
        if request is not None:
            label.config(text=(f"Mission tokens: ≈{int(request):,} last request"
                               f" · ≈{int(retained or 0):,} retained"))
            return
        chars = len(self.mission.get("1.0", "end-1c"))
        role = self.store.spec.get("roles", {}).get("mission", {})
        chars += len(role.get("instructions", "") or DEFAULT_SYSTEM["mission"])
        try:
            with open(os.path.join(self.project_dir, AGENTS_DIR,
                                   "mission_session.json"),
                      "r", encoding="utf-8") as f:
                session = json.load(f)
            chars += sum(len(m.get("content", ""))
                         for m in session.get("messages", [])
                         if isinstance(m, dict))
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        for fp in role.get("files", []):
            p = fp if os.path.isabs(fp) else os.path.join(self.project_dir, fp)
            try:
                chars += min(os.path.getsize(p), MAX_INLINE_SRC)
            except OSError:
                pass
            if chars >= 640000:
                break
        label.config(text=f"Mission tokens: ≈{(chars + 3) // 4:,} staged")

    def _set_role_workspace(self, role, text, append=False):
        box = self.role_text.get(role)
        if not box:
            return
        box.configure(state="normal")
        if not append:
            box.delete("1.0", "end")
        box.insert("end", text.rstrip() + "\n")
        box.see("end" if append else "1.0")
        box.configure(state="disabled")

    def _refresh_roster(self):
        self.roster.delete(*self.roster.get_children())
        for name, cfg in self.store.spec["agents"].items():
            self.roster.insert("", "end", text=name,
                               values=(cfg.get("provider", ""),
                                       cfg.get("model", "")))
        names = list(self.store.spec["agents"])
        for box in self.role_boxes.values():
            box["values"] = names

    def _home_cfg(self) -> dict:
        """The CURRENT main-page API settings: re-read from
        ~/.pyedit_config.json on every use so the agents tab can never
        show stale values, merged into the shared api_cfg dict so runs
        use them too."""
        try:
            with open(HOME_CFG_PATH, "r", encoding="utf-8") as f:
                self.api_cfg.update(json.load(f))
        except (OSError, json.JSONDecodeError):
            pass
        return self.api_cfg

    def _cfg_models(self):
        """Model choices = the list configured in the main-page API
        Settings ("+ model" there), never hardcoded here."""
        home = self._home_cfg()
        ms = [m for m in (home.get("models") or []) if m]
        m0 = home.get("model", "")
        if m0 and m0 not in ms:
            ms.insert(0, m0)
        return ms

    def _form_reset(self):
        """New agent: blank name, everything else imported from the
        main-page API settings."""
        home = self._home_cfg()
        self.model_box["values"] = self._cfg_models()
        self.f_name.set("")
        self.f_provider.set(home.get("provider", "custom"))
        self.f_url.set(home.get("base_url", ""))
        self.f_model.set(home.get("model", ""))
        self.f_key.set("")
        try:
            self.roster.selection_remove(*self.roster.get_children())
        except tk.TclError:
            pass

    def _roster_select(self, _e):
        sel = self.roster.focus()
        if not sel:
            return
        name = self.roster.item(sel, "text")
        cfg = self.store.spec["agents"].get(name, {})
        home = self._home_cfg()
        self.model_box["values"] = self._cfg_models()
        self.f_name.set(name)
        self.f_provider.set(cfg.get("provider")
                            or home.get("provider", "custom"))
        self.f_url.set(cfg.get("base_url") or home.get("base_url", ""))
        self.f_model.set(cfg.get("model") or home.get("model", ""))
        self.f_key.set(cfg.get("key_ref", ""))

    def _provider_models(self, _e=None):
        home = self._home_cfg()
        vals = self._cfg_models()
        self.model_box["values"] = vals
        p = self.f_provider.get()
        if p == home.get("provider"):
            # home protocol: import its URL and default model
            self.f_url.set(home.get("base_url", ""))
            if vals and self.f_model.get() not in vals:
                self.f_model.set(vals[0])
        else:
            # different protocol: its canonical endpoint, model typed in
            self.f_url.set(PROVIDER_URLS.get(p, self.f_url.get()))

    def _roster_save(self):
        name = self.f_name.get().strip()
        if not name:
            self._out("Agent needs a name.\n", "err")
            return
        rec = {"provider": self.f_provider.get().strip() or "custom",
               "base_url": self.f_url.get().strip(),
               "model": self.f_model.get().strip(),
               "key_ref": self.f_key.get().strip()}
        verb = ("updated" if name in self.store.spec["agents"]
                else "added")
        self.store.spec["agents"][name] = rec
        self._refresh_roster()
        for iid in self.roster.get_children():
            if self.roster.item(iid, "text") == name:
                self.roster.selection_set(iid)
                self.roster.focus(iid)
                break
        self._out(f"Agent '{name}' {verb}: {rec['provider']} · "
                  f"{rec['model'] or '(home model)'} · "
                  f"{rec['base_url'] or '(home url)'}\n", "meta")

    def _roster_remove(self):
        sel = self.roster.focus()
        if not sel:
            return
        self.store.spec["agents"].pop(self.roster.item(sel, "text"), None)
        self._refresh_roster()

    def _file_add(self, r):
        p = filedialog.askopenfilename(parent=self,
                                       initialdir=self.project_dir)
        if not p:
            return
        rel = os.path.relpath(p, self.project_dir)
        entry = p if rel.startswith("..") else rel.replace(os.sep, "/")
        if entry not in self.role_files[r].get(0, "end"):
            self.role_files[r].insert("end", entry)
            if r == "mission":
                self._collect()
                self._refresh_mission_token_label()

    def _file_remove(self, r):
        sel = self.role_files[r].curselection()
        if sel:
            self.role_files[r].delete(sel[0])
            if r == "mission":
                self._collect()
                self._refresh_mission_token_label()

    # ── actions ──
    def _save(self):
        self._collect()
        try:
            self.store.save()
        except OSError as e:
            messagebox.showerror("Save failed", str(e), parent=self)
            return
        # AgentSpecStore.save owns the one canonical mission file.
        self._prime_mission_workspace()
        self._out(f"Spec saved → {self.store.path}\n", "meta")
        if self.on_change:
            self.on_change()

    def _run(self):
        self._collect()
        self.store.save()
        missing = [r for r in ROLES
                   if not self.store.spec["roles"][r].get("agent")]
        if missing:
            self._out(f"Assign agent(s) for: {', '.join(missing)} — the "
                      "coordinated cycle requires Mission, Builder and "
                      "Reviewer.\n", "err")
            return
        if self._busy:
            self._out("An orchestration is already running — Stop it or "
                      "wait for it to finish.\n", "err")
            return
        self._out("Run: agent 1 (mission) triggers the design → build → "
                  "review cycle on the product "
                  f"[{self.product.get('name') or 'scratch'}]…\n", "meta")
        self._busy = True
        self.orch = o = self._orch()
        self.run_btn.config(state="disabled", text="running…")

        seed = (self.product.get("evidence") or "").strip()

        def work():
            try:
                o.run(seed_feedback=seed)
            except OrchestratorStopped:
                self._q.put(("err", "[stopped by user]\n"))
            except Exception as e:  # noqa: BLE001
                self._q.put(("err", f"Run failed: {e}\n"))
            finally:
                self._q.put(("_done", None))
        threading.Thread(target=work, daemon=True).start()

    def _stop_run(self):
        if self.orch:
            self.orch.stop()

    def _evolution_move(self, direction):
        log = EvolutionLog(self.project_dir)

        if direction == "redo":
            ok, message = log.redo()
        else:
            ok, message = log.undo()

        self._out(
            message + "\n",
            "ok" if ok else "meta",
        )

    def _open_live(self):
        if self.live and self.live.winfo_exists():
            self.live.lift()
            return
        self.live = LiveViewWindow(self, self.project_dir, bg=self.bg,
                                   fg=self.fg, tbg=self.tbg, tfg=self.tfg)
        if self.product.get("source"):
            if hasattr(self.live, "load_source"):
                self.live.load_source(
                    self.product.get("rel") or self.product.get("name")
                    or "product.py", self.product["source"], flash=False)
            elif self.product.get("rel"):
                self.live.load(self.product["rel"], flash=False)

    # ── the product ──
    def _orch(self) -> Orchestrator:
        """Fresh Orchestrator bound to the shared product dict (spec may
        have been edited since the last one)."""
        return Orchestrator(self.project_dir, self.desig, self.store.spec,
                            self.api_cfg, self.chat_fn,
                            emit=lambda t, tag=None: self._q.put((tag, t)),
                            product=self.product)

    def _product_lbl(self):
        p = self.product
        self.product_lbl.config(
            text=(f"{p['name']}  ·  base: {p.get('base_name') or '?'}  ·  "
                  f"{len(p.get('source', ''))} chars"
                  + (f"  ·  gate {p['gate']}" if p.get("gate") else "")
                  + (f"  ·  {len(p.get('touched') or [])} change(s)"
                     if p.get("touched") else "")
                  + ("  ·  ⚠ error evidence staged"
                     if p.get("evidence") else "")
                  if p.get("name") else
                  "(nothing imported — Import Active File or From "
                  "Scratch)"))

    def _import_active(self):
        """Base template = the active file on the main page."""
        got = self.get_active() if self.get_active else None
        if not got:
            self._out("No active editor tab on the main page to import.\n",
                      "err")
            return
        name, content = got[0], got[1]
        rel = got[2] if len(got) > 2 else ""
        self.product.clear()
        self.product.update({"name": name, "base_name": name, "rel": rel,
                             "base_hash": hashlib.sha1(
                                 content.encode("utf-8",
                                                "replace")).hexdigest(),
                             "source": content, "objectives": [],
                             "directive": "", "gate": "",
                             "gate_detail": "", "diff": "", "touched": []})
        self._product_lbl()
        self._out(f"Imported active file: {name} ({len(content)} chars) "
                  "— iterated here in memory until Approve.\n", "meta")

    def _delete_import(self):
        """Remove the imported file from the agents tab — the iteration
        in memory is discarded; the main page is untouched."""
        name = self.product.get("name", "")
        if not (name or self.product.get("source")):
            self._out("Nothing imported to delete.\n", "err")
            return
        if not messagebox.askyesno(
                "Delete import",
                f"Remove '{name or '(unnamed)'}' and its iteration from "
                "the agents tab? The file on the main page is "
                "untouched."):
            return
        self.product.clear()
        self._product_lbl()
        self._out(f"Import removed: {name or '(unnamed)'} — the main "
                  "page is unchanged.\n", "meta")

    def _from_scratch(self):
        self.product.clear()
        self.product.update({"name": "", "base_name": "(scratch)",
                             "rel": "", "source": "", "objectives": [],
                             "directive": "", "gate": "",
                             "gate_detail": "", "diff": "", "touched": []})
        self._product_lbl()
        self._out("Building from scratch: empty base — the builder will "
                  "name the file.\n", "meta")

    def _approve(self):
        """Import the completed product into the active file on the main
        page (the editor buffer — you still review and save). A failed
        oracle gate refuses by default; overriding requires an explicit
        confirm."""
        srctxt = self.product.get("source", "")
        if not srctxt:
            self._out("No completed product to approve yet.\n", "err")
            return
        # base drift: if the active file changed since import, Approve
        # would overwrite edits the agents never saw
        bh = self.product.get("base_hash")
        if bh and self.get_active:
            got = self.get_active()
            if got and hashlib.sha1(
                    got[1].encode("utf-8", "replace")).hexdigest() != bh:
                if not messagebox.askyesno(
                        "Base drifted",
                        "The active file CHANGED after it was imported "
                        "here — this iteration was built against the old "
                        "version, and approving will overwrite your "
                        "newer edits in the buffer.\n\nApprove anyway?",
                        parent=self):
                    self._out("Approve refused: the active file drifted "
                              "since import (re-import to rebuild on the "
                              "current version).\n", "err")
                    return
        gate = self.product.get("gate", "")
        if gate == "fail":
            detail = (self.product.get("gate_detail") or "")[:300]
            if not messagebox.askyesno(
                    "Gate failed",
                    "The oracle gate FAILED for this iteration"
                    + (f":\n\n{detail}" if detail else ".")
                    + "\n\nApprove it into the editor anyway?",
                    parent=self):
                self._out("Approve refused: gate fail (override "
                          "declined).\n", "err")
                return
            self._out("Gate fail OVERRIDDEN by user — approving "
                      "anyway.\n", "err")
        name = self.product.get("name") or "product.py"
        if self.apply_active:
            applied = self.apply_active(
                name,
                srctxt,
                self.product.get("rel", ""),
            )
            if applied is False:
                self._out(
                    f"Approval failed: {name} was not written to the active file.\n",
                    "err",
                )
                return

            self._out(
                f"Approved: {name} written to the active file.\n",
                "ok",
            )
        else:
            p = os.path.join(self.project_dir, name)
            atomic_write(p, srctxt)
            self._out(f"Approved: written to {p}\n", "ok")
            if self.open_at:
                self.open_at(p, 1)

    # ── run-error intake (pyedit's 'Send Last Error to Agents') ──
    def receive_error(self, tb: str, name: str | None = None,
                      content: str | None = None, rel: str = ""):
        """Stage a runtime traceback as oracle evidence. If the file the
        exception rose in was resolved, it becomes the product base
        (open-buffer content preferred). With mission+builder assigned
        and nothing running, the fix cycle starts immediately: the
        evidence seeds the mission's feedback AND rides verbatim into
        every builder prompt; the run gate then re-launches the
        candidate, so a fix that still crashes at startup never reaches
        Approve. The evidence clears itself on a satisfactory verdict."""
        try:
            self.nb.select(self.build_tab)
            self.lift()
        except tk.TclError:
            pass
        if content is not None and name:
            self.product.clear()
            self.product.update({"name": name, "base_name": name,
                                 "rel": rel, "source": content,
                                 "base_hash": hashlib.sha1(
                                     content.encode(
                                         "utf-8",
                                         "replace")).hexdigest(),
                                 "objectives": [], "directive": "",
                                 "gate": "", "gate_detail": "",
                                 "diff": "", "touched": []})
            self._out(f"Imported {name} from the runtime traceback "
                      f"(the frame the exception rose in).\n", "meta")
        elif not self.product.get("source"):
            self._out("Traceback names no project file and nothing is "
                      "imported — Import Active File first.\n", "err")
        self.product["evidence"] = tb[:6000]
        self._product_lbl()
        self._out("RUNTIME ERROR EVIDENCE staged:\n"
                  + "\n".join(tb.splitlines()[-6:]) + "\n", "err")
        missing = [r for r in ("mission", "builder")
                   if not self.store.spec["roles"][r].get("agent")]
        if missing:
            self._out(f"Assign agent(s) for {', '.join(missing)}, then "
                      f"▶ Run — the evidence stays staged.\n", "meta")
            return
        if self._busy:
            self._out("An orchestration is running — the evidence is "
                      "staged for the next Run.\n", "meta")
            return
        if self.product.get("source"):
            self._run()

    # ── per-role workspaces ──
    def _workspace(self, role) -> "RoleWorkspace":
        w = self.ws.get(role)

        if w and w.winfo_exists():
            return w

        w = RoleWorkspace(
            self,
            role,
            self.store.spec["roles"][role].get(
                "agent",
                "",
            ),
            self.bg,
            self.fg,
            self.tbg,
            self.tfg,

            # Mission and Reviewer deliberately receive no approval button.
            on_approve=(
                self._approve
                if role == "builder"
                else None
            ),
        )

        self.ws[role] = w
        return w

    # ── per-role agent chat: manual operation of each agent ──
    def _chat_out(self, role, line):
        t = self.role_chat.get(role)
        if not t:
            return
        t.configure(state="normal")
        t.insert("end", line)
        t.see("end")
        t.configure(state="disabled")

    def _role_chat_send(self, role):
        """Manually operate this agent: your message is guidance for its
        pipeline step; its tangible product appears in its workspace
        window (mission: objectives + delegated prompt; builder: the
        file; reviewer: the assessment)."""
        if role != "mission":
            self._out("User chat is routed through Mission; Builder and "
                      "Reviewer are delegated workers.\n", "meta")
            return
        text = self.role_entry[role].get().strip()
        if not text:
            return
        if self._busy:
            self._chat_out(role, "[busy: an orchestration is already "
                                 "running — Stop it or wait]\n")
            return
        self._collect()
        self.role_entry[role].delete(0, "end")
        self._chat_out(role, f"you: {text}\n")
        if not self.store.spec["roles"][role].get("agent"):
            self._chat_out(role, "[no agent assigned to this role]\n")
            return
        missing = [r for r in ROLES
                   if not self.store.spec["roles"][r].get("agent")]
        if missing:
            self._chat_out(
                role, "[assign all three roles before Mission coordinates: "
                + ", ".join(missing) + "]\n")
            return
        self._busy = True
        self.orch = o = self._orch()

        def w():
            try:
                if role == "mission":
                    record = o.run(mission_guidance=text)
                    out = (f"coordinated Mission → Builder → Reviewer; "
                           f"result {record.get('verdict')} — see workspaces")
                elif role == "builder":
                    s = o.step_build(guidance=text)
                    out = (f"built {o.product.get('name')} ({len(s)} "
                           f"chars, gate {o.product.get('gate')}) — see "
                           f"my workspace")
                else:
                    ok, msg = o.step_review(guidance=text)
                    out = ("satisfactory" if ok else
                           f"not satisfactory — {(msg or 'see workspace')[:200]}")
                self._q.put(("_chat", (role, f"{role}: {out}\n")))
            except OrchestratorStopped:
                self._q.put(("_chat", (role, "[stopped]\n")))
            except Exception as e:  # noqa: BLE001
                self._q.put(("_chat", (role, f"[error: {e}]\n")))
            finally:
                self._q.put(("_busy_clear", None))
        threading.Thread(target=w, daemon=True).start()

    # ── log plumbing ──
    def _out(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _poll(self):
        try:
            while True:
                tag, payload = self._q.get_nowait()
                if tag == "_done":
                    self._busy = False
                    self.run_btn.config(state="normal", text="▶ Run")
                    if self.on_change:
                        self.on_change()
                elif tag == "_busy_clear":
                    self._busy = False
                elif tag == "_ws":
                    role, mode, text = payload
                    # Builder remains a live code popout. Mission updates its
                    # popout only if the user opened it. Reviewer never opens
                    # a separate window; its compact report is in-tab.
                    if role == "builder":
                        w = self._workspace(role)
                    else:
                        w = self.ws.get(role)
                        if w and not w.winfo_exists():
                            w = None
                    if w:
                        (w.working if mode == "working" else
                         w.append if mode == "append" else
                         w.set_text)(text)
                elif tag == "_mission_spec":
                    self.mission.delete("1.0", "end")
                    self.mission.insert("1.0", payload)
                    self.store.spec["mission"] = payload
                    self._set_role_workspace("mission", payload)
                    self._refresh_mission_token_label()
                elif tag == "_mission_tokens":
                    request, retained = payload
                    self._refresh_mission_token_label(request, retained)
                elif tag == "_role_status":
                    role, text = payload
                    self._set_role_workspace(role, text)
                elif tag == "_product":
                    self.product["name"] = payload[0]
                    self.product["source"] = payload[1]
                    self._product_lbl()
                elif tag == "_chat":
                    self._chat_out(*payload)
                elif tag == "_working":
                    if self.live and self.live.winfo_exists():
                        self.live.working(payload)
                elif tag == "_filechg":
                    if self.live and self.live.winfo_exists():
                        self.live.load(payload)
                else:
                    self._out(payload, tag)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)


# ──────────────────────────────────────────────────────────────────────────
# LiveViewWindow — watch the file change as gated code is promoted
# ──────────────────────────────────────────────────────────────────────────

class RoleWorkspace(tk.Toplevel if tk else object):
    """Optional Mission workspace or live Builder workspace.

    Mission opens only when requested. Builder opens while it works and
    carries the Approve button that imports the completed file into the
    active file on the main page. Reviewer reports stay in the Agents tab.
    """

    def __init__(self, master, role, agent, bg, fg, tbg, tfg,
                 on_approve=None):
        super().__init__(master)
        self.title(f"{role} workspace — {agent or '(unassigned)'}")
        self.configure(bg=bg)
        persist_geometry(self, getattr(master, "ui", None) or UIState(),
                         f"agents.ws.{role}", "780x640")
        top = tk.Frame(self, bg=bg)
        top.pack(fill="x")
        self.status = tk.Label(top, text="idle", bg=bg, fg=fg, anchor="w")
        self.status.pack(side="left", padx=8, pady=4)
        if on_approve:
            tk.Button(top, text="Approve → Active File",
                      command=on_approve, relief="flat",
                      padx=10).pack(side="right", padx=8, pady=4)
        wrap = tk.Frame(self, bg=bg)
        wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.text = tk.Text(wrap, bg=tbg, fg=tfg, wrap="none",
                            font=("Consolas", 10), state="disabled",
                            undo=False)
        ys = tk.Scrollbar(wrap, command=self.text.yview)
        self.text.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        _attach_menu(self.text, read_only=True)

    def _put(self, s, clear):
        self.text.configure(state="normal")
        if clear:
            self.text.delete("1.0", "end")
        self.text.insert("end", s)
        self.text.see("end")
        self.text.configure(state="disabled")

    def set_text(self, s):
        self.status.config(text=f"updated {now_iso()}")
        self._put(s, clear=True)

    def append(self, s):
        self._put(s, clear=False)

    def working(self, msg):
        self.status.config(text=msg)
        self.append(f"\n[{msg}]\n")


class LiveViewWindow(tk.Toplevel if tk else object):
    def __init__(self, master, project_dir, bg, fg, tbg, tfg):
        super().__init__(master)
        self.title("Live View")
        self.configure(bg=bg)
        persist_geometry(self, getattr(master, "ui", None) or UIState(),
                         "agents.liveview", "760x680")
        self.project_dir = project_dir
        self.current = None
        self.prev = ""
        self.header = tk.Label(self, text="(no file loaded)", bg=bg, fg=fg,
                               anchor="w", font=("Segoe UI", 9, "bold"))
        self.header.pack(fill="x", padx=8, pady=(6, 2))
        self.text = tk.Text(self, bg=tbg, fg=tfg, font=("Consolas", 10),
                            wrap="none", state="disabled", padx=6)
        self.text.pack(fill="both", expand=True, padx=8, pady=(0, 2))
        self.text.tag_configure("chg", background="#1f3a26")
        self.stat = tk.Label(self, text="waiting…", bg=bg, fg="#3574f0",
                             anchor="w")
        self.stat.pack(fill="x", padx=8, pady=(0, 6))

    def working(self, unit):
        self.stat.config(text=f"working: {unit}")

    def load(self, rel, flash=True):
        path = os.path.join(self.project_dir, rel.replace("/", os.sep))
        try:
            with open(path, "r", encoding="utf-8") as f:
                new = f.read()
        except OSError as e:
            self.stat.config(text=f"cannot read {rel}: {e}")
            return
        old = self.prev if rel == self.current else ""
        self.current, self.prev = rel, new
        self.header.config(text=rel)
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", new)
        first = None
        if flash and old:
            sm = difflib.SequenceMatcher(None, old.split("\n"),
                                         new.split("\n"))
            for op, _i1, _i2, j1, j2 in sm.get_opcodes():
                if op in ("replace", "insert"):
                    first = first or j1 + 1
                    for ln in range(j1 + 1, j2 + 1):
                        self.text.tag_add("chg", f"{ln}.0", f"{ln}.end")
        self.text.configure(state="disabled")
        if first:
            self.text.see(f"{first}.0")
            self.stat.config(text=f"updated: {rel} (line {first})")
        else:
            self.stat.config(text=f"loaded: {rel}")

"""
verify.py — the Verify tab: designation-level specification, execution-gated
verification, and threshold-driven automated iteration.

How the json files work together here:
  designations.json  addresses every part and carries the verification
                     ledger (per-entity "verified" block: status, rubric
                     score, failed requirement ids, code/spec hashes).
  instructions/<designation>.md
                     the semantic spec per designation — numbered
                     requirements (R1., R2., …) form the rubric. Drafted by
                     the API from code + the persisted chat record + parent
                     guidelines; the user edits and CONFIRMS. Unconfirmed
                     (draft) specs cap a designation at amber, never green.
  evolution.json     the revision memory — sliced per designation and fed
                     into repair directives so retries know what was tried.
  kg_mappings.json   pathway context — designations sharing a mapping row
                     are neighbours: their signatures ride into review
                     context, and a promoted change demotes green
                     neighbours to stale-neighbour for re-verification.

Verdict rule (the oracle keeps final authority):
  green  = rubric score ≥ threshold  AND  every harness-executed
           requirement passed  AND  the module byte-compiles  AND  the
           spec is confirmed.   LLM interpretation alone never mints green.
  amber  = passing but on a draft spec, or harness could not run.
  red    = below threshold after ≤ max_revisions repair attempts.
  stale / stale-neighbour / manual / unverified as displayed states.

The harness is generated FROM the rubric (requirements become asserts),
executed in a temp working directory (module files copied in, timeout).
NOTE: file isolation only — harness code runs with normal user
privileges; third-party services are imitated by agent-built
pseudo-plugins injected via sys.modules before import. Windows and Linux:
plain subprocess + tempfile throughout.

Repair is SHADOW-FIRST: agents.splice + common.gate_compile produce a
candidate that is judged in memory (harness runs against it through
override_src). The real file is promoted (backup-then-promote,
atomically) only AFTER the final result passes the oracle; a candidate
that stays red is discarded and logged, never left on disk. Harness
execution goes through common.run_harness_sandbox — the SAME oracle the
Orchestrator's gate 3 calls; these harnesses guard Build runs too.
A verified block carries "why" so amber/red is always explainable in
the tree, and a harness that produces no results is regenerated once
instead of pinning its designation at amber.

v7 circulation: staleness IS the work queue — run(stale_only=True) /
the ▶ Verify Stale button verifies only what is unverified, red,
demoted, or code-moved, making verification proportional to change.
Neighbour demotion is driven by COMPUTED call edges (KGMemory) with
mapping rows as a supplement. Harnesses are first-class oracle
components: freezable from the tree menu (never regenerated while
frozen), with every regeneration diffed into the evolution log. The
harness prompt carries a Tkinter widget-walk convention so
interaction-driven GUI errors are gateable without a human click.
Spec drafting reads the three newest chat records, not just the last.
Repair prompts carry pitfalls distilled across ALL past runs. tkinter
is OPTIONAL: without it this module still exports the full Verifier
for kernel.py.
"""

from __future__ import annotations

import ast
import difflib
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:          # headless kernel: Verifier only
    tk = ttk = messagebox = None

from common import (atomic_write, attach_context_menu, extract_json,
                    gate_compile, now_iso, promote, run_harness_sandbox,
                    segment_for, sha1_text)
from agents import AGENTS_DIR, EvolutionLog, splice

INSTR_DIR = "instructions"
HARNESS_SUB = "harness"          # under agents/
VERIF_SUB = "verification"       # under agents/
REQ_RE = re.compile(r"^R(\d+)[.:)]\s*(.+)$", re.M)
HARNESS_TIMEOUT = 90
MAX_SEG = 20000

STATUS_COLOR = {"green": "#6aab73", "amber": "#f0a732", "red": "#f75464",
                "stale": "#c77dbb", "stale-neighbour": "#9aa7ff",
                "manual": "#2aacb8", "unverified": "#7a7e85",
                "no-spec": "#606366"}

INSTR_TEMPLATE = """# {designation} — {name}

## Purpose
(what this part is for)

## Requirements
R1. (numbered, testable requirements — these ARE the rubric)
R2. …

## Inputs / Outputs
(arguments, returns, side effects)

## Invariants / Connections
(what must stay true; which other designations this touches)
"""

DRAFT_PROMPT = """Write a designation-level specification for one code
designation. The chat record shows what the user originally asked for —
requirements must reflect INTENT from the chat, not merely paraphrase the
implementation. Number requirements R1., R2., … — each one concrete and
individually checkable. Respond with ONLY the markdown document, following
this template:

{template}

DESIGNATION: {designation} ({name})
PARENT GUIDELINES (coherency constraints — do not contradict):
{guidelines}
CHAT RECORD (user intent):
{chat}
CODE:
{code}
"""

HARNESS_PROMPT = """Write a standalone Python test harness for ONE code
designation. Respond with ONLY Python code, no fences, no prose.

Contract (strict):
- The module under test sits in the SAME directory: import it by module
  name ({modname}).
- Test each requirement below that is executable. For each tested
  requirement print exactly one JSON line:
  print(json.dumps({{"req": "R1", "pass": True_or_False, "detail": "..."}}))
- Wrap every check in try/except; an exception = pass False with the
  traceback text in detail. The harness itself must always exit 0.
- No network, no GUI mainloop, no user input, deterministic.
- If the code imports third-party or side-effecting services (twilio,
  requests, serial, smtplib, …), build a PSEUDO-PLUGIN: a fake module or
  class imitating the used interface, injected via
  sys.modules["<name>"] = <fake>  BEFORE importing the module under test.
- Do not test requirements that cannot be executed (style, documentation);
  simply omit them.
- TKINTER CODE: interaction errors are testable headfully. If the module
  builds a Tk UI, add one widget-walk check: create the app with its root
  withdrawn (root.withdraw()), pump root.update(), then walk
  winfo_children() recursively and for every Button call .invoke(), for
  every widget with bindings fire representative events via
  event_generate, pumping update() between actions, each action in its
  own try/except (a TclError or any exception = pass False with the
  widget path and traceback in detail), then root.destroy(). This is how
  'unknown option -bg on click' class errors are caught without a human.

DESIGNATION: {designation}  ({qual} in {rel})
REQUIREMENTS:
{reqs}
CODE UNDER TEST:
{code}
"""

REVIEW_PROMPT = """You are an independent verifier. Judge the code against
each numbered requirement. Give line-anchored findings ONLY where something
is wrong. Respond ONLY with JSON:
{{"requirements": {{"R1": {{"met": true_or_false, "finding": "…line N…"}}}},
 "summary": "one paragraph: what it does and does not do",
 "improvements": "how it could be improved relative to the spec"}}

DESIGNATION: {designation}
PARENT GUIDELINES:
{guidelines}
NEIGHBOUR CONTEXT (connected parts — treat their behaviour as theirs):
{neighbours}
REQUIREMENTS:
{reqs}
CODE:
{code}
"""

REPAIR_PROMPT = """Repair ONE function so it meets its failed requirements.
Respond ONLY with JSON:
{{"code": "the complete amended function definition",
 "reasoning": "why this fixes the failures"}}

DESIGNATION: {designation}
FAILED REQUIREMENTS:
{failed}
EXECUTION EVIDENCE (harness output / tracebacks — fix these exactly):
{evidence}
PRIOR ATTEMPTS (do not repeat what already failed):
{history}
PARENT GUIDELINES:
{guidelines}
CURRENT CODE:
{code}
"""


def safe_name(designation: str) -> str:
    return re.sub(r"[^\w().\-]", "_", designation)


def iter_designations(desig) -> list:
    """Flatten designations.json → [{designation, rel, chain, kind, name,
    ent, level}] in tree order. P1 prefix per pyedit convention."""
    out = []
    for rel, mod in desig.data.get("modules", {}).items():
        if mod.get("deleted"):
            continue
        out.append({"designation": f"P1{mod['id']}", "rel": rel, "chain": [],
                    "kind": "M", "name": rel, "ent": mod, "level": "module"})

        def walk(rec, chain, prefix):
            for cname, c in rec.get("classes", {}).items():
                if c.get("deleted"):
                    continue
                ch2 = chain + [("C", cname)]
                out.append({"designation": prefix + c["id"], "rel": rel,
                            "chain": ch2, "kind": "C", "name": cname,
                            "ent": c, "level": "class"})
                walk(c, ch2, prefix + c["id"])
            for fname, f in rec.get("functions", {}).items():
                if f.get("deleted"):
                    continue
                ch2 = chain + [("F", fname)]
                out.append({"designation": prefix + f["id"], "rel": rel,
                            "chain": ch2, "kind": "F", "name": fname,
                            "ent": f, "level": "function"})
                walk(f, ch2, prefix + f["id"])
        walk(mod, [], f"P1{mod['id']}")
    return out


class VerifierStopped(Exception):
    pass


# ──────────────────────────────────────────────────────────────────────────
# Verifier — headless engine
# ──────────────────────────────────────────────────────────────────────────

class Verifier:
    def __init__(self, project_dir: str, desig, api_cfg: dict, chat_fn,
                 spec: dict, emit, threshold: int = 80, max_rev: int = 5,
                 repair: bool = False):
        self.root = os.path.abspath(project_dir)
        self.desig = desig
        self.api_cfg = api_cfg
        self.chat_fn = chat_fn
        self.spec = spec or {}
        self.emit = emit
        self.threshold = threshold
        self.max_rev = max_rev
        self.repair = repair
        self.evo = EvolutionLog(project_dir)
        self._stop = False
        self._kg = None
        self.llm_calls = 0

    def kg(self):
        if self._kg is None:
            try:
                from kgraph import KGMemory
                self._kg = KGMemory(self.root, self.desig.data)
            except Exception:  # noqa: BLE001
                self._kg = False
        return self._kg or None

    def stop(self):
        self._stop = True

    def _check(self):
        if self._stop:
            raise VerifierStopped()

    # ── paths / io ──
    def instr_path(self, designation: str) -> str:
        return os.path.join(self.root, INSTR_DIR,
                            safe_name(designation) + ".md")

    def harness_path(self, designation: str) -> str:
        return os.path.join(self.root, AGENTS_DIR, HARNESS_SUB,
                            safe_name(designation) + "_test.py")

    def _read(self, path: str, cap: int = 8000) -> str:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()[:cap]
        except OSError:
            return ""

    def _module_source(self, rel: str) -> str:
        return self._read(os.path.join(self.root, rel.replace("/", os.sep)),
                          cap=10 ** 7)

    # ── LLM (stop-responsive, same pattern as agents) ──
    def _llm(self, prompt: str, system: str, want_json: bool = True):
        self._check()
        box: dict = {}

        def w():
            try:
                box["r"] = self.chat_fn(dict(self.api_cfg),
                                        [{"role": "user", "content": prompt}],
                                        system=system)
            except Exception as e:  # noqa: BLE001
                box["e"] = e
        t = threading.Thread(target=w, daemon=True)
        t.start()
        while t.is_alive():
            t.join(0.5)
            if self._stop:
                raise VerifierStopped()
        if "e" in box:
            raise box["e"]
        self.llm_calls += 1
        return extract_json(box["r"]) if want_json else box["r"]

    # ── spec / rubric ──
    def read_spec(self, designation: str) -> tuple:
        # full read — the hash must equal confirm_spec's hash of the full
        # editor text, or green caching breaks on specs longer than a cap
        text = self._read(self.instr_path(designation), cap=200000)
        reqs = {f"R{m.group(1)}": m.group(2).strip()
                for m in REQ_RE.finditer(text)}
        return text, reqs

    def draft_instructions(self, item: dict) -> str:
        seg = (segment_for(self._module_source(item["rel"]), item["chain"],
                           MAX_SEG) if item["chain"]
               else self._module_source(item["rel"])[:MAX_SEG])
        md = self._llm(DRAFT_PROMPT.format(
            template=INSTR_TEMPLATE.format(designation=item["designation"],
                                           name=item["name"]),
            designation=item["designation"], name=item["name"],
            guidelines=self.guidelines(item), chat=self.chat_context(),
            code=seg or "(unavailable)"),
            system="Respond with ONLY a markdown document.", want_json=False)
        atomic_write(self.instr_path(item["designation"]), md)
        v = item["ent"].setdefault("verified", {})
        v["spec_state"] = "draft"
        v["spec_hash"] = sha1_text(md)
        self.desig.save()
        return md

    def confirm_spec(self, item: dict, text: str):
        atomic_write(self.instr_path(item["designation"]), text)
        v = item["ent"].setdefault("verified", {})
        v["spec_state"] = "confirmed"
        v["spec_hash"] = sha1_text(text)
        self.desig.save()

    # ── context assembly ──
    def guidelines(self, item: dict) -> str:
        """Ancestor instruction files, read-only coherency constraints:
        project (P1) → module → enclosing classes."""
        chain_ids = ["P1"]
        mod = self.desig.data["modules"].get(item["rel"], {})
        if mod:
            chain_ids.append("P1" + mod.get("id", ""))
        prefix = "P1" + mod.get("id", "")
        node = mod
        for kind, name in item["chain"][:-1]:
            node = node.get("classes" if kind == "C" else "functions",
                            {}).get(name)
            if not node:
                break
            prefix += node["id"]
            chain_ids.append(prefix)
        out = []
        for d in chain_ids:
            t = self._read(self.instr_path(d), cap=2500)
            if t:
                out.append(f"--- {d} ---\n{t}")
        return "\n".join(out) or "(none)"

    def chat_context(self) -> str:
        """Intent memory: the tails of the THREE newest chat records, not
        just the last one — long-lived projects carry their intent across
        conversations."""
        cdir = os.path.join(self.root, "chats")
        try:
            names = sorted(os.listdir(cdir))
        except OSError:
            return "(no chat record)"
        if not names:
            return "(no chat record)"
        parts = []
        for n in names[-3:]:
            t = self._read(os.path.join(cdir, n), cap=10 ** 7)
            if t:
                parts.append(f"--- {n} (tail) ---\n{t[-3500:]}")
        return "\n".join(parts)[:11000] or "(no chat record)"

    def neighbours(self, item: dict) -> str:
        """Designations sharing a mapping row: their one-liners ride into
        review context so pathways are judged whole."""
        try:
            with open(os.path.join(self.root, "kg_mappings.json"), "r",
                      encoding="utf-8") as f:
                rows = json.load(f)
        except (OSError, json.JSONDecodeError):
            return "(none)"
        hits = [f"- {r['functionality']}: {r['mapping']}"
                for r in rows if item["name"] in r.get("mapping", "")]
        return "\n".join(hits)[:2000] or "(none)"

    def evolution_slice(self, designation: str, k: int = 4) -> str:
        ents = [e for e in self.evo.entries
                if e.get("designation") == designation][-k:]
        return "\n".join(f"- {e.get('ts','')}: {e.get('verdict','')} — "
                         f"{e.get('job','')[:100]}"
                         for e in ents) or "(no prior attempts)"

    # ── harness ──
    def ensure_harness(self, item: dict, reqs: dict, seg: str,
                       spec_hash: str) -> str:
        """Harnesses are load-bearing oracle components, not disposable
        artifacts: a FROZEN harness (user has read and locked it) is
        never regenerated, and every regeneration of an unfrozen one is
        diffed into the evolution log — a silently-changed oracle can no
        longer mint green unnoticed."""
        p = self.harness_path(item["designation"])
        v = item["ent"].get("verified") or {}
        if os.path.isfile(p) and v.get("harness_frozen"):
            return p                     # human-locked oracle
        if os.path.isfile(p) and v.get("harness_spec") == spec_hash:
            return p
        modname = os.path.splitext(os.path.basename(item["rel"]))[0]
        code = self._llm(HARNESS_PROMPT.format(
            modname=modname, designation=item["designation"],
            qual=".".join(n for _k, n in item["chain"]) or item["name"],
            rel=item["rel"],
            reqs="\n".join(f"{k}. {t}" for k, t in reqs.items()),
            code=seg), system="Respond with ONLY Python code.",
            want_json=False)
        code = re.sub(r"^```[\w]*\n?|```$", "", code.strip(), flags=re.M)
        old_code = self._read(p, cap=10 ** 6) if os.path.isfile(p) else ""
        atomic_write(p, code)
        if old_code and old_code != code:
            hdiff = "\n".join(difflib.unified_diff(
                old_code.split("\n"), code.split("\n"),
                "old_harness", "new_harness", lineterm=""))[:4000]
            self.evo.append(run="", agent="verifier",
                            level=item["level"],
                            designation=item["designation"],
                            job="harness REGENERATED (oracle changed)",
                            reason="spec hash moved",
                            solution_reasoning="", diff=hdiff,
                            verdict="harness", verdict_by="verifier",
                            verdict_reason="review the harness diff — "
                                           "freeze it once trusted")
            self.emit(f"  [{item['designation']}] harness regenerated — "
                      f"diff logged to evolution; freeze it once "
                      f"trusted\n", "meta")
        item["ent"].setdefault("verified", {})["harness_spec"] = spec_hash
        self.desig.save()
        return p

    def run_harness(self, item: dict, module_override: str | None = None,
                    required_ids: tuple | None = None) -> tuple:
        """Thin wrapper over common.run_harness_sandbox — the single
        execution oracle shared with the Orchestrator's gate 3. The
        requirement-id contract (strict booleans, no duplicates, no
        unknown ids, exit 0) is enforced in the runner.
        → ({rid: {pass, detail}}, error)"""
        p = self.harness_path(item["designation"])
        if not os.path.isfile(p):
            return {}, "no harness"
        cp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "common.py")
        return run_harness_sandbox(
            self.root, list(self.desig.data.get("modules", {})), p,
            interpreter=self.spec.get("interpreter"),
            target_rel=item["rel"], override_src=module_override,
            extra_files=(cp,) if os.path.isfile(cp) else (),
            timeout=HARNESS_TIMEOUT, required_ids=required_ids)

    # ── verification of one designation ──
    def verify_one(self, item: dict, run_ts: str) -> dict:
        self._check()
        designation = item["designation"]
        ent = item["ent"]
        v = ent.setdefault("verified", {})
        if v.get("manual"):
            v["status"] = "manual"
            self.desig.save()
            return {"designation": designation, "status": "manual"}
        source = self._module_source(item["rel"])
        seg = (segment_for(source, item["chain"], MAX_SEG)
               if item["chain"] else source[:MAX_SEG]) or ""
        code_hash = sha1_text(seg)
        text, reqs = self.read_spec(designation)
        if not reqs:
            v.update({"status": "no-spec", "ts": now_iso()})
            self.desig.save()
            self.emit(f"  [{designation}] no requirements — draft "
                      f"instructions first\n", "meta")
            return {"designation": designation, "status": "no-spec"}
        spec_hash = sha1_text(text)
        if (v.get("status") == "green" and v.get("code_hash") == code_hash
                and v.get("spec_hash") == spec_hash):
            self.emit(f"  [{designation}] green, unchanged — skipped\n",
                      "meta")
            return {"designation": designation, "status": "green",
                    "cached": True, "score": v.get("score")}

        self.emit(f"{designation}", "_working")
        disk_source = source                 # what is actually on disk
        result = self._judge(item, seg, reqs, source)
        attempts, history = 0, []
        while (self.repair and result["pct"] < self.threshold
               and attempts < self.max_rev):
            attempts += 1
            self._check()
            self.emit(f"  [{designation}] {result['pct']}% < "
                      f"{self.threshold}% — repair attempt {attempts}\n",
                      "meta")
            new_source = self._repair(item, seg, reqs, result, source,
                                      history, run_ts, attempts)
            if new_source is None:
                break
            source = new_source              # SHADOW: judged in memory,
            seg = (segment_for(source, item["chain"], MAX_SEG)   # not on
                   if item["chain"] else source[:MAX_SEG]) or seg  # disk
            result = self._judge(item, seg, reqs, source)
        # promote/discard AFTER the verdict — the oracle keeps final
        # authority: a candidate that still fails never reaches the file
        if source != disk_source:
            passing = (result["pct"] >= self.threshold
                       and not result["harness_err"]
                       and (not result["had_harness"]
                            or result["harness_pass"]))
            if passing:
                self._promote_candidate(item, disk_source, source,
                                        run_ts, attempts, result)
            else:
                source = disk_source
                seg = (segment_for(source, item["chain"], MAX_SEG)
                       if item["chain"] else source[:MAX_SEG]) or ""
                self.evo.append(
                    run=run_ts, agent="verifier-repair",
                    level="function", designation=designation,
                    job=f"{attempts} repair attempt(s) DISCARDED",
                    reason="; ".join(result["failed"])[:200],
                    solution_reasoning="",
                    verdict="discarded", verdict_by="oracle",
                    verdict_reason=f"still {result['pct']}% after "
                                   f"{attempts} attempt(s) — file "
                                   f"untouched")
                self.emit(f"  [{designation}] repairs discarded — still "
                          f"failing; the file was never touched\n", "err")
                result = self._judge(item, seg, reqs, source)
        code_hash = sha1_text(seg)
        status, why = self._status(result, ent)
        pct = result["pct"]
        v.update({"status": status, "score": f"{result['met']}/"
                  f"{result['total']}", "pct": pct, "why": why,
                  "failed": result["failed"], "code_hash": code_hash,
                  "spec_hash": spec_hash, "ts": now_iso(),
                  "summary": result.get("summary", "")[:400],
                  "improvements": result.get("improvements", "")[:400],
                  "findings": result["findings"]})
        self.desig.save()
        self.evo.append(run=run_ts, agent="verifier", level=item["level"],
                        designation=designation,
                        job=f"verified {result['met']}/{result['total']} "
                            f"({pct}%)",
                        reason=f"threshold {self.threshold}%",
                        solution_reasoning=result.get("summary", "")[:300],
                        verdict=status, verdict_by="oracle+verifier",
                        verdict_reason="; ".join(result["failed"])[:200])
        tag = ("ok" if status in ("green", "amber") else "err")
        self.emit(f"  [{designation}] {status.upper()} — "
                  f"{result['met']}/{result['total']} ({pct}%)"
                  + (f" — {why}" if why else "") + "\n", tag)
        return {"designation": designation, "status": status, "pct": pct,
                "score": v["score"], "failed": result["failed"],
                "why": why, "attempts": attempts}

    def _judge(self, item, seg, reqs, source) -> dict:
        """Merge harness execution with independent semantic review.
        Executable requirements take their verdict from EXECUTION."""
        spec_hash = sha1_text(self.read_spec(item["designation"])[0])
        harness_err = ""
        hres = {}
        if item["level"] == "function":
            try:
                self.ensure_harness(item, reqs, seg, spec_hash)
                hres, harness_err = self.run_harness(
                    item, module_override=source,
                    required_ids=tuple(reqs))
                if not hres and harness_err.startswith(
                        "harness produced no results"):
                    # a broken GENERATED harness must not pin this
                    # designation at amber forever (ensure_harness caches
                    # on spec hash) — regenerate once and retry
                    try:
                        os.remove(self.harness_path(item["designation"]))
                    except OSError:
                        pass
                    (item["ent"].setdefault("verified", {})
                     ).pop("harness_spec", None)
                    self.emit(f"  [{item['designation']}] harness gave no "
                              f"results — regenerating once\n", "meta")
                    self.ensure_harness(item, reqs, seg, spec_hash)
                    hres, harness_err = self.run_harness(
                        item, module_override=source,
                        required_ids=tuple(reqs))
            except VerifierStopped:
                raise
            except Exception as e:  # noqa: BLE001
                harness_err = f"harness generation failed: {e}"
        review = self._llm(REVIEW_PROMPT.format(
            designation=item["designation"],
            guidelines=self.guidelines(item),
            neighbours=self.neighbours(item),
            reqs="\n".join(f"{k}. {t}" for k, t in reqs.items()),
            code=seg), system="Respond ONLY with JSON.")
        rrev = review.get("requirements") or {}
        findings, failed, met = [], [], 0
        for rid in reqs:
            if rid in hres:                       # execution wins
                ok = hres[rid]["pass"]
                src_of = "harness"
                detail = hres[rid]["detail"]
            else:
                rv = rrev.get(rid) or {}
                ok = bool(rv.get("met"))
                src_of = "review"
                detail = str(rv.get("finding", ""))
            if ok:
                met += 1
            else:
                failed.append(rid)
            findings.append({"req": rid, "text": reqs[rid][:120],
                             "verdict": "pass" if ok else "fail",
                             "source": src_of, "detail": detail[:300]})
        total = max(len(reqs), 1)
        return {"met": met, "total": total,
                "pct": round(100 * met / total),
                "failed": failed, "findings": findings,
                "harness_err": harness_err,
                "harness_pass": all(r["pass"] for r in hres.values()),
                "had_harness": bool(hres),
                "summary": str(review.get("summary", "")),
                "improvements": str(review.get("improvements", ""))}

    def _status(self, result, ent) -> tuple:
        """→ (status, why). The why rides into verified['why'] and the
        tree column, so a 20/20 amber is never a mystery again."""
        confirmed = (ent.get("verified", {}).get("spec_state") == "confirmed")
        if result["pct"] < self.threshold:
            return "red", f"{result['pct']}% below threshold"
        if result["had_harness"] and not result["harness_pass"]:
            return "red", "harness-executed requirement failed"
        if result["harness_err"]:
            return "amber", f"harness: {result['harness_err'][:120]}"
        if not confirmed:
            return "amber", "spec not confirmed — draft caps at amber"
        return "green", ""

    def _repair(self, item, seg, reqs, result, source, history,
                run_ts, attempt):
        """One SHADOW repair attempt: builder fix → splice → compile
        gate → candidate returned for in-memory judgement. Never writes
        the real file — promotion happens in verify_one only after the
        final verdict passes."""
        if item["level"] != "function":
            return None                      # only leaves are repaired
        evidence = "\n".join(f"{f['req']}: {f['detail']}"
                             for f in result["findings"]
                             if f["verdict"] == "fail") or "(none)"
        try:
            resp = self._llm(REPAIR_PROMPT.format(
                designation=item["designation"],
                failed="\n".join(f"{r}. {reqs[r]}"
                                 for r in result["failed"]),
                evidence=evidence[:2000],
                history=(("\n".join(history[-3:]) + "\nKNOWN PITFALLS "
                          "(distilled across ALL past runs — do not "
                          "repeat):\n"
                          + self.evo.pitfalls(item["designation"]))
                         .strip()),
                guidelines=self.guidelines(item), code=seg),
                system="Respond ONLY with JSON.")
        except VerifierStopped:
            raise
        except Exception as e:  # noqa: BLE001
            self.emit(f"  [{item['designation']}] repair call failed: "
                      f"{e}\n", "err")
            return None
        code = str(resp.get("code", ""))
        if not code.strip():
            return None
        try:
            candidate = splice(source, item["chain"], code)
        except (ValueError, SyntaxError) as e:
            history.append(f"attempt {attempt}: splice rejected ({e})")
            return None
        # compile gate on the candidate before it goes anywhere
        tmp = tempfile.mkdtemp(prefix="pyedit_gate_")
        try:
            tp = os.path.join(tmp, "candidate.py")
            with open(tp, "w", encoding="utf-8") as f:
                f.write(candidate)
            ok, err = gate_compile(tp, self.spec.get("interpreter"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not ok:
            history.append(f"attempt {attempt}: compile gate failed ({err})")
            return None
        udiff = "\n".join(difflib.unified_diff(
            source.split("\n"), candidate.split("\n"),
            "before", "after", lineterm=""))[:4000]
        self.evo.append(run=run_ts, agent="verifier-repair",
                        level="function", designation=item["designation"],
                        job=f"repair attempt {attempt} — candidate built "
                            f"(shadow, not promoted)",
                        reason="; ".join(result["failed"]),
                        solution_reasoning=str(resp.get("reasoning",
                                                        ""))[:300],
                        diff=udiff, verdict="candidate",
                        verdict_by="oracle",
                        verdict_reason="compile ok — awaiting judgement")
        history.append(f"attempt {attempt}: candidate built — "
                       f"{str(resp.get('reasoning', ''))[:120]}")
        self.emit(f"  [{item['designation']}] candidate built (shadow) — "
                  f"judging…\n", "meta")
        return candidate

    def _promote_candidate(self, item, disk_source, candidate, run_ts,
                           attempts, result):
        """The ONLY path a repaired file takes to disk: after the final
        verdict passed the oracle. Backup-then-promote (atomic),
        designation revision bump, neighbour demotion."""
        path = os.path.join(self.root, item["rel"].replace("/", os.sep))
        backup = promote(path, candidate,
                         os.path.join(self.root, AGENTS_DIR, "backups"),
                         item["rel"], f"{run_ts}_v{attempts}")
        udiff = "\n".join(difflib.unified_diff(
            disk_source.split("\n"), candidate.split("\n"),
            "before", "after", lineterm=""))[:4000]
        self.evo.append(run=run_ts, agent="verifier-repair",
                        level="function", designation=item["designation"],
                        job=f"repair promoted after {attempts} "
                            f"attempt(s) — verdict passed",
                        reason="; ".join(result["failed"])[:200] or
                               "(all requirements now pass)",
                        solution_reasoning="",
                        diff=udiff, verdict="promoted", verdict_by="oracle",
                        verdict_reason=f"backup {os.path.basename(backup)}")
        self.desig.sync_module(item["rel"], candidate)   # revision bump
        self._demote_neighbours(item)
        self.emit(f"  [{item['designation']}] repair promoted → "
                  f"{item['rel']} (backup "
                  f"{os.path.basename(backup)})\n", "ok")

    def _demote_neighbours(self, item):
        """Pathway integrity from the GRAPH: a promoted change demotes
        the green designations that actually call (or are called by) it —
        computed call edges, with hand-typed mapping rows as a
        supplement. This is the KG acting as memory with consequences."""
        names = set()
        kg = self.kg()
        if kg:
            dotted = ".".join(n for _k, n in item["chain"])
            names |= kg.neighbour_names(item["rel"], dotted)
        try:
            with open(os.path.join(self.root, "kg_mappings.json"), "r",
                      encoding="utf-8") as f:
                rows = json.load(f)
            related = " ".join(r.get("mapping", "") for r in rows
                               if item["name"] in r.get("mapping", ""))
            names |= {w.strip(",.") for w in related.split()
                      if w.strip(",.")}
        except (OSError, json.JSONDecodeError):
            pass
        if not names:
            return
        for other in iter_designations(self.desig):
            if other["designation"] == item["designation"]:
                continue
            v = other["ent"].get("verified") or {}
            if v.get("status") == "green" and other["name"] in names:
                v["status"] = "stale-neighbour"
                self.emit(f"  [{other['designation']}] green → "
                          f"stale-neighbour (graph-adjacent to the "
                          f"promotion)\n", "meta")
        self.desig.save()

    # ── run over a scope ──
    STALE_STATUSES = (None, "", "unverified", "stale", "stale-neighbour",
                      "red")

    def _needs_verify(self, item) -> bool:
        """The staleness flags EXIST to be the work queue: anything
        unverified, red, demoted, or whose code moved since its last
        green. Verification proportional to change, not project size."""
        v = item["ent"].get("verified") or {}
        if v.get("manual"):
            return False
        st = v.get("status")
        if st in self.STALE_STATUSES:
            return True
        mod = self.desig.data.get("modules", {}).get(item["rel"], {})
        if mod.get("stale"):
            return True
        src = self._module_source(item["rel"])
        seg = (segment_for(src, item["chain"], MAX_SEG)
               if item["chain"] else src[:MAX_SEG]) or ""
        return bool(v.get("code_hash")
                    and sha1_text(seg) != v.get("code_hash"))

    def run(self, scope_rel: str | None = None,
            only: list | None = None,
            stale_only: bool = False) -> dict:
        self._stop = False
        ts = time.strftime("%Y%m%d-%H%M%S")
        items = [i for i in iter_designations(self.desig)
                 if (scope_rel is None or i["rel"] == scope_rel)]
        if only is not None:
            items = [i for i in items if i["designation"] in only]
        leaves = [i for i in items if i["level"] == "function"]
        if stale_only:
            queued = [i for i in leaves if self._needs_verify(i)]
            self.emit(f"stale queue: {len(queued)} of {len(leaves)} "
                      f"function designation(s) need work\n", "meta")
            leaves = queued
            if not leaves:
                self.emit("nothing stale — the ledger matches the "
                          "disk\n", "ok")
                return {"started": ts, "scope": scope_rel or "ALL",
                        "stale_only": True, "results": [],
                        "finished": now_iso()}
        if not leaves:
            raise RuntimeError("no function designations in scope")
        results = [self.verify_one(i, ts) for i in leaves]
        self._rollup(items)
        record = {"started": ts, "scope": scope_rel or "ALL",
                  "threshold": self.threshold, "repair": self.repair,
                  "stale_only": stale_only, "llm_calls": self.llm_calls,
                  "results": results, "finished": now_iso()}
        vdir = os.path.join(self.root, AGENTS_DIR, VERIF_SUB)
        atomic_write(os.path.join(vdir, f"run_{ts}.json"),
                     json.dumps(record, indent=2))
        lines = [f"# Verification run {ts}", "",
                 f"threshold {self.threshold}% · repair "
                 f"{'on' if self.repair else 'off'}", "",
                 "| designation | status | score | failed |",
                 "| --- | --- | --- | --- |"]
        for r in results:
            lines.append(f"| {r['designation']} | {r['status']} | "
                         f"{r.get('score', '—')} | "
                         f"{', '.join(r.get('failed', [])) or '—'} |")
        atomic_write(os.path.join(vdir, f"run_{ts}.md"),
                     "\n".join(lines) + "\n")
        greens = sum(1 for r in results if r["status"] == "green")
        self.emit(f"VERIFY ↑ {greens}/{len(results)} green — "
                  f"{AGENTS_DIR}/{VERIF_SUB}/run_{ts}.md\n",
                  "ok" if greens == len(results) else "meta")
        return record

    def _rollup(self, items):
        order = {"red": 0, "stale": 1, "stale-neighbour": 1, "no-spec": 2,
                 "unverified": 2, "amber": 3, "manual": 3, "green": 4}
        for it in items:
            if it["level"] == "function":
                continue
            kids = [o for o in items
                    if o["designation"].startswith(it["designation"])
                    and o["level"] == "function"]
            if not kids:
                continue
            sts = [((k["ent"].get("verified") or {}).get("status")
                    or "unverified") for k in kids]
            worst = min(sts, key=lambda s: order.get(s, 2))
            pcts = [(k["ent"].get("verified") or {}).get("pct")
                    for k in kids]
            pcts = [p for p in pcts if isinstance(p, (int, float))]
            it["ent"].setdefault("verified", {}).update(
                {"status": worst,
                 "why": (f"worst of {len(kids)} function(s)"
                         if worst != "green" else ""),
                 "pct": round(sum(pcts) / len(pcts)) if pcts else None,
                 "ts": now_iso()})
        self.desig.save()


# ──────────────────────────────────────────────────────────────────────────
# VerifyTab — the UI (mounted as the second tab of the Agent Workspace)
# ──────────────────────────────────────────────────────────────────────────

class VerifyTab(tk.Frame if tk else object):
    def __init__(self, master, project_dir, desig, api_cfg, chat_fn,
                 spec, theme=None, open_at=None):
        t = theme or {}
        self.bg = t.get("panel", "#2b2d30")
        self.fg = t.get("panel_fg", "#bbbbbb")
        self.tbg = t.get("bg", "#1e1f22")
        self.tfg = t.get("fg", "#dcdcdc")
        self.accent = t.get("accent", "#3574f0")
        super().__init__(master, bg=self.bg)
        self.project_dir = project_dir
        self.desig = desig
        self.api_cfg = api_cfg
        self.chat_fn = chat_fn
        self.spec = spec
        self.open_at = open_at
        self.verifier: Verifier | None = None
        self.items: dict = {}                 # iid → item dict
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._busy = False
        self._build()
        self.populate()
        self._poll()

    def _lbl(self, parent, text):
        return tk.Label(parent, text=text, bg=self.bg, fg=self.fg,
                        font=("Segoe UI", 9))

    def _build(self):
        bar = tk.Frame(self, bg=self.bg)
        bar.pack(fill="x", padx=6, pady=4)
        self._lbl(bar, "threshold %").pack(side="left")
        self.th = tk.Spinbox(bar, from_=50, to=100, width=4)
        attach_context_menu(self.th)
        self.th.delete(0, "end"); self.th.insert(0, "80")
        self.th.pack(side="left", padx=(2, 8))
        self._lbl(bar, "max revisions").pack(side="left")
        self.mr = tk.Spinbox(bar, from_=0, to=10, width=3)
        attach_context_menu(self.mr)
        self.mr.delete(0, "end"); self.mr.insert(0, "5")
        self.mr.pack(side="left", padx=(2, 8))
        self._lbl(bar, "scope").pack(side="left")
        self.scope = tk.StringVar(value="ALL")
        self.scope_box = ttk.Combobox(bar, textvariable=self.scope,
                                      width=20, state="readonly")
        self.scope_box.pack(side="left", padx=2)
        tk.Button(bar, text="■ Stop", relief="flat", padx=8,
                  command=self._stop).pack(side="right")
        self.run_btn = tk.Button(bar, text="▶ Verify", bg=self.accent,
                                 fg="white", relief="flat", padx=10,
                                 command=lambda: self._run(False))
        self.run_btn.pack(side="right", padx=4)
        self.rep_btn = tk.Button(bar, text="▶ Verify + Repair",
                                 relief="flat", padx=8,
                                 command=lambda: self._run(True))
        self.rep_btn.pack(side="right", padx=4)
        self.stale_btn = tk.Button(bar, text="▶ Verify Stale",
                                   relief="flat", padx=8,
                                   command=lambda: self._run(
                                       False, stale_only=True))
        self.stale_btn.pack(side="right", padx=4)
        tk.Button(bar, text="Refresh", relief="flat", padx=8,
                  command=self.populate).pack(side="right", padx=4)

        pw = tk.PanedWindow(self, orient="horizontal", sashwidth=6,
                            bg=self.bg, border=0)
        pw.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # left: designation tree
        lf = tk.Frame(pw, bg=self.bg)
        pw.add(lf, minsize=300)
        self.tree = ttk.Treeview(lf, columns=("st", "score", "spec", "why"),
                                 selectmode="browse")
        self.tree.heading("#0", text="Designation")
        self.tree.heading("st", text="status")
        self.tree.heading("score", text="score")
        self.tree.heading("spec", text="spec")
        self.tree.heading("why", text="why")
        self.tree.column("#0", width=260)
        self.tree.column("st", width=110)
        self.tree.column("score", width=80)
        self.tree.column("spec", width=70)
        self.tree.column("why", width=260)
        vsb = ttk.Scrollbar(lf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for st, col in STATUS_COLOR.items():
            self.tree.tag_configure(st, foreground=col)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.tree.bind("<Double-Button-1>", self._jump)
        self.tree.bind("<Button-3>", self._menu)

        # right: instructions / findings / evolution
        rf = tk.PanedWindow(pw, orient="vertical", sashwidth=6, bg=self.bg,
                            border=0)
        pw.add(rf, minsize=380)

        inf = tk.LabelFrame(rf, text="Instructions (R1., R2., … = rubric; "
                            "Confirm makes green possible)",
                            bg=self.bg, fg=self.fg)
        rf.add(inf, minsize=140)
        self.instr = tk.Text(inf, height=8, bg=self.tbg, fg=self.tfg,
                             insertbackground=self.tfg, wrap="word")
        self.instr.pack(fill="both", expand=True, padx=4, pady=(4, 0))
        attach_context_menu(self.instr)
        ib = tk.Frame(inf, bg=self.bg)
        ib.pack(fill="x", padx=4, pady=4)
        tk.Button(ib, text="Draft via API", relief="flat", padx=8,
                  command=self._draft).pack(side="left")
        tk.Button(ib, text="Save draft", relief="flat", padx=8,
                  command=lambda: self._save_spec(False)).pack(side="left",
                                                               padx=4)
        tk.Button(ib, text="Confirm", relief="flat", padx=8,
                  bg=self.accent, fg="white",
                  command=lambda: self._save_spec(True)).pack(side="left")
        self.spec_state = self._lbl(ib, "")
        self.spec_state.pack(side="right")

        ff = tk.LabelFrame(rf, text="Findings (last verification)",
                           bg=self.bg, fg=self.fg)
        rf.add(ff, minsize=110)
        self.findings = ttk.Treeview(ff, columns=("v", "src", "detail"),
                                     selectmode="browse", height=5)
        self.findings.heading("#0", text="req")
        self.findings.heading("v", text="verdict")
        self.findings.heading("src", text="source")
        self.findings.heading("detail", text="finding")
        self.findings.column("#0", width=150)
        self.findings.column("v", width=60)
        self.findings.column("src", width=70)
        self.findings.column("detail", width=520, stretch=False)
        fsb = ttk.Scrollbar(ff, orient="horizontal",
                            command=self.findings.xview)
        self.findings.configure(xscrollcommand=fsb.set)
        self.findings.pack(fill="both", expand=True, padx=4, pady=(4, 0))
        fsb.pack(fill="x", padx=4)
        self.findings.tag_configure("pass", foreground=STATUS_COLOR["green"])
        self.findings.tag_configure("fail", foreground=STATUS_COLOR["red"])

        ef = tk.LabelFrame(rf, text="Evolution (nested — designation → "
                           "runs → attempts)", bg=self.bg, fg=self.fg)
        rf.add(ef, minsize=110)
        erow = tk.Frame(ef, bg=self.bg)
        erow.pack(fill="x", padx=4)
        self.evo_all = tk.BooleanVar(value=False)
        tk.Checkbutton(erow, text="show all designations",
                       variable=self.evo_all, bg=self.bg, fg=self.fg,
                       selectcolor=self.tbg, activebackground=self.bg,
                       command=self._load_evo).pack(side="left")
        # tree above, full-entry detail below, DRAGGABLE sash between
        epw = tk.PanedWindow(ef, orient="vertical", sashwidth=6,
                             bg=self.bg, border=0)
        epw.pack(fill="both", expand=True, padx=4, pady=(2, 4))
        etop = tk.Frame(epw, bg=self.bg)
        epw.add(etop, minsize=70)
        self.evo_tree = ttk.Treeview(etop, columns=("what",),
                                     selectmode="browse", height=5)
        self.evo_tree.heading("#0", text="entry")
        self.evo_tree.heading("what", text="detail")
        self.evo_tree.column("#0", width=230)
        self.evo_tree.column("what", width=560, stretch=False)
        esb = ttk.Scrollbar(etop, orient="horizontal",
                            command=self.evo_tree.xview)
        self.evo_tree.configure(xscrollcommand=esb.set)
        esb.pack(side="bottom", fill="x")
        self.evo_tree.pack(fill="both", expand=True)
        self.evo_tree.bind("<<TreeviewSelect>>", self._evo_select)
        self._evo_map: dict = {}  # iid → full evolution entry
        # full-entry pane: select any row above → every field UNTRUNCATED,
        # including the complete stored diff (where exactly code changed)
        ebot = tk.Frame(epw, bg=self.bg)
        epw.add(ebot, minsize=60)
        self.evo_detail = tk.Text(ebot, height=7, bg=self.tbg, fg=self.tfg,
                                  wrap="none", state="disabled", padx=6,
                                  font=("Consolas", 9))
        edsb = ttk.Scrollbar(ebot, orient="horizontal",
                             command=self.evo_detail.xview)
        self.evo_detail.configure(xscrollcommand=edsb.set)
        edsb.pack(side="bottom", fill="x")
        self.evo_detail.pack(fill="both", expand=True)

        attach_context_menu(self.evo_detail, read_only=True)

        self.log = tk.Text(self, height=5, bg=self.tbg, fg=self.tfg,
                           state="disabled", wrap="word", padx=6)
        self.log.pack(fill="x", padx=6, pady=(0, 6))
        for tag, col in (("meta", self.accent), ("ok", "#6aab73"),
                         ("err", "#f75464")):
            self.log.tag_configure(tag, foreground=col)
        attach_context_menu(self.log, read_only=True)

    # ── tree ──
    def populate(self):
        self.tree.delete(*self.tree.get_children())
        self.items.clear()
        mods = sorted({i["rel"] for i in iter_designations(self.desig)})
        self.scope_box["values"] = ["ALL"] + mods
        parents = {"": ""}
        for it in iter_designations(self.desig):
            v = it["ent"].get("verified") or {}
            st = v.get("status") or "unverified"
            why = v.get("why", "")
            # display staleness: code moved after last verification
            if st == "green" and it["level"] == "function":
                src = self._src(it)
                if src and v.get("code_hash") and \
                        sha1_text(src) != v.get("code_hash"):
                    st = "stale"
                    why = "code changed since last verification"
            spec_disp = {"confirmed": "✔", "draft": "✎"}.get(
                v.get("spec_state"),
                "—" if not os.path.isfile(self._vp().instr_path(
                    it["designation"])) else "✎")
            parent_desig = (it["designation"][:-len(it["ent"]["id"])]
                            if it["level"] != "module" else "")
            iid = self.tree.insert(parents.get(parent_desig, ""), "end",
                                   text=f"{it['designation']}  {it['name']}",
                                   values=(st, v.get("score", "—"),
                                           spec_disp, why),
                                   tags=(st,), open=True)
            parents[it["designation"]] = iid
            self.items[iid] = it

    def _vp(self) -> Verifier:
        if not self.verifier:
            self.verifier = Verifier(self.project_dir, self.desig,
                                     self.api_cfg, self.chat_fn, self.spec,
                                     emit=lambda t, tag=None:
                                     self._q.put((tag, t)))
        return self.verifier

    def _src(self, it):
        try:
            with open(os.path.join(self.project_dir,
                                   it["rel"].replace("/", os.sep)),
                      "r", encoding="utf-8") as f:
                s = f.read()
        except OSError:
            return ""
        return (segment_for(s, it["chain"], MAX_SEG)
                if it["chain"] else s[:MAX_SEG]) or ""

    def _sel_item(self):
        iid = self.tree.focus()
        return self.items.get(iid)

    # ── selection panes ──
    def _select(self, _e=None):
        it = self._sel_item()
        if not it:
            return
        text, _ = self._vp().read_spec(it["designation"])
        self.instr.delete("1.0", "end")
        self.instr.insert("1.0", text or INSTR_TEMPLATE.format(
            designation=it["designation"], name=it["name"]))
        v = it["ent"].get("verified") or {}
        self.spec_state.config(
            text=f"state: {v.get('spec_state', 'missing')}")
        self.findings.delete(*self.findings.get_children())
        for f in (v.get("findings") or []):
            self.findings.insert("", "end", text=f"{f['req']} {f['text']}",
                                 values=(f["verdict"], f["source"],
                                         f["detail"]),
                                 tags=(("pass",) if f["verdict"] == "pass"
                                       else ("fail",)))
        self._load_evo()

    def _load_evo(self):
        self.evo_tree.delete(*self.evo_tree.get_children())
        self._evo_map.clear()
        it = self._sel_item()
        entries = EvolutionLog(self.project_dir).entries
        if not self.evo_all.get() and it:
            entries = [e for e in entries
                       if e.get("designation") == it["designation"]]
        runs: dict = {}
        for e in entries[-200:]:
            rk = f"{e.get('designation', '?')} · run {e.get('run', '?')}"
            if rk not in runs:
                runs[rk] = self.evo_tree.insert("", "end", text=rk,
                                                values=("",), open=False)
            iid = self.evo_tree.insert(
                runs[rk], "end",
                text=f"{e.get('ts', '')} [{e.get('verdict', '')}]",
                values=(f"{e.get('job', '')} — "
                        f"{e.get('verdict_reason', '')}"[:160],))
            self._evo_map[iid] = e
            for fld in ("reason", "solution_reasoning", "diff"):
                val = str(e.get(fld) or "")
                if val:
                    fiid = self.evo_tree.insert(iid, "end", text=fld,
                                                values=(val[:400],))
                    self._evo_map[fiid] = e

    def _evo_select(self, _e=None):
        """Selecting any evolution row dumps the FULL entry — including
        the complete stored diff — into the detail pane below."""
        e = self._evo_map.get(self.evo_tree.focus())
        self.evo_detail.configure(state="normal")
        self.evo_detail.delete("1.0", "end")
        if e:
            head = (f"{e.get('ts', '')} · {e.get('designation', '')} · "
                    f"run {e.get('run', '')} · agent {e.get('agent', '')} "
                    f"· {e.get('level', '')}\n"
                    f"verdict {e.get('verdict', '')} "
                    f"({e.get('verdict_by', '')})")
            parts = [head]
            for fld in ("job", "reason", "verdict_reason",
                        "solution_reasoning", "diff"):
                val = str(e.get(fld) or "")
                if val:
                    parts.append(f"--- {fld} ---\n{val}")
            self.evo_detail.insert("1.0", "\n\n".join(parts))
        self.evo_detail.configure(state="disabled")

    # ── actions ──
    def _save_spec(self, confirm: bool):
        it = self._sel_item()
        if not it:
            return
        text = self.instr.get("1.0", "end-1c")
        if confirm:
            self._vp().confirm_spec(it, text)
        else:
            atomic_write(self._vp().instr_path(it["designation"]), text)
            v = it["ent"].setdefault("verified", {})
            v["spec_state"] = "draft"
            self.desig.save()
        self._out(f"spec {'confirmed' if confirm else 'saved (draft)'}: "
                  f"{it['designation']}\n", "meta")
        self.populate()

    def _draft(self):
        it = self._sel_item()
        if not it or self._busy:
            return
        self._busy = True
        self._out(f"drafting instructions for {it['designation']}…\n",
                  "meta")

        def w():
            try:
                md = self._vp().draft_instructions(it)
                self._q.put(("_spec", md))
            except Exception as e:  # noqa: BLE001
                self._q.put(("err", f"draft failed: {e}\n"))
            finally:
                self._q.put(("_done", None))
        threading.Thread(target=w, daemon=True).start()

    def _run(self, repair: bool, stale_only: bool = False):
        if self._busy:
            return
        self._busy = True
        self.run_btn.config(state="disabled")
        self.rep_btn.config(state="disabled")
        scope = None if self.scope.get() == "ALL" else self.scope.get()
        sel = self._sel_item()
        only = None
        if sel and scope == "SELECTION":
            only = [sel["designation"]]
        try:
            th = int(self.th.get())
            mr = int(self.mr.get())
        except ValueError:
            th, mr = 80, 5
        self.verifier = Verifier(
            self.project_dir, self.desig, self.api_cfg, self.chat_fn,
            self.spec, emit=lambda t, tag=None: self._q.put((tag, t)),
            threshold=th, max_rev=mr, repair=repair)

        def w():
            try:
                self.verifier.run(scope, only, stale_only=stale_only)
            except VerifierStopped:
                self._q.put(("err", "[stopped by user]\n"))
            except Exception as e:  # noqa: BLE001
                self._q.put(("err", f"verify run failed: {e}\n"))
            finally:
                self._q.put(("_done", None))
        threading.Thread(target=w, daemon=True).start()

    def _stop(self):
        if self.verifier:
            self.verifier.stop()

    def _jump(self, _e=None):
        it = self._sel_item()
        if not (it and self.open_at):
            return
        path = os.path.join(self.project_dir,
                            it["rel"].replace("/", os.sep))
        line = 1
        src = self._vp()._module_source(it["rel"])
        if it["chain"] and src:
            try:
                from common import locate_chain
                node = locate_chain(ast.parse(src), it["chain"])
                if node is not None:
                    line = node.lineno
            except SyntaxError:
                pass
        self.open_at(path, line)

    def _menu(self, e):
        iid = self.tree.identify_row(e.y)
        if iid:
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        it = self._sel_item()
        if not it:
            return
        m = tk.Menu(self.tree, tearoff=0)
        m.add_command(label="Open in editor", command=self._jump)
        v = it["ent"].setdefault("verified", {})

        def toggle_manual():
            v["manual"] = not v.get("manual")
            v["status"] = "manual" if v["manual"] else "unverified"
            self.desig.save()
            self.populate()
        m.add_command(label=("Unmark manual" if v.get("manual")
                             else "Mark manual (side-effect opt-out)"),
                      command=toggle_manual)

        def toggle_freeze():
            v["harness_frozen"] = not v.get("harness_frozen")
            self.desig.save()
            self._out(f"harness {'FROZEN' if v['harness_frozen'] else 'unfrozen'} "
                      f"for {it['designation']} — "
                      + ("it will never be regenerated"
                         if v["harness_frozen"] else
                         "spec changes may regenerate it (diff logged)")
                      + "\n", "meta")

        hp = self._vp().harness_path(it["designation"])
        if os.path.isfile(hp):
            m.add_command(label=("Unfreeze harness"
                                 if v.get("harness_frozen")
                                 else "Freeze harness (lock the oracle)"),
                          command=toggle_freeze)
            m.add_command(label="Open harness in editor",
                          command=lambda: self.open_at
                          and self.open_at(hp, 1))
        m.tk_popup(e.x_root, e.y_root)

    # ── plumbing ──
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
                    self.run_btn.config(state="normal")
                    self.rep_btn.config(state="normal")
                    self.populate()
                elif tag == "_spec":
                    self.instr.delete("1.0", "end")
                    self.instr.insert("1.0", payload)
                    self._out("draft ready — edit, then Confirm\n", "ok")
                elif tag == "_working":
                    self._out(f"… {payload}\n", "meta")
                else:
                    self._out(payload, tag)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(120, self._poll)

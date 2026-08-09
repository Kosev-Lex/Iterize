"""
kernel.py — the pyedit engine from a terminal. No tkinter required.

The reframe this file embodies: the designation index, the Verify
oracle, the change-set orchestrator, and the knowledge-graph memory ARE
the product; the Tk editor is one client of them. This is the other
client — point it at any project directory and the same engine that
runs behind the GUI runs here, scriptably, overnight, from anywhere.

Commands (run from anywhere; --project defaults to the cwd):

  python kernel.py status   [--project DIR]
      Designation counts, staleness, verification rollup — the ledger
      at a glance.

  python kernel.py graph    [--project DIR] [--rel REL [--for NAME ...]]
      The knowledge graph AS MEMORY, readable: compressed cards for the
      named entities (or the whole module) with callers, callees, and
      mapped functionality. What the builder reads before touching code.

  python kernel.py verify   [--project DIR] [--scope REL] [--stale]
                            [--repair] [--threshold N] [--max-rev N]
      The Verify oracle headless. --stale runs only the work queue
      (unverified / red / demoted / code-moved). Repair is shadow-first
      exactly as in the GUI: the file is promoted only after the final
      verdict passes.

  python kernel.py fix      [--project DIR] --file REL
                            [--error PATH | --error-stdin] [--apply]
      The mission→builder→review cycle on one file, optionally seeded
      with a runtime traceback as evidence. WITHOUT --apply the result
      is written beside the file as <name>.candidate.py plus a unified
      diff to stdout — the disk file is untouched (Approve semantics).
      WITH --apply a passing candidate is promoted with backup.

  python kernel.py compact  [--project DIR]
      Bound the evolution log (archive the overflow).

API config comes from ~/.pyedit_config.json (same file the GUI writes).
Pure standard library. Safe to import: no side effects at import time.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys

from common import (atomic_write, load_api_config, api_chat, now_iso,
                    promote, sha1_text)


def _emit(text, tag=None):
    """Console emitter: the engine's queue-shaped messages → stdout,
    with control tags dropped."""
    if tag in ("_working", "_ws", "_chat", "_product", "_filechg"):
        return
    out = text if isinstance(text, str) else str(text)
    sys.stdout.write(out if out.endswith("\n") else out + "\n")
    sys.stdout.flush()


def _project(args) -> str:
    p = os.path.abspath(args.project or os.getcwd())
    if not os.path.isdir(p):
        sys.exit(f"not a directory: {p}")
    return p


def _desig(root):
    """The DesignationManager without importing pyedit (which needs Tk):
    a minimal loader honouring the same schema, plus the sync path via
    a late pyedit import when Tk is available."""
    try:
        from pyedit import DesignationManager      # full manager (Tk box)
        return DesignationManager(root)
    except ImportError:
        pass

    class _RO:
        """Read-only fallback when tkinter is absent: enough for the
        Verifier/Orchestrator (data + save + sync_module is only needed
        for promotion revision bumps — degrade to hash-note)."""

        def __init__(self, root):
            self.root = root
            self.path = os.path.join(root, "designations.json")
            self.load_error = ""
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                self.data = {"modules": {}, "project": {},
                             "counters": {}, "log": []}
                self.load_error = str(e)

        def save(self):
            from common import atomic_write_json
            try:
                atomic_write_json(self.path, self.data)
            except OSError:
                pass

        def sync_module(self, rel, source):
            mod = self.data.get("modules", {}).get(rel)
            if mod is not None:
                mod["file_hash"] = sha1_text(source)
                mod["current_hash"] = mod["file_hash"]
                mod["stale"] = False
                self.save()
            return bool(mod)

        def designation(self, rel, chain):
            mod = self.data.get("modules", {}).get(rel)
            if not mod or mod.get("deleted"):
                return None
            parts, node = ["P1", mod["id"]], mod
            for kind, name in chain:
                node = (node.get("classes", {}).get(name)
                        or node.get("functions", {}).get(name))
                if node is None:
                    return None
                parts.append(node["id"])
            return "".join(parts), node
    return _RO(root)


def _spec(root) -> dict:
    from agents import AgentSpecStore
    return AgentSpecStore(root).spec


# ── commands ──────────────────────────────────────────────────────────────

def cmd_status(args):
    root = _project(args)
    d = _desig(root)
    if getattr(d, "load_error", ""):
        _emit(f"WARNING designations: {d.load_error}", "err")
    mods = {r: m for r, m in d.data.get("modules", {}).items()
            if not m.get("deleted")}
    from verify import iter_designations
    items = [i for i in iter_designations(d)]
    fns = [i for i in items if i["level"] == "function"]
    by = {}
    for i in fns:
        st = (i["ent"].get("verified") or {}).get("status") or "unverified"
        by[st] = by.get(st, 0) + 1
    _emit(f"project: {root}")
    _emit(f"modules: {len(mods)}  ·  designations: {len(items)}  ·  "
          f"functions: {len(fns)}")
    _emit("verification: " + (", ".join(
        f"{k} {v}" for k, v in sorted(by.items())) or "(none)"))
    stale = [r for r, m in mods.items() if m.get("stale")]
    if stale:
        _emit("stale modules (edited past their checkpoint):")
        for r in stale:
            _emit(f"  {r}")
    from verify import Verifier
    v = Verifier(root, d, {}, None, {}, emit=_emit)
    queue = [i["designation"] for i in fns if v._needs_verify(i)]
    _emit(f"work queue (verify --stale would run): {len(queue)}"
          + (" — " + ", ".join(queue[:12])
             + (" …" if len(queue) > 12 else "") if queue else ""))


def cmd_graph(args):
    root = _project(args)
    d = _desig(root)
    from kgraph import KGMemory
    kg = KGMemory(root, d.data)
    if not args.rel:
        _emit(f"{len(kg.graph)} module(s), "
              f"{sum(1 for e in kg.edges if e[2] == 'call')} call edge(s), "
              f"{sum(1 for e in kg.edges if e[2] == 'import')} import "
              f"edge(s)")
        for rel in sorted(kg.graph):
            _emit(f"  {kg.card(rel)}")
        return
    names = args.names or (
        [f"{c}.{f}" for c, cd in kg.graph.get(args.rel, {}).get(
            "classes", {}).items() for f in cd["functions"]]
        + list(kg.graph.get(args.rel, {}).get("functions", {})))
    _emit(kg.context_for(args.rel, names, max_chars=20000))


def cmd_verify(args):
    root = _project(args)
    d = _desig(root)
    from verify import Verifier
    v = Verifier(root, d, load_api_config(), api_chat, _spec(root),
                 emit=_emit, threshold=args.threshold,
                 max_rev=args.max_rev, repair=args.repair)
    rec = v.run(args.scope, stale_only=args.stale)
    _emit(f"llm calls: {rec.get('llm_calls', v.llm_calls)}")


def cmd_fix(args):
    root = _project(args)
    d = _desig(root)
    rel = args.file.replace(os.sep, "/")
    path = os.path.join(root, rel.replace("/", os.sep))
    try:
        with open(path, "r", encoding="utf-8") as f:
            base = f.read()
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}")
    evidence = ""
    if args.error:
        with open(args.error, "r", encoding="utf-8",
                  errors="replace") as f:
            evidence = f.read()[:6000]
    elif args.error_stdin:
        evidence = sys.stdin.read()[:6000]
    from agents import Orchestrator
    product = {"name": os.path.basename(rel), "base_name": rel,
               "rel": rel if rel in d.data.get("modules", {}) else "",
               "source": base, "objectives": [], "directive": "",
               "gate": "", "gate_detail": "", "diff": "", "touched": [],
               "evidence": evidence}
    o = Orchestrator(root, d, _spec(root), load_api_config(), api_chat,
                     emit=_emit, product=product)
    rec = o.run(seed_feedback=evidence)
    candidate = product.get("source", "")
    if candidate == base:
        _emit("no change produced.")
        return
    udiff = "\n".join(difflib.unified_diff(
        base.split("\n"), candidate.split("\n"),
        rel, rel + " (candidate)", lineterm=""))
    _emit(udiff)
    gate_ok = product.get("gate") == "pass"
    verdict_ok = rec.get("verdict") in ("pass", "pass_with_warnings")
    if args.apply:
        if not (gate_ok and verdict_ok):
            sys.exit(f"REFUSED --apply: gate={product.get('gate')} "
                     f"verdict={rec.get('verdict')} — the oracle keeps "
                     f"final authority. Candidate discarded from disk "
                     f"perspective; rerun without --apply to inspect.")
        backup = promote(path, candidate,
                         os.path.join(root, "agents", "backups"),
                         rel, rec.get("started", now_iso()))
        d.sync_module(rel, candidate)
        _emit(f"APPLIED (backup {os.path.basename(backup)}) — "
              f"llm calls: {o.llm_calls}")
    else:
        cand = path + ".candidate.py"
        atomic_write(cand, candidate)
        _emit(f"candidate written (disk file untouched): {cand}  ·  "
              f"gate {product.get('gate')}  ·  verdict "
              f"{rec.get('verdict')}  ·  llm calls {o.llm_calls}")


def cmd_compact(args):
    root = _project(args)
    from agents import EvolutionLog
    arc = EvolutionLog(root).compact()
    _emit(f"archived → {arc}" if arc else "nothing to compact")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="kernel", description="pyedit engine, headless")
    ap.add_argument("--project", default=None,
                    help="project dir (default: cwd)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    g = sub.add_parser("graph")
    g.add_argument("--rel", default=None)
    g.add_argument("--for", dest="names", nargs="*", default=None,
                   help="dotted entity names (Class.method, func)")
    v = sub.add_parser("verify")
    v.add_argument("--scope", default=None)
    v.add_argument("--stale", action="store_true")
    v.add_argument("--repair", action="store_true")
    v.add_argument("--threshold", type=int, default=80)
    v.add_argument("--max-rev", type=int, default=5)
    f = sub.add_parser("fix")
    f.add_argument("--file", required=True, help="project-relative .py")
    f.add_argument("--error", default=None,
                   help="path to a traceback/evidence file")
    f.add_argument("--error-stdin", action="store_true")
    f.add_argument("--apply", action="store_true",
                   help="promote a PASSING candidate (backup kept)")
    sub.add_parser("compact")

    args = ap.parse_args(argv)
    {"status": cmd_status, "graph": cmd_graph, "verify": cmd_verify,
     "fix": cmd_fix, "compact": cmd_compact}[args.cmd](args)


if __name__ == "__main__":
    main()

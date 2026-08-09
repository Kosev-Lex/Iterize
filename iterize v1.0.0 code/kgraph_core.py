"""
kgraph.py — knowledge-graph view for pyedit.

Separate module beside pyedit (like agents.py); no pyedit imports —
dependencies are injected. Pure standard library, except that PNG snapshot
uses Pillow when present (Tk cannot rasterize a canvas natively; without
Pillow the snapshot falls back to .eps via canvas.postscript).

Model:
  * Nodes come straight from designations.json: module → big bubble
    (distinct colour per module), classes → circles inside the module
    bubble, methods → circles inside their class circle, module-level
    functions → circles inside the module bubble but outside any class.
  * Edges are computed from the source AST: module→module imports (dashed)
    and function/method call links (solid lines between circle centres) —
    local calls, self.method calls, imported-symbol calls, and
    alias.module calls, resolved only within the project.
  * Canvas: left-drag on empty space pans, mouse wheel zooms at the
    cursor, left-drag on a module bubble moves that whole module (its
    edges follow). Snapshot writes Snapshots/kg_<timestamp>.png.
  * Below the graph: connection mappings — functionality on the left,
    mapping text on the right — persisted to <project>/kg_mappings.json.

Components:
  build_graph      — designations → node structure (headless)
  analyze_edges    — AST scan → {(src_key, dst_key, kind)} (headless)
  compute_layout   — deterministic nested-circle geometry (headless)
  MappingStore     — kg_mappings.json persistence
  KGWindow         — the Toplevel: canvas, toolbar, mappings panel
"""

from __future__ import annotations

import ast
import json
import math
import os
import queue
import threading
import time

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:          # headless kernel: engine functions only
    tk = ttk = None

from common import (atomic_write, atomic_write_json,
                    attach_context_menu, extract_json, persist_geometry,
                    UIState)

_extract_json = extract_json

SNAPSHOTS_DIR = "Snapshots"
MAPPINGS_FILE = "kg_mappings.json"
LAYOUT_FILE = "kg_layout.json"
MAX_MAP_SRC = 30000

MAP_PROMPT = """Read this Python module and extract its major functional
areas — "this part of the code does A, this part does B". Respond ONLY with
JSON:
{"mappings": [{"functionality": "short area name",
  "mapping": "%(rel)s :: Class.method, other_func — what these parts do"}]}
MODULE: %(rel)s
SOURCE:
%(src)s
"""


PALETTE = ["#3574f0", "#6aab73", "#cf8e6d", "#c77dbb",
           "#2aacb8", "#f0a732", "#f75464", "#9aa7ff"]

R_FN = 11           # function/method circle radius
CANVAS_BG = "#1e1f22"
EDGE_CALL = "#7a7e85"
EDGE_IMPORT = "#3574f0"


# ──────────────────────────────────────────────────────────────────────────
# Graph construction (headless)
# ──────────────────────────────────────────────────────────────────────────

def build_graph(desig_data: dict) -> dict:
    """designations → {rel: {"id", "classes": {name: {"id", "functions":
    {fname: id}}}, "functions": {fname: id}}} — deleted entities skipped."""
    out = {}
    for rel, m in desig_data.get("modules", {}).items():
        if m.get("deleted"):
            continue
        classes = {}
        for cname, c in m.get("classes", {}).items():
            if c.get("deleted"):
                continue
            classes[cname] = {
                "id": c["id"],
                "functions": {fn: f["id"]
                              for fn, f in c.get("functions", {}).items()
                              if not f.get("deleted")},
            }
        out[rel] = {
            "id": m["id"],
            "classes": classes,
            "functions": {fn: f["id"]
                          for fn, f in m.get("functions", {}).items()
                          if not f.get("deleted")},
        }
    return out


def _qual(rel: str) -> str:
    return (rel[:-3] if rel.endswith(".py") else rel).replace("/", ".")


def analyze_edges(project_dir: str, graph: dict) -> set:
    """{(src_key, dst_key, kind)}: kind 'import' (module→module) or 'call'.
    Keys: rel for modules, '<rel>::Name' / '<rel>::Cls.meth' for entities."""
    qual2rel = {_qual(r): r for r in graph}
    sym: dict[str, dict] = {}
    meth: dict[tuple, dict] = {}
    for rel, m in graph.items():
        s = {}
        for cname, c in m["classes"].items():
            s[cname] = f"{rel}::{cname}"
            meth[(rel, cname)] = {fn: f"{rel}::{cname}.{fn}"
                                  for fn in c["functions"]}
        for fn in m["functions"]:
            s[fn] = f"{rel}::{fn}"
        sym[rel] = s

    edges: set = set()
    for rel in graph:
        path = os.path.join(project_dir, rel.replace("/", os.sep))
        try:
            with open(path, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except (OSError, SyntaxError):
            continue

        alias_mod: dict[str, str] = {}          # local name → project rel
        alias_sym: dict[str, tuple] = {}        # local name → (rel, symbol)
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    r2 = qual2rel.get(a.name)
                    if r2:
                        edges.add((rel, r2, "import"))
                        local = a.asname or (a.name if "." not in a.name else None)
                        if local:
                            alias_mod[local] = r2
            elif isinstance(n, ast.ImportFrom) and n.module:
                r2 = qual2rel.get(n.module)
                if r2:
                    edges.add((rel, r2, "import"))
                    for a in n.names:
                        alias_sym[a.asname or a.name] = (r2, a.name)

        # instance maps — the dominant real-code pattern:
        #   self.vm = VersionManager(...)   →  self.vm.mark_original() resolves
        #   v = Helper(...)                 →  v.run() resolves (function-local)
        def class_target(fnode):
            if isinstance(fnode, ast.Name):
                nm = fnode.id
                if nm in graph[rel]["classes"]:
                    return (rel, nm)
                if nm in alias_sym:
                    r2, s2 = alias_sym[nm]
                    if s2 in graph.get(r2, {}).get("classes", {}):
                        return (r2, s2)
            elif isinstance(fnode, ast.Attribute) and isinstance(fnode.value,
                                                                 ast.Name):
                r2 = alias_mod.get(fnode.value.id)
                if r2 and fnode.attr in graph.get(r2, {}).get("classes", {}):
                    return (r2, fnode.attr)
            return None

        inst_self: dict = {}          # class name → {attr: (rel2, Class2)}
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            amap = inst_self.setdefault(cls.name, {})
            for n in ast.walk(cls):
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
                    tgt = class_target(n.value.func)
                    if not tgt:
                        continue
                    for t in n.targets:
                        if (isinstance(t, ast.Attribute)
                                and isinstance(t.value, ast.Name)
                                and t.value.id == "self"):
                            amap[t.attr] = tgt

        def calls_in(node, out):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                    continue                     # owned by the nested scope
                if isinstance(ch, ast.Call):
                    out.append(ch)
                calls_in(ch, out)

        def resolve(call, key, cls_name, local_inst):
            f = call.func
            tgt = None
            if isinstance(f, ast.Name):
                nm = f.id
                if nm in sym[rel]:
                    tgt = sym[rel][nm]
                elif nm in alias_sym:
                    r2, s2 = alias_sym[nm]
                    tgt = sym.get(r2, {}).get(s2)
            elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                base, attr = f.value.id, f.attr
                if base == "self" and cls_name:
                    tgt = meth.get((rel, cls_name), {}).get(attr)
                elif base in alias_mod:
                    tgt = sym.get(alias_mod[base], {}).get(attr)
                elif base in local_inst:                 # v = Cls(); v.m()
                    tgt = meth.get(local_inst[base], {}).get(attr)
            elif (isinstance(f, ast.Attribute)
                  and isinstance(f.value, ast.Attribute)
                  and isinstance(f.value.value, ast.Name)
                  and f.value.value.id == "self" and cls_name):
                # self.<attr>.<meth>() via the held-instance map
                held = inst_self.get(cls_name, {}).get(f.value.attr)
                if held:
                    tgt = meth.get(held, {}).get(f.attr)
            if tgt and tgt != key:
                edges.add((key, tgt, "call"))

        def walk_scope(node, cls_name):
            for ch in ast.iter_child_nodes(node):
                if isinstance(ch, ast.ClassDef):
                    walk_scope(ch, ch.name)
                elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if cls_name:
                        key = meth.get((rel, cls_name), {}).get(ch.name)
                    else:
                        key = sym[rel].get(ch.name)
                    if key:
                        local_inst = {}
                        for n2 in ast.walk(ch):
                            if (isinstance(n2, ast.Assign)
                                    and isinstance(n2.value, ast.Call)):
                                t2 = class_target(n2.value.func)
                                if t2:
                                    for t in n2.targets:
                                        if isinstance(t, ast.Name):
                                            local_inst[t.id] = t2
                        found = []
                        calls_in(ch, found)
                        for c in found:
                            resolve(c, key, cls_name, local_inst)
                    walk_scope(ch, cls_name)     # nested defs keep self context
        walk_scope(tree, None)
    return edges


# ──────────────────────────────────────────────────────────────────────────
# KGMemory — the graph AS MEMORY (headless)
#
# The graph is not a picture: it is the compressed, rapidly accessible
# memory of the codebase. KGMemory is the fetch API over it — the builder
# reads a change target's callers before amending it, the verifier
# demotes a promotion's graph-neighbours, the kernel answers "what talks
# to X" from a terminal. One build per instantiation; keys are
# analyze_edges keys ('<rel>::Cls.meth', '<rel>::fn', rel for modules).
# ──────────────────────────────────────────────────────────────────────────

class KGMemory:
    def __init__(self, project_dir: str, desig_data: dict):
        self.root = os.path.abspath(project_dir)
        self.desig_data = desig_data
        self.graph = build_graph(desig_data)
        self.edges = analyze_edges(self.root, self.graph)
        self.callers_of: dict[str, set] = {}
        self.callees_of: dict[str, set] = {}
        for a, b, kind in self.edges:
            if kind != "call":
                continue
            self.callers_of.setdefault(b, set()).add(a)
            self.callees_of.setdefault(a, set()).add(b)
        try:
            self.mappings = MappingStore(project_dir).rows
        except Exception:  # noqa: BLE001
            self.mappings = []

    # ── key helpers ──
    def key(self, rel: str, dotted: str) -> str:
        return f"{rel}::{dotted}" if dotted else rel

    @staticmethod
    def split(key: str) -> tuple:
        return tuple(key.split("::", 1)) if "::" in key else (key, "")

    def _entity(self, key: str):
        rel, dotted = self.split(key)
        mod = self.desig_data.get("modules", {}).get(rel)
        if not mod or not dotted:
            return mod
        node = mod
        for part in dotted.split("."):
            node = (node.get("classes", {}).get(part)
                    or node.get("functions", {}).get(part))
            if node is None:
                return None
        return node

    def card(self, key: str) -> str:
        """One compressed line for one entity: designation · signature ·
        annotated description. This is the unit of recall."""
        rel, dotted = self.split(key)
        ent = self._entity(key)
        if ent is None:
            return f"{key}  (unregistered)"
        mod = self.desig_data.get("modules", {}).get(rel, {})
        did = "P1" + mod.get("id", "?") + "".join(
            p for p in [ent.get("id", "")] if dotted)
        sig = (ent.get("signature") or "").strip()
        desc = (ent.get("description") or "").strip()
        bits = [f"[{did}] {key}"]
        if sig:
            bits.append(sig)
        if desc:
            bits.append(desc[:140])
        return "  ·  ".join(bits)

    def callers(self, key: str) -> list:
        return sorted(self.callers_of.get(key, ()))

    def callees(self, key: str) -> list:
        return sorted(self.callees_of.get(key, ()))

    def mapping_rows_for(self, name: str) -> list:
        return [f"- {r.get('functionality', '')}: {r.get('mapping', '')}"
                for r in self.mappings if name in r.get("mapping", "")]

    def context_for(self, rel: str, dotted_names: list,
                    max_chars: int = 4000) -> str:
        """The recall block a builder/reviewer reads before touching the
        named entities: each target's card, its callers and callees as
        cards, and any human mapping rows naming it. Compressed —
        segments are fetched separately by whoever needs code."""
        out, seen = [], set()
        for dotted in dotted_names:
            k = self.key(rel, dotted)
            if k in seen:
                continue
            seen.add(k)
            out.append(f"TARGET {self.card(k)}")
            cs = self.callers(k)
            if cs:
                out.append("  called by:")
                out.extend(f"    {self.card(c)}" for c in cs[:8])
            ce = self.callees(k)
            if ce:
                out.append("  calls:")
                out.extend(f"    {self.card(c)}" for c in ce[:8])
            leaf = dotted.split(".")[-1]
            rows = self.mapping_rows_for(leaf)
            if rows:
                out.append("  mapped functionality:")
                out.extend(f"    {r}" for r in rows[:4])
        return "\n".join(out)[:max_chars] or "(no graph memory for these)"

    def caller_segments(self, rel: str, dotted_names: list,
                        per_cap: int = 2000, total_cap: int = 8000) -> str:
        """The CODE of every function that calls the targets — the seam
        the change must not break, fetched from wherever it lives."""
        from common import segment_for
        out, used, seen = [], 0, set()
        for dotted in dotted_names:
            for ck in self.callers(self.key(rel, dotted)):
                if ck in seen or used >= total_cap:
                    continue
                seen.add(ck)
                crel, cdot = self.split(ck)
                if not cdot:
                    continue
                path = os.path.join(self.root, crel.replace("/", os.sep))
                try:
                    with open(path, "r", encoding="utf-8",
                              errors="replace") as f:
                        s = f.read()
                except OSError:
                    continue
                seg = segment_for(
                    s, [("?", p) for p in cdot.split(".")], per_cap)
                if seg:
                    out.append(f"### CALLER {ck}\n{seg}")
                    used += len(seg)
        return "\n\n".join(out)

    def neighbour_names(self, rel: str, dotted: str) -> set:
        """Bare leaf names of graph-adjacent entities (callers+callees) —
        what the verifier demotes when this entity's code is promoted."""
        k = self.key(rel, dotted)
        out = set()
        for nk in list(self.callers(k)) + list(self.callees(k)):
            _r, d = self.split(nk)
            if d:
                out.add(d.split(".")[-1])
        return out


# ──────────────────────────────────────────────────────────────────────────
# Layout (headless): nested circles, deterministic
# ──────────────────────────────────────────────────────────────────────────

def _adjacency(graph: dict, edges) -> tuple:
    """Intra-module adjacency: (unit↔unit weights per module,
    method↔method weights per class) — drives ring clustering."""
    unit_adj: dict = {}
    meth_adj: dict = {}

    def unit_of(key):
        rel, _, q = key.partition("::")
        if not q:
            return rel, None
        top = q.split(".")[0]
        kind = "C" if top in graph.get(rel, {}).get("classes", {}) else "F"
        return rel, (kind, top)

    for s, t, _k in edges or []:
        rs, us = unit_of(s)
        rt, ut = unit_of(t)
        if us and ut and rs == rt and us != ut:
            a = unit_adj.setdefault(rs, {})
            a.setdefault(us, {})[ut] = a.setdefault(us, {}).get(ut, 0) + 1
            a.setdefault(ut, {})[us] = a.setdefault(ut, {}).get(us, 0) + 1
        if rs == rt and "::" in s and "::" in t:
            qs, qt = s.split("::", 1)[1], t.split("::", 1)[1]
            if "." in qs and "." in qt:
                cs, ms = qs.split(".", 1)
                ct, mt = qt.split(".", 1)
                if cs == ct and ms != mt:
                    a = meth_adj.setdefault((rs, cs), {})
                    a.setdefault(ms, {})[mt] = a.setdefault(ms, {}).get(mt, 0) + 1
                    a.setdefault(mt, {})[ms] = a.setdefault(mt, {}).get(ms, 0) + 1
    return unit_adj, meth_adj


def _chain(ids: list, adj: dict) -> list:
    """Greedy nearest-neighbour ordering: connected nodes end up adjacent
    on the ring. Deterministic (sorted tie-breaks)."""
    ids = sorted(ids, key=str)
    if len(ids) < 3 or not adj:
        return ids
    rem = set(ids)
    cur = max(ids, key=lambda i: (sum(adj.get(i, {}).values()), str(i)))
    order = [cur]
    rem.discard(cur)
    while rem:
        nxt = max(sorted(rem, key=str),
                  key=lambda j: adj.get(order[-1], {}).get(j, 0))
        order.append(nxt)
        rem.discard(nxt)
    return order


def compute_layout(graph: dict, edges=None) -> tuple:
    """Returns (geo, pos):
      geo[rel] = {"center", "R", "classes": {name: {"c", "r"}}, ...}
      pos[key] = (x, y) for every node key (module rel + entity keys)."""
    geo, pos = {}, {}

    unit_adj, meth_adj = _adjacency(graph, edges)

    # per-module geometry in local coords, centred on (0, 0)
    local = {}
    for rel, m in graph.items():
        radii = {}
        for cname, c in m["classes"].items():
            radii[("C", cname)] = 30 + 9 * math.sqrt(max(len(c["functions"]), 1))
        for fname in m["functions"]:
            radii[("F", fname)] = R_FN + 4
        ordered = _chain(list(radii), unit_adj.get(rel, {}))
        items = [(k, n, radii[(k, n)]) for k, n in ordered]
        if items:
            max_r = max(r for _k, _n, r in items)
            need = 1.7 * sum(2 * r for _k, _n, r in items)
            ring = max(max_r + 30, need / (2 * math.pi))
            R = ring + max_r + 26
        else:
            ring, R = 0, 60
        placed = {}
        n = max(len(items), 1)
        for i, (kind, name, r) in enumerate(items):
            th = 2 * math.pi * i / n - math.pi / 2
            placed[(kind, name)] = (ring * math.cos(th),
                                    ring * math.sin(th), r)
        local[rel] = (R, placed)

    # arrange module bubbles in rows
    n_mod = max(len(graph), 1)
    per_row = max(1, math.ceil(math.sqrt(n_mod)))
    x, y, row_h, col = 80.0, 80.0, 0.0, 0
    for rel, m in graph.items():
        R, placed = local[rel]
        if col == per_row:
            col, x = 0, 80.0
            y += row_h + 70
            row_h = 0.0
        cx, cy = x + R, y + R
        x += 2 * R + 70
        row_h = max(row_h, 2 * R)
        col += 1

        pos[rel] = (cx, cy)
        classes, functions = {}, {}
        for (kind, name), (lx, ly, r) in placed.items():
            px, py = cx + lx, cy + ly
            if kind == "C":
                classes[name] = {"c": (px, py), "r": r}
                pos[f"{rel}::{name}"] = (px, py)
                fns = _chain(list(graph[rel]["classes"][name]["functions"]),
                             meth_adj.get((rel, name), {}))
                mring = max(r - R_FN - 10, r * 0.45)
                for j, fn in enumerate(fns):
                    if len(fns) == 1:
                        fx, fy = px, py + 2
                    else:
                        t2 = 2 * math.pi * j / len(fns) - math.pi / 2
                        fx = px + mring * math.cos(t2)
                        fy = py + mring * math.sin(t2)
                    pos[f"{rel}::{name}.{fn}"] = (fx, fy)
            else:
                functions[name] = (px, py)
                pos[f"{rel}::{name}"] = (px, py)
        geo[rel] = {"center": (cx, cy), "R": R,
                    "classes": classes, "functions": functions}
    return geo, pos


# ──────────────────────────────────────────────────────────────────────────
# MappingStore — functionality → mapping rows
# ──────────────────────────────────────────────────────────────────────────

class MappingStore:
    def __init__(self, project_dir: str):
        self.path = os.path.join(project_dir, MAPPINGS_FILE)
        self.rows: list[dict] = self.load()

    def load(self) -> list:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def save(self):
        try:
            atomic_write_json(self.path, self.rows)
        except OSError:
            pass

    def upsert(self, functionality: str, mapping: str):
        for r in self.rows:
            if r["functionality"] == functionality:
                r["mapping"] = mapping
                break
        else:
            self.rows.append({"functionality": functionality,
                              "mapping": mapping})
        self.save()

    def remove(self, functionality: str):
        self.rows = [r for r in self.rows
                     if r["functionality"] != functionality]
        self.save()


# ──────────────────────────────────────────────────────────────────────────
# KGWindow
# ──────────────────────────────────────────────────────────────────────────

class KGWindow(tk.Toplevel if tk else object):
    # (headless import: class exists but is never instantiated without tk)
    def __init__(self, master, project_dir: str, desig, theme=None,
                 api_cfg=None, chat_fn=None):
        super().__init__(master)
        self.title(f"Knowledge Graph — {os.path.basename(project_dir)}")
        persist_geometry(self, UIState(), "kgraph.window", "1000x820")
        t = theme or {}
        self.bg = t.get("panel", "#2b2d30")
        self.fg = t.get("panel_fg", "#bbbbbb")
        self.configure(bg=self.bg)

        self.project_dir = project_dir
        self.desig = desig
        self.api_cfg = api_cfg
        self.chat_fn = chat_fn
        self.maps = MappingStore(project_dir)
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._gen_busy = False
        self._dragged = False
        self.layout_path = os.path.join(project_dir, LAYOUT_FILE)
        self.pos: dict[str, tuple] = {}
        self.edge_items: list[dict] = []     # {"src","dst","cid"}
        self.tag_rel: dict[str, str] = {}    # canvas module tag → rel
        self._drag = None

        bar = tk.Frame(self, bg=self.bg); bar.pack(fill="x")
        tk.Button(bar, text="Rebuild", command=self.rebuild, relief="flat",
                  padx=8).pack(side="left", padx=(8, 4), pady=4)
        tk.Button(bar, text="Snapshot (.png)", command=self.snapshot,
                  relief="flat", padx=8).pack(side="left")
        tk.Button(bar, text="Generate mappings (API)",
                  command=self._gen_mappings, relief="flat",
                  padx=8).pack(side="left", padx=4)
        tk.Button(bar, text="Reset layout", command=self._reset_layout,
                  relief="flat", padx=8).pack(side="left")
        self.status = tk.Label(bar, text="drag empty space to pan · wheel to "
                               "zoom · drag a module bubble to move it",
                               bg=self.bg, fg=self.fg, anchor="e")
        self.status.pack(side="right", padx=8)

        self.vpane = tk.PanedWindow(self, orient="vertical", sashwidth=6,
                                    bg=self.bg, border=0)
        self.vpane.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        cframe = tk.Frame(self.vpane, bg=self.bg)
        self.vpane.add(cframe, minsize=220)
        self.canvas = tk.Canvas(cframe, bg=CANVAS_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<MouseWheel>", self._wheel)            # Windows
        self.canvas.bind("<Button-4>", lambda e: self._zoom(1.1, e))
        self.canvas.bind("<Button-5>", lambda e: self._zoom(0.9, e))

        self._build_mappings_panel()
        self.rebuild()
        self._poll()

    # ── drawing ──
    def rebuild(self):
        self.canvas.delete("all")
        self.edge_items.clear()
        self.tag_rel.clear()
        graph = build_graph(self.desig.data)
        if not graph:
            self.status.config(text="no modules registered — save project "
                                    ".py files first")
            return
        edges = analyze_edges(self.project_dir, graph)
        geo, self.pos = compute_layout(graph, edges)
        # persisted per-node overrides (drag positions survive rebuilds)
        try:
            with open(self.layout_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for key, xy in saved.items():
                if key in self.pos and isinstance(xy, list) and len(xy) == 2:
                    self.pos[key] = (float(xy[0]), float(xy[1]))
        except (OSError, json.JSONDecodeError, ValueError):
            pass

        # edges first (nodes drawn over the endpoints)
        for src, dst, kind in sorted(edges):
            if src not in self.pos or dst not in self.pos:
                continue
            x1, y1 = self.pos[src]
            x2, y2 = self.pos[dst]
            if kind == "import":
                cid = self.canvas.create_line(x1, y1, x2, y2,
                                              fill=EDGE_IMPORT, dash=(4, 4))
            else:
                cid = self.canvas.create_line(x1, y1, x2, y2, fill=EDGE_CALL)
            self.edge_items.append({"src": src, "dst": dst, "cid": cid})

        for i, (rel, g) in enumerate(geo.items()):
            colour = PALETTE[i % len(PALETTE)]
            tag = f"mod{i}"
            self.tag_rel[tag] = rel
            cx, cy = self.pos[rel]
            R = g["R"]
            self.canvas.create_oval(cx - R, cy - R, cx + R, cy + R,
                                    outline=colour, width=2, fill="",
                                    tags=(tag,))
            self.canvas.create_text(cx, cy - R + 14, text=rel, fill=colour,
                                    font=("Segoe UI", 10, "bold"),
                                    tags=(tag,))
            for cname, c in g["classes"].items():
                ckey = f"{rel}::{cname}"
                ctag = f"c:{ckey}"
                px, py = self.pos[ckey]
                r = c["r"]
                self.canvas.create_oval(px - r, py - r, px + r, py + r,
                                        outline=colour, width=1,
                                        fill="", tags=(tag, ctag))
                self.canvas.create_text(px, py - r + 10, text=cname,
                                        fill=self.fg,
                                        font=("Segoe UI", 8, "bold"),
                                        tags=(tag, ctag))
                for key, (fx, fy) in self.pos.items():
                    if key.startswith(ckey + "."):
                        self._fn_circle(fx, fy, key.rsplit(".", 1)[1],
                                        colour, (tag, ctag, f"f:{key}"))
            for fname in g["functions"]:
                fkey = f"{rel}::{fname}"
                fx, fy = self.pos[fkey]
                self._fn_circle(fx, fy, fname, colour, (tag, f"f:{fkey}"))
        self.status.config(text=f"{len(geo)} modules · "
                                f"{len(self.edge_items)} links")

    def _fn_circle(self, x, y, name, colour, tags):
        self.canvas.create_oval(x - R_FN, y - R_FN, x + R_FN, y + R_FN,
                                outline=colour, fill=CANVAS_BG, tags=tags)
        self.canvas.create_text(x, y + R_FN + 7, text=name, fill=self.fg,
                                font=("Segoe UI", 7), tags=tags)

    # ── navigation ──
    def _press(self, e):
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        cur = self.canvas.find_withtag("current")
        self._drag = None
        self._dragged = False
        if cur:
            tags = self.canvas.gettags(cur[0])
            ftag = next((t for t in tags if t.startswith("f:")), None)
            ctag = next((t for t in tags if t.startswith("c:")), None)
            mtag = next((t for t in tags if t.startswith("mod")), None)
            if ftag:                                   # single function/method
                self._drag = (ftag, {ftag[2:]}, cx, cy)
            elif ctag:                                 # class + its methods
                ck = ctag[2:]
                keys = {ck} | {k for k in self.pos if k.startswith(ck + ".")}
                self._drag = (ctag, keys, cx, cy)
            elif mtag:                                 # whole module
                rel = self.tag_rel[mtag]
                keys = {rel} | {k for k in self.pos
                                if k.startswith(rel + "::")}
                self._drag = (mtag, keys, cx, cy)
        if self._drag is None:
            self.canvas.scan_mark(e.x, e.y)

    def _motion(self, e):
        if self._drag:
            tag, keys, px, py = self._drag
            cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
            dx, dy = cx - px, cy - py
            self.canvas.move(tag, dx, dy)
            for key in keys:
                x, y = self.pos[key]
                self.pos[key] = (x + dx, y + dy)
            for ed in self.edge_items:
                if ed["src"] in keys or ed["dst"] in keys:
                    x1, y1 = self.pos[ed["src"]]
                    x2, y2 = self.pos[ed["dst"]]
                    self.canvas.coords(ed["cid"], x1, y1, x2, y2)
            self._drag = (tag, keys, cx, cy)
            self._dragged = True
        else:
            self.canvas.scan_dragto(e.x, e.y, gain=1)

    def _release(self, _e):
        if self._dragged:
            self._save_layout()
        self._drag = None
        self._dragged = False

    def _save_layout(self):
        try:
            atomic_write_json(self.layout_path,
                              {k: [round(x, 1), round(y, 1)]
                               for k, (x, y) in self.pos.items()}, indent=1)
            self.status.config(text="layout saved")
        except OSError as e:
            self.status.config(text=f"layout save failed: {e}")

    def _reset_layout(self):
        try:
            os.remove(self.layout_path)
        except OSError:
            pass
        self.rebuild()
        self.status.config(text="layout reset to computed positions")

    # zoom also rescales the persisted layout coherently on next save
    def _gen_mappings(self):
        """Read each module via the API, extract its major functional areas,
        and write them into the connection-mapping treeview."""
        if self._gen_busy:
            return
        if not (self.chat_fn and self.api_cfg):
            self.status.config(text="API not wired — open the graph from a "
                                    "current pyedit (needs api_chat)")
            return
        mods = [r for r, m in self.desig.data.get("modules", {}).items()
                if not m.get("deleted")]
        if not mods:
            self.status.config(text="no modules registered")
            return
        self._gen_busy = True
        self.status.config(text=f"generating mappings over {len(mods)} "
                                f"module(s)…")
        cfg = dict(self.api_cfg)

        def work():
            rows = []
            try:
                for rel in mods:
                    p = os.path.join(self.project_dir,
                                     rel.replace("/", os.sep))
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            s = f.read()[:MAX_MAP_SRC]
                    except OSError:
                        continue
                    reply = self.chat_fn(
                        cfg,
                        [{"role": "user",
                          "content": MAP_PROMPT % {"rel": rel, "src": s}}],
                        system="Respond ONLY with JSON.")
                    for m in (_extract_json(reply).get("mappings") or []):
                        if isinstance(m, dict) and m.get("functionality"):
                            rows.append((str(m["functionality"]),
                                         str(m.get("mapping", ""))))
                self._q.put(("rows", rows))
            except Exception as e:  # noqa: BLE001
                self._q.put(("err", str(e)))
            finally:
                self._q.put(("done", None))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                tag, payload = self._q.get_nowait()
                if tag == "rows":
                    for func, mapping in payload:
                        self.maps.upsert(func, mapping)
                    self._map_refresh()
                    self.status.config(text=f"{len(payload)} mapping(s) "
                                            f"written")
                elif tag == "err":
                    self.status.config(text=f"mapping generation failed: "
                                            f"{payload}"[:160])
                elif tag == "done":
                    self._gen_busy = False
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

    def _wheel(self, e):
        self._zoom(1.1 if e.delta > 0 else 0.9, e)

    def _zoom(self, factor, e):
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        self.canvas.scale("all", cx, cy, factor, factor)
        for key, (x, y) in self.pos.items():
            self.pos[key] = (cx + (x - cx) * factor, cy + (y - cy) * factor)

    # ── snapshot ──
    def snapshot(self):
        snap_dir = os.path.join(self.project_dir, SNAPSHOTS_DIR)
        os.makedirs(snap_dir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(snap_dir, f"kg_{ts}.png")
        try:
            from PIL import ImageGrab
        except ImportError:
            eps = path[:-4] + ".eps"
            try:
                self.canvas.postscript(file=eps, colormode="color")
                self.status.config(text=f"Pillow not installed — wrote EPS "
                                        f"instead: {os.path.basename(eps)} "
                                        f"(pip install pillow for PNG)")
            except tk.TclError as err:
                self.status.config(text=f"snapshot failed: {err}")
            return
        self.update()
        x = self.canvas.winfo_rootx()
        y = self.canvas.winfo_rooty()
        w = self.canvas.winfo_width()
        h = self.canvas.winfo_height()
        try:
            ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
            self.status.config(text=f"snapshot → Snapshots/kg_{ts}.png")
        except Exception as err:  # noqa: BLE001
            self.status.config(text=f"snapshot failed: {err}")

    # ── mappings panel ──
    def _build_mappings_panel(self):
        frm = tk.LabelFrame(self.vpane, text="Connection mappings "
                            "(functionality → mapping) — drag the divider "
                            "above to stretch",
                            bg=self.bg, fg=self.fg)
        self.vpane.add(frm, minsize=140)
        wrap = tk.Frame(frm, bg=self.bg)
        wrap.pack(fill="both", expand=True, padx=4, pady=4)
        self.map_tree = ttk.Treeview(wrap, columns=("mapping",),
                                     selectmode="browse")
        self.map_tree.heading("#0", text="Functionality")
        self.map_tree.heading("mapping", text="Mapping")
        self.map_tree.column("#0", width=240, stretch=False)
        self.map_tree.column("mapping", width=1600, stretch=False)
        vsb = ttk.Scrollbar(wrap, orient="vertical",
                            command=self.map_tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal",
                            command=self.map_tree.xview)
        self.map_tree.configure(yscrollcommand=vsb.set,
                                xscrollcommand=hsb.set)
        self.map_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.map_tree.bind("<<TreeviewSelect>>", self._map_select)
        self.map_tree.bind("<Button-3>", self._map_menu)
        self.map_tree.bind("<Control-c>", lambda e: self._map_copy())

        row = tk.Frame(frm, bg=self.bg); row.pack(fill="x", padx=4, pady=(0, 4))
        self.m_func = tk.StringVar()
        self.m_map = tk.StringVar()
        tk.Label(row, text="functionality", bg=self.bg,
                 fg=self.fg).pack(side="left", padx=(0, 2))
        e_f = tk.Entry(row, textvariable=self.m_func, width=24)
        e_f.pack(side="left")
        attach_context_menu(e_f)
        tk.Label(row, text="mapping", bg=self.bg,
                 fg=self.fg).pack(side="left", padx=(8, 2))
        e_m = tk.Entry(row, textvariable=self.m_map, width=60)
        e_m.pack(side="left", fill="x", expand=True)
        attach_context_menu(e_m)
        tk.Button(row, text="Add / Update", command=self._map_save,
                  relief="flat", padx=8).pack(side="left", padx=6)
        tk.Button(row, text="Remove", command=self._map_remove,
                  relief="flat", padx=8).pack(side="left")
        tk.Button(row, text="Export…", command=self._map_export,
                  relief="flat", padx=8).pack(side="left", padx=6)
        self._map_refresh()

    def _map_copy(self):
        sel = self.map_tree.focus()
        if not sel:
            return
        vals = self.map_tree.item(sel, "values")
        text = (f"{self.map_tree.item(sel, 'text')}: "
                f"{vals[0] if vals else ''}")
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.config(text="row copied")

    def _map_menu(self, e):
        iid = self.map_tree.identify_row(e.y)
        if iid:
            self.map_tree.selection_set(iid)
            self.map_tree.focus(iid)
        m = tk.Menu(self.map_tree, tearoff=0)
        m.add_command(label="Copy row (Ctrl+C)", command=self._map_copy)
        m.add_command(label="Export all…", command=self._map_export)
        m.tk_popup(e.x_root, e.y_root)

    def _map_export(self):
        """Export mappings — default: <project>/mappings/, or anywhere."""
        from tkinter import filedialog
        ddir = os.path.join(self.project_dir, "mappings")
        os.makedirs(ddir, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = filedialog.asksaveasfilename(
            parent=self, initialdir=ddir,
            initialfile=f"mappings_{ts}.md", defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("JSON", "*.json")])
        if not path:
            return
        try:
            if path.lower().endswith(".json"):
                atomic_write_json(path, self.maps.rows)
            else:
                lines = ["| Functionality | Mapping |", "| --- | --- |"]
                for r in self.maps.rows:
                    lines.append(f"| {r['functionality']} | "
                                 f"{r['mapping']} |")
                atomic_write(path, "\n".join(lines) + "\n")
        except OSError as err:
            self.status.config(text=f"export failed: {err}")
            return
        self.status.config(text=f"exported {len(self.maps.rows)} row(s) → "
                                f"{os.path.basename(path)}")

    def _map_refresh(self):
        self.map_tree.delete(*self.map_tree.get_children())
        for r in self.maps.rows:
            self.map_tree.insert("", "end", text=r["functionality"],
                                 values=(r["mapping"],))

    def _map_select(self, _e):
        sel = self.map_tree.focus()
        if not sel:
            return
        self.m_func.set(self.map_tree.item(sel, "text"))
        vals = self.map_tree.item(sel, "values")
        self.m_map.set(vals[0] if vals else "")

    def _map_save(self):
        f = self.m_func.get().strip()
        if not f:
            return
        self.maps.upsert(f, self.m_map.get().strip())
        self._map_refresh()

    def _map_remove(self):
        f = self.m_func.get().strip()
        if not f:
            return
        self.maps.remove(f)
        self._map_refresh()

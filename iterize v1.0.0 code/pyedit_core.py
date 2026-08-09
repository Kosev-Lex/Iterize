"""
Iterize IDE by JL Kosev-Lex. Copyright (c) 2026. Iterize.org
This program is hereby made available as open source software under Apache 2.0 license terms.


pyedit.py — a multi AI agentic coding editor.

Architecture (single-responsibility components, IDE is only an orchestrator):
  ProcessManager     — single-slot subprocess runner; stdin attached; all output
                       marshalled to the UI thread through one queue
  Console            — output pane + persistent interactive input line:
                       process alive → stdin; idle → shell command. Repeats forever.
  VersionManager     — originals (frozen, read-only) / active ('a') / iterations
                       chain. Deliberate Unfreeze supported; frozen items can be
                       deleted only after explicit unfreeze-and-delete confirm.
  DesignationManager — global top-down ID system: P1 M# C# F# (F = function/method,
                       distinct from M = module). IDs are stable: deletions retire a
                       number (1,2,3,5,6…), edits append revision suffixes
                       4(a), 4(b) … 4(z), then 4(a)(i). Everything is persisted in
                       designations.json at the project root, with an action log
                       (added / revised / deleted / restored / iteration). The
                       description/role/outline fields are null in the schema and
                       are completed later by the configured LLM API.
                       designations.json is copied alongside every 'Save as
                       Iteration' as name_N_timestamp.json.
  DependencyDialog   — pip browser for the selected interpreter (version, size,
                       details, uninstall, install). Packages are global to the
                       interpreter, not per-project: installing an already-present
                       package asks for confirmation first. A package can be added
                       to the project via requirements.txt.
  ApiSettingsDialog  — Tools ▸ API Settings…: provider / base URL / model / key,
                       stored in ~/.pyedit_config.json.
  InstructionsDialog — Tools ▸ Instructions Setup…: author the mission
                       instructions (typed or copied in from the chat pane)
                       → <project>/agents/instructions_mission.md; the Agent
                       Workspace Mission box auto-populates from it.
  Editor             — gutter, syntax highlight, read-only mode
  IDE                — menu, project tree (expansion-preserving refresh, OS file
                       paste), tabs, Structure panel with per-entity open/collapse
                       checkboxes (green = open) and a Snapshot button that writes
                       name_overview_timestamp.md / name_open_timestamp.md into an
                       auto-created Snapshots/ dir.

v6 protections: every persist (files, designations, marks, config,
chats, snapshots) is atomic (tmp + os.replace); damaged JSON is backed
up aside and reported, never silently reset; closing the window, closing
tabs in bulk, and switching projects all guard dirty buffers; renames
and deletes retarget or close their open tabs; Mark-as-Original archives
the on-screen buffer, not a stale disk copy; autosave records designation
CURRENCY (stale flag) without minting revisions; window geometry, sash
positions, chat visibility, and per-project open tabs persist across
sessions (~/.pyedit_ui.json); syntax highlight / gutter / Structure
rebuilds are debounced off the keystroke path.

Pure standard library.  Run:  python pyedit.py  [optional/path]
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.request
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from common import (api_chat, api_complete, atomic_write,
                    atomic_write_json, attach_context_menu,
                    backup_damaged, extract_json, load_api_config,
                    now_iso, persist_geometry, save_api_config,
                    sha1_text, UIState, API_CONFIG_PATH,
                    PROVIDER_PRESETS, resolve_python_interpreter)

_extract_json = extract_json
_sha1 = sha1_text

# ──────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────

FONT = ("Consolas", 11)
TAB_SPACES = 4
PY_EXTS = (".py", ".pyw")

THEME = {
    "bg": "#1e1f22", "fg": "#dcdcdc", "gutter_bg": "#2b2d30", "gutter_fg": "#606366",
    "sel": "#214283", "panel": "#2b2d30", "panel_fg": "#bbbbbb", "accent": "#3574f0",
    "kw": "#cf8e6d", "str": "#6aab73", "num": "#2aacb8", "comment": "#7a7e85",
    "builtin": "#c77dbb", "console_bg": "#1e1f22", "console_fg": "#dcdcdc",
    "err": "#f75464", "stdin": "#6aab73", "open_chk": "#6aab73",
    "code_bg": "#111418",
}

KEYWORDS = {
    "False", "None", "True", "and", "as", "assert", "async", "await", "break",
    "class", "continue", "def", "del", "elif", "else", "except", "finally", "for",
    "from", "global", "if", "import", "in", "is", "lambda", "nonlocal", "not", "or",
    "pass", "raise", "return", "try", "while", "with", "yield", "match", "case",
}
BUILTINS = {
    "print", "len", "range", "int", "str", "float", "list", "dict", "set", "tuple",
    "bool", "open", "input", "enumerate", "zip", "map", "filter", "sum", "min", "max",
    "abs", "sorted", "reversed", "type", "isinstance", "super", "object", "self",
}

JS_KW = {"function","var","let","const","if","else","for","while","do","return",
         "class","new","this","typeof","instanceof","switch","case","break",
         "continue","try","catch","finally","throw","async","await","import",
         "export","default","extends","super","null","undefined","true","false",
         "delete","in","of","yield","static","get","set"}
CSS_KW = {"display","position","color","background","margin","padding","border",
          "width","height","font","font-size","font-family","flex","grid","float",
          "clear","overflow","opacity","content","transform","transition",
          "animation","cursor","hover","active","root","media","auto","none",
          "block","inline","absolute","relative","fixed","solid","bold","center",
          "important","align-items","justify-content","box-shadow","z-index"}
HTML_KW = {"html","head","body","div","span","p","a","img","ul","ol","li","table",
           "tr","td","th","form","input","button","select","option","script",
           "style","link","meta","title","h1","h2","h3","h4","h5","h6","br","hr",
           "nav","header","footer","section","article","main","label","textarea",
           "canvas","iframe","strong","em","class","id","src","href","type",
           "value","name","rel","charset"}
SQL_KW = {"select","from","where","insert","into","values","update","set","delete",
          "create","table","drop","alter","index","join","inner","left","right",
          "outer","on","as","and","or","not","null","primary","key","foreign",
          "references","unique","order","by","group","having","limit","offset",
          "distinct","union","all","exists","in","like","between","is","case",
          "when","then","else","end","begin","commit","rollback","transaction",
          "integer","text","varchar","real","blob","default","constraint","if"}

LANG_DEFS = {
    "python": {"kw": KEYWORDS, "extra": BUILTINS, "line": "#", "block": None},
    "javascript": {"kw": JS_KW, "extra": set(), "line": "//",
                   "block": ("/*", "*/")},
    "css": {"kw": CSS_KW, "extra": set(), "line": None, "block": ("/*", "*/")},
    "html": {"kw": HTML_KW, "extra": set(), "line": None,
             "block": ("<!--", "-->")},
    "sql": {"kw": SQL_KW, "extra": set(), "line": "--", "block": ("/*", "*/"),
            "nocase": True},
}
EXT_LANG = {".py": "python", ".pyw": "python", ".js": "javascript",
            ".jsx": "javascript", ".ts": "javascript", ".css": "css",
            ".html": "html", ".htm": "html", ".sql": "sql"}
_RX: dict = {}
_SSTR = (r"'(?:\\.|[^'\\" + "\\n" + r"])*'" + "|"
         + r'"(?:\\.|[^"\\' + "\\n" + r'])*"')


def lang_for(path: str) -> str:
    return EXT_LANG.get(os.path.splitext(path or "")[1].lower(), "text")


def _lang_regex(lang: str):
    if lang in _RX:
        return _RX[lang]
    d = LANG_DEFS[lang]
    com = []
    if d.get("block"):
        o, c = d["block"]
        com.append("(?s:" + re.escape(o) + ".*?" + re.escape(c) + ")")
    if d.get("line"):
        com.append(re.escape(d["line"]) + "[^\\n]*")
    parts = []
    if com:
        parts.append("(?P<comment>" + "|".join(com) + ")")
    if lang == "python":
        t1, t2 = "'" * 3, chr(34) * 3
        parts.append("(?P<str>" + t1 + "(?s:.*?)" + t1 + "|"
                     + t2 + "(?s:.*?)" + t2 + "|" + _SSTR + ")")
    else:
        parts.append("(?P<str>" + _SSTR + ")")
    parts.append(r"(?P<num>\b\d+\.?\d*\b)")
    parts.append(r"(?P<word>[A-Za-z_][A-Za-z0-9_\-]*)" if lang == "css"
                 else r"(?P<word>\b\w+\b)")
    _RX[lang] = re.compile("|".join(parts))
    return _RX[lang]


TOKEN_RE = re.compile(r"""
    (?P<comment>\#[^\n]*) |
    (?P<str>'''(?:.|\n)*?'''|\"\"\"(?:.|\n)*?\"\"\"|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\") |
    (?P<num>\b\d+\.?\d*\b) |
    (?P<word>\b\w+\b)
""", re.VERBOSE)

GITIGNORE = """__pycache__/
*.py[cod]
.venv/
venv/
env/
build/
dist/
*.spec
.idea/
.vscode/
"""

MAIN_TEMPLATE = '''"""{name} — entry point."""


def main():
    print("Hello from {name}")


if __name__ == "__main__":
    main()
'''

ORIGINALS_DIR = "originals"
ITERATIONS_DIR = "iterations"
SNAPSHOTS_DIR = "Snapshots"
DESIGNATIONS_FILE = "designations.json"


# Runs inside the *selected* interpreter so the package list always matches it.
DEP_LIST_SCRIPT = r"""
import json, os
import importlib.metadata as md
out = []
for d in md.distributions():
    try:
        name = d.metadata.get("Name") or ""
    except Exception:
        name = ""
    if not name:
        continue
    size = 0
    try:
        for f in (d.files or []):
            try:
                size += os.path.getsize(d.locate_file(f))
            except (OSError, TypeError):
                pass
    except Exception:
        pass
    out.append({"name": name, "version": d.version or "", "size": size})
print(json.dumps(out))
"""

ANNOTATE_MODULE_PROMPT = """You are annotating a code designation index for a Python module.
Respond with ONLY a JSON object — no prose, no markdown fences — matching:
{
  "description": "what this module is and does",
  "role": "role it plays in the overall system",
  "major_components": "its major components, one line",
  "interacts_with": ["other modules/parts it interacts with"],
  "classes": {"ClassName": {"description": "...", "role": "...",
              "methods": {"method_name": "one-line description"}}},
  "functions": {"function_name": "one-line description"}
}
Only include names that appear in the skeleton.

SKELETON:
%s

SOURCE:
%s
"""

ANNOTATE_PROJECT_PROMPT = """You are writing the top-level entry of a code designation index.
Respond with ONLY a JSON object — no prose, no markdown fences:
{"description": "...", "how_it_works": "...", "outline": "..."}
- description: what the codebase is and does
- how_it_works: how it works end to end
- outline: full general outline of the system, top-down

MODULES (relpath, id, description):
%s
"""


# ──────────────────────────────────────────────────────────────────────────
# Small helpers
# ──────────────────────────────────────────────────────────────────────────

def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(path), root]) == root
    except ValueError:
        return False


def iter_defs(node):
    """Yield def/class statements in a scope, looking through transparent
    containers (if/try/with) so e.g. functions defined under
    `if __name__ == "__main__":` register in designations and Structure."""
    stack = list(getattr(node, "body", []))
    while stack:
        ch = stack.pop(0)
        if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield ch
        elif isinstance(ch, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
            inner = []
            for attr in ("body", "orelse", "finalbody"):
                inner.extend(getattr(ch, attr, []))
            for h in getattr(ch, "handlers", []):
                inner.extend(h.body)
            stack = inner + stack


def _roman(n: int) -> str:
    out = ""
    for sym, val in (("x", 10), ("ix", 9), ("v", 5), ("iv", 4), ("i", 1)):
        while n >= val:
            out += sym
            n -= val
    return out


def rev_suffix(n: int) -> str:
    """Revision 1 → (a), 2 → (b) … 26 → (z), 27 → (a)(i), 28 → (b)(i) …"""
    if n <= 0:
        return ""
    n -= 1
    letter = chr(ord("a") + n % 26)
    cycle = n // 26
    return f"({letter})" if cycle == 0 else f"({letter})({_roman(cycle)})"


def _rmtree_force(path: str):
    def onerr(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass
    shutil.rmtree(path, onerror=onerr)


# load_api_config / save_api_config / api_chat / api_complete now live in
# common.py (one home, shared by the GUI and the headless kernel) and are
# imported below — the names keep working for everything in this module.


# ──────────────────────────────────────────────────────────────────────────
# Tooltip (used for the Structure checkboxes)
# ──────────────────────────────────────────────────────────────────────────

class Tooltip:
    def __init__(self, widget):
        self.widget = widget
        self.tip = None
        self.text = ""

    def show(self, text, x, y):
        if self.tip and self.text == text:
            self.tip.wm_geometry(f"+{x + 14}+{y + 10}")
            return
        self.hide()
        self.text = text
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x + 14}+{y + 10}")
        tk.Label(tw, text=text, bg="#3c3f41", fg="#dcdcdc", padx=6, pady=2,
                 font=("Segoe UI", 9)).pack()

    def hide(self):
        if self.tip:
            self.tip.destroy()
            self.tip = None
            self.text = ""


# ──────────────────────────────────────────────────────────────────────────
# ChatPane — collapsible right-hand API chat (Instructions / Answer)
# ──────────────────────────────────────────────────────────────────────────

class ChatPane(tk.Frame):
    """Instructions on top (typing area + editable project prompt with
    include/hide toggle + cumulative toggle), Answer below (conversational
    replies; fenced code rendered on a contrast background with a per-block
    copy button). Collapsed/expanded via the slim tab strip in the IDE."""

    FENCE_RE = re.compile(r"```[\w+-]*\n?(.*?)```", re.S)

    def __init__(self, master, on_send):
        super().__init__(master, bg=THEME["panel"])
        self.on_send = on_send          # on_send(system_or_None, messages)
        self.messages: list[dict] = []  # conversation turns (user/assistant)
        self.cum_log: list[str] = []    # cumulative instruction blocks
        self._busy = False

        head = tk.Frame(self, bg=THEME["panel"]); head.pack(fill="x")
        tk.Label(head, text="Assistant", bg=THEME["panel"], fg=THEME["panel_fg"],
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=6, pady=3)
        self.tok_lbl = tk.Label(head, text="≈0 tok ctx", bg=THEME["panel"],
                                fg=THEME["gutter_fg"])
        self.tok_lbl.pack(side="left", padx=6)
        tk.Button(head, text="Reset", command=self.reset, relief="flat",
                  padx=6).pack(side="right", padx=4)
        tk.Button(head, text="↑ last Q", command=self._goto_last_q,
                  relief="flat", padx=6).pack(side="right")
        self._lastq = None
        self.conv_ts = time.strftime("%Y%m%d-%H%M%S")   # one file per conversation

        # project prompt — hidden ⇒ excluded from messages; shown ⇒ sent as system
        self._prow = tk.Frame(self, bg=THEME["panel"]); self._prow.pack(fill="x")
        self._prompt_shown = True
        self._prompt_btn = tk.Button(self._prow, text="▾ Project prompt (included)",
                                     command=self._toggle_prompt, relief="flat",
                                     anchor="w", bg=THEME["panel"],
                                     fg=THEME["panel_fg"])
        self._prompt_btn.pack(fill="x")
        self.prompt = tk.Text(self, height=4, bg=THEME["bg"], fg=THEME["fg"],
                              insertbackground=THEME["fg"], font=FONT, wrap="word")
        self.prompt.pack(fill="x", padx=4, pady=(0, 4))
        attach_context_menu(self.prompt)

        irow = tk.Frame(self, bg=THEME["panel"]); irow.pack(fill="x")
        tk.Label(irow, text="Instructions", bg=THEME["panel"], fg=THEME["panel_fg"],
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=6)
        self.cumulative = tk.BooleanVar(value=False)
        tk.Checkbutton(irow, text="cumulative", variable=self.cumulative,
                       bg=THEME["panel"], fg=THEME["panel_fg"],
                       selectcolor=THEME["bg"],
                       activebackground=THEME["panel"]).pack(side="left")
        self.send_btn = tk.Button(irow, text="Send", command=self.send,
                                  bg=THEME["accent"], fg="white", relief="flat",
                                  padx=10)
        self.send_btn.pack(side="right", padx=4, pady=2)

        self.instructions = tk.Text(self, height=6, bg=THEME["bg"], fg=THEME["fg"],
                                    insertbackground=THEME["fg"], font=FONT,
                                    wrap="word")
        self.instructions.pack(fill="x", padx=4, pady=(0, 4))
        self.instructions.bind("<Control-Return>",
                               lambda e: (self.send(), "break")[1])
        attach_context_menu(self.instructions)

        arow = tk.Frame(self, bg=THEME["panel"]); arow.pack(fill="x", padx=6)
        tk.Label(arow, text="Answer", bg=THEME["panel"], fg=THEME["panel_fg"],
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left")
        self.busy_lbl = tk.Label(arow, text="", bg=THEME["panel"],
                                 fg=THEME["accent"], anchor="e")
        self.busy_lbl.pack(side="right")
        af = tk.Frame(self, bg=THEME["console_bg"])
        af.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.answer = tk.Text(af, bg=THEME["console_bg"], fg=THEME["fg"],
                              font=FONT, wrap="word", state="disabled", padx=6)
        avsb = ttk.Scrollbar(af, command=self.answer.yview)
        self.answer.configure(yscrollcommand=avsb.set)
        self.answer.pack(side="left", fill="both", expand=True)
        avsb.pack(side="right", fill="y")
        self._blocks: dict[str, str] = {}
        self.answer.tag_configure("code", background=THEME["code_bg"],
                                  foreground="#bfe3bf",
                                  lmargin1=8, lmargin2=8)
        self.answer.tag_configure("you", foreground="#9fc1ff",
                                  background="#232f45",
                                  lmargin1=4, lmargin2=4)
        self.answer.tag_configure("err", foreground=THEME["err"])
        self.answer.bind("<Button-3>", self._answer_menu)

    # ── prompt visibility ⇔ inclusion ──
    def _toggle_prompt(self):
        self._prompt_shown = not self._prompt_shown
        if self._prompt_shown:
            self.prompt.pack(fill="x", padx=4, pady=(0, 4), after=self._prow)
            self._prompt_btn.config(text="▾ Project prompt (included)")
        else:
            self.prompt.pack_forget()
            self._prompt_btn.config(text="▸ Project prompt (hidden — not sent)")

    def _system(self):
        txt = self.prompt.get("1.0", "end-1c").strip()
        return txt if (self._prompt_shown and txt) else None

    # ── send / receive ──
    def send(self):
        if self._busy:
            return
        text = self.instructions.get("1.0", "end-1c").strip()
        if not text:
            return
        if self.cumulative.get():
            self.cum_log.append(text)
            body = "\n\n".join(self.cum_log)
        else:
            body = text
        self.instructions.delete("1.0", "end")
        self.messages.append({"role": "user", "content": body})
        self._lastq = self.answer.index("end-1c")
        self._append(f"\n➤ {body}\n", "you")
        self._update_tokens()
        self._busy = True
        self.send_btn.config(text="…", state="disabled")
        self.busy_lbl.config(text="● generating — waiting for API reply…")
        self.on_send(self._system(), list(self.messages))

    def receive(self, text):
        self.messages.append({"role": "assistant", "content": text})
        self._render(text)
        self._update_tokens()
        self._done()

    def error(self, msg):
        # drop the failed turn so a retry doesn't duplicate it
        if self.messages and self.messages[-1]["role"] == "user":
            self.messages.pop()
        if self.cumulative.get() and self.cum_log:
            self.instructions.insert("1.0", self.cum_log.pop())
        self._append(f"API error: {msg}\n", "err")
        self._done()

    def _done(self):
        self._busy = False
        self.send_btn.config(text="Send", state="normal")
        self.busy_lbl.config(text="")

    def reset(self):
        self.messages.clear()
        self.cum_log.clear()
        self._blocks.clear()
        self._lastq = None
        self._update_tokens()
        # a new conversation gets a NEW file — the previous conversation's
        # record is closed and never written again
        self.conv_ts = time.strftime("%Y%m%d-%H%M%S")
        self.answer.configure(state="normal")
        self.answer.delete("1.0", "end")
        self.answer.configure(state="disabled")

    # ── answer rendering ──
    def _append(self, text, tag=None):
        self.answer.configure(state="normal")
        self.answer.insert("end", text, tag or ())
        self.answer.see("end")
        self.answer.configure(state="disabled")

    def _render(self, text):
        self.answer.configure(state="normal")
        pos = 0
        for m in self.FENCE_RE.finditer(text):
            self._insert_prose(text[pos:m.start()])
            self._insert_code(m.group(1))
            pos = m.end()
        self._insert_prose(text[pos:])
        self.answer.insert("end", "\n")
        self.answer.see("end")
        self.answer.configure(state="disabled")

    def _insert_prose(self, chunk):
        if chunk.strip():
            self.answer.insert("end", chunk.strip("\n") + "\n")

    def _insert_code(self, code):
        code = code.rstrip("\n")
        cb = f"cb{len(self._blocks)}"
        self._blocks[cb] = code
        btn = tk.Button(self.answer, text="copy", relief="flat", padx=4, pady=0,
                        font=("Segoe UI", 8), bg=THEME["panel"],
                        fg=THEME["panel_fg"],
                        command=lambda c=code: self._copy(c))
        self.answer.insert("end", "\n")
        self.answer.window_create("end", window=btn)
        self.answer.insert("end", "\n" + code + "\n", ("code", cb))
        self.answer.tag_bind(cb, "<Double-Button-1>",
                             lambda e, c=code: self._copy(c))
        btn2 = tk.Button(self.answer, text="copy", relief="flat", padx=4,
                         pady=0, font=("Segoe UI", 8), bg=THEME["panel"],
                         fg=THEME["panel_fg"],
                         command=lambda c=code: self._copy(c))
        self.answer.window_create("end", window=btn2)
        self.answer.insert("end", "\n")

    def _answer_menu(self, e):
        idx = self.answer.index(f"@{e.x},{e.y}")
        cb = next((t for t in self.answer.tag_names(idx)
                   if t.startswith("cb")), None)
        m = tk.Menu(self.answer, tearoff=0)
        if cb:                       # right-click anywhere inside a block
            m.add_command(label="Copy code block",
                          command=lambda c=self._blocks[cb]: self._copy(c))
        def copy_sel():
            try:
                self._copy(self.answer.selection_get())
            except tk.TclError:
                pass
        m.add_command(label="Copy selection", command=copy_sel)
        m.add_command(label="Select All",
                      command=lambda: self.answer.tag_add("sel", "1.0",
                                                          "end-1c"))
        m.tk_popup(e.x_root, e.y_root)

    def _copy(self, code):
        self.clipboard_clear()
        self.clipboard_append(code)

    def _goto_last_q(self):
        if self._lastq:
            total = max(float(self.answer.index("end-1c").split(".")[0]), 1.0)
            line = float(self.answer.index(self._lastq).split(".")[0])
            self.answer.yview_moveto(max(0.0, (line - 1) / total))

    def _update_tokens(self):
        total = (len(self._system() or "")
                 + sum(len(m["content"]) for m in self.messages))
        self.tok_lbl.config(text=f"≈{total // 4} tok ctx")

    def export_md(self) -> str:
        out = [f"# Chat session — {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
        p = self.prompt.get("1.0", "end-1c").strip()
        if p:
            out += ["## Project prompt", "", p, ""]
        if self.cum_log:
            out += ["## Cumulative instructions", ""]
            out += [f"- {c}" for c in self.cum_log] + [""]
        for m in self.messages:
            out += [f"## {'You' if m['role'] == 'user' else 'Assistant'}",
                    "", m["content"], ""]
        return "\n".join(out)


# ──────────────────────────────────────────────────────────────────────────
# ProcessManager — one running process at a time, stdin attached
# ──────────────────────────────────────────────────────────────────────────

class ProcessManager:
    """All child processes go through here. Output lines are pushed onto a
    queue as (tag, payload) tuples; the UI drains it on the Tk thread."""

    def __init__(self, out_q: "queue.Queue[tuple]"):
        self.q = out_q
        self.proc: subprocess.Popen | None = None
        self._busy = False

    def running(self) -> bool:
        return self._busy or (self.proc is not None and self.proc.poll() is None)

    def spawn(self, args, cwd, shell=False, on_done=None) -> bool:
        if self.running():
            self.q.put(("err", "A process is already running. Stop it first.\n"))
            return False
        self._busy = True

        def work():
            try:
                try:
                    self.proc = subprocess.Popen(
                        args, cwd=cwd, shell=shell,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1)
                except Exception as e:  # noqa: BLE001
                    self.q.put(("err", f"Failed to start: {e}\n"))
                    return
                for line in self.proc.stdout:
                    self.q.put(("out", line))
                self.proc.wait()
                self.q.put(("meta", f"[exit {self.proc.returncode}]\n"))
                if on_done:
                    self.q.put(("callback", on_done))
            finally:
                self._busy = False
        threading.Thread(target=work, daemon=True).start()
        return True

    def write_stdin(self, text: str):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write(text)
                self.proc.stdin.flush()
            except OSError as e:
                self.q.put(("err", f"stdin: {e}\n"))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ──────────────────────────────────────────────────────────────────────────
# Console — output pane + persistent interactive input line
# ──────────────────────────────────────────────────────────────────────────

class Console(tk.Frame):
    """The input line never dies: submit a command,
    it runs, prompt is ready again. If a process is alive, input is routed
    to its stdin (so input() etc. work); otherwise it is a shell command."""

    def __init__(self, master, on_submit):
        super().__init__(master, bg=THEME["console_bg"])
        self.on_submit = on_submit
        self.history: list[str] = []
        self.hidx = 0

        row = tk.Frame(self, bg=THEME["panel"])
        row.pack(side="bottom", fill="x")
        tk.Label(row, text="❯", bg=THEME["panel"], fg=THEME["accent"],
                 font=FONT).pack(side="left", padx=(8, 4))
        self.entry = tk.Entry(row, bg=THEME["bg"], fg=THEME["fg"],
                              insertbackground=THEME["fg"], relief="flat", font=FONT)
        self.entry.pack(side="left", fill="x", expand=True, pady=4, padx=(0, 8))
        self.entry.bind("<Return>", self._submit)
        self.entry.bind("<Up>", lambda e: self._hist(-1))
        self.entry.bind("<Down>", lambda e: self._hist(+1))
        attach_context_menu(self.entry)

        self.out = tk.Text(self, bg=THEME["console_bg"], fg=THEME["console_fg"],
                           font=FONT, border=0, padx=8, state="disabled", wrap="word")
        self.out.pack(side="top", fill="both", expand=True)
        self.out.tag_configure("err", foreground=THEME["err"])
        self.out.tag_configure("meta", foreground=THEME["accent"])
        self.out.tag_configure("stdin", foreground=THEME["stdin"])
        attach_context_menu(self.out, read_only=True)

    def _submit(self, _e):
        line = self.entry.get()
        self.entry.delete(0, "end")
        if line.strip():
            self.history.append(line)
        self.hidx = len(self.history)
        self.on_submit(line)
        return "break"

    def _hist(self, delta):
        if not self.history:
            return "break"
        self.hidx = max(0, min(len(self.history), self.hidx + delta))
        self.entry.delete(0, "end")
        if self.hidx < len(self.history):
            self.entry.insert(0, self.history[self.hidx])
        return "break"

    def write(self, text, tag=None):
        self.out.configure(state="normal")
        self.out.insert("end", text, tag or ())
        self.out.see("end")
        self.out.configure(state="disabled")

    def clear(self):
        self.out.configure(state="normal")
        self.out.delete("1.0", "end")
        self.out.configure(state="disabled")


# ──────────────────────────────────────────────────────────────────────────
# VersionManager — originals (frozen) / active / iterations, with unfreeze
# ──────────────────────────────────────────────────────────────────────────

class VersionManager:
    def __init__(self, project_dir: str):
        self.root = os.path.abspath(project_dir)
        self.originals = os.path.join(self.root, ORIGINALS_DIR)
        self.iterations = os.path.join(self.root, ITERATIONS_DIR)

    # ── queries ──
    def _rel(self, path: str) -> str | None:
        rel = os.path.relpath(os.path.abspath(path), self.root)
        return None if rel.startswith("..") else rel

    def in_archive(self, path: str) -> bool:
        return _within(path, self.originals)

    def is_frozen(self, path: str) -> bool:
        """Frozen = inside originals/ AND still read-only (not yet unfrozen)."""
        return (self.in_archive(path) and os.path.isfile(path)
                and not os.access(path, os.W_OK))

    def _is_versionable(self, path: str) -> bool:
        return (os.path.isfile(path)
                and self._rel(path) is not None
                and not self.in_archive(path)
                and not _within(path, self.iterations))

    def is_active(self, path: str) -> bool:
        if not self._is_versionable(path):
            return False
        return os.path.isfile(os.path.join(self.originals, self._rel(path)))

    # ── operations ──
    def mark_original(self, path: str) -> str:
        if not self._is_versionable(path):
            raise ValueError("Only project files outside originals/ and "
                             "iterations/ can be marked as original.")
        dest = os.path.join(self.originals, self._rel(path))
        if os.path.exists(dest):
            raise ValueError("Already marked — a frozen original exists.")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        os.chmod(dest, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)  # freeze
        return dest

    def unfreeze(self, path: str):
        """Deliberate unfreeze: the archived copy becomes writable/deletable."""
        if not self.in_archive(path):
            raise ValueError("Not inside originals/ — nothing to unfreeze.")
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

    def save_iteration(self, path: str) -> str:
        if not self._is_versionable(path):
            raise ValueError("Only project files outside originals/ and "
                             "iterations/ can be saved as an iteration.")
        rel = self._rel(path)
        it_dir = os.path.join(self.iterations, os.path.dirname(rel))
        os.makedirs(it_dir, exist_ok=True)
        base, ext = os.path.splitext(os.path.basename(path))
        pat = re.compile(rf"^{re.escape(base)}_(\d+)_\d{{8}}-\d{{6}}{re.escape(ext)}$")
        nums = [int(m.group(1)) for f in os.listdir(it_dir) if (m := pat.match(f))]
        n = max(nums, default=0) + 1
        ts = time.strftime("%Y%m%d-%H%M%S")
        dest = os.path.join(it_dir, f"{base}_{n}_{ts}{ext}")
        shutil.copy2(path, dest)
        return dest


# ──────────────────────────────────────────────────────────────────────────
# DesignationManager — P1 M# C# F# identity system + designations.json
# ──────────────────────────────────────────────────────────────────────────

class DesignationManager:
    """Global, top-down identity for every module/class/function in a project.

    Rules:
      * Numbers are assigned once per scope and never reused. A deleted entity
        keeps its record (deleted=true) so the Structure shows 1,2,3,5,6.
      * If an entity's source hash changes on save, its revision increments and
        the display id gains a suffix: F4 → F4(a) → F4(b) … → F4(a)(i).
      * A deleted name that reappears is restored (same id, revision bumped).
      * Every action is appended to the log[] with a timestamp.
      * description/role/outline fields are null until completed by the API.
    """

    SCHEMA = "pyedit.designations/1"

    def __init__(self, project_dir: str):
        self.root = os.path.abspath(project_dir)
        self.path = os.path.join(self.root, DESIGNATIONS_FILE)
        self.load_error = ""      # surfaced by the IDE after open_project
        self.data = self._load()

    # ── persistence ──
    def _load(self) -> dict:
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError as e:
                bak = backup_damaged(self.path)
                self.load_error = (
                    f"designations.json is DAMAGED ({e}) — the damaged "
                    f"file was preserved as "
                    f"{os.path.basename(bak) if bak else '(backup failed)'}"
                    f" and a fresh index was started")
            except OSError as e:
                self.load_error = f"designations.json unreadable: {e}"
        return self._skeleton()

    def _skeleton(self) -> dict:
        return {
            "_schema": self.SCHEMA,
            "_note": ("Null description/role/outline fields are completed by the "
                      "configured LLM API (Tools ▸ Annotate…). Ids: P=project, "
                      "M=module, C=class, F=function/method. Revision suffixes: "
                      "(a),(b)…(z),(a)(i)…"),
            "project": {
                "id": "P1",
                "name": os.path.basename(self.root),
                "created": now_iso(),
                "updated": now_iso(),
                "description": None,     # ← completed by API
                "how_it_works": None,    # ← completed by API
                "outline": None,         # ← completed by API
            },
            "counters": {"next_module": 1},
            "modules": {},
            "log": [],
        }

    def save(self):
        try:
            atomic_write_json(self.path, self.data)
        except OSError:
            pass

    def _log_action(self, action, designation, name, module):
        self.data["log"].append({"ts": now_iso(), "action": action,
                                 "designation": designation, "name": name,
                                 "module": module})

    # ── entity records ──
    @staticmethod
    def _new_entity(idd: str, kind: str, sig: str | None) -> dict:
        e = {"id": idd, "deleted": False, "revision": 0, "hash": "",
             "description": None,                       # ← completed by API
             "next_class": 1, "next_function": 1,
             "classes": {}, "functions": {}}
        if kind == "C":
            e["role"] = None                            # ← completed by API
        else:
            e["signature"] = sig
        return e

    # ── sync ──
    def sync_module(self, rel: str, source: str) -> bool:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return False
        mods = self.data["modules"]
        mod = mods.get(rel)
        if mod is None:
            mid = f"M{self.data['counters']['next_module']}"
            self.data["counters"]["next_module"] += 1
            mod = {"id": mid, "deleted": False,
                   "description": None, "role": None,          # ← API
                   "major_components": None,                   # ← API
                   "interacts_with": None,                     # ← API
                   "next_class": 1, "next_function": 1,
                   "classes": {}, "functions": {}}
            mods[rel] = mod
            self._log_action("added", f"P1{mid}", rel, rel)
        elif mod.get("deleted"):
            mod["deleted"] = False
            self._log_action("restored", f"P1{mod['id']}", rel, rel)
        lines = source.split("\n")
        self._sync_scope(mod, tree, source, lines, "P1" + mod["id"], rel)
        # formal checkpoint: currency now equals the checkpoint
        mod["file_hash"] = _sha1(source)
        mod["current_hash"] = mod["file_hash"]
        mod["stale"] = False
        self.data["project"]["updated"] = now_iso()
        self.save()
        return True

    def _sync_scope(self, rec, node, source, lines, prefix, rel):
        rec.setdefault("classes", {})
        rec.setdefault("functions", {})
        rec.setdefault("next_class", 1)
        rec.setdefault("next_function", 1)
        # gather children with content hash (drives revisions) and body hash
        # (drives rename detection: body minus the def line / member names)
        kids = []
        for child in iter_defs(node):
            if isinstance(child, ast.ClassDef):
                bucket, letter, counter = "classes", "C", "next_class"
                h = _sha1(lines[child.lineno - 1].strip())
                bh = _sha1("|".join(sorted(c.name for c in iter_defs(child))))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bucket, letter, counter = "functions", "F", "next_function"
                seg = ast.get_source_segment(source, child) or ""
                h = _sha1(seg)
                bh = _sha1(seg.split("\n", 1)[1] if "\n" in seg else "")
            else:
                continue
            kids.append((child, bucket, letter, counter, h, bh))

        seen = {"classes": {k[0].name for k in kids if k[1] == "classes"},
                "functions": {k[0].name for k in kids if k[1] == "functions"}}

        # rename detection: a new name whose body-hash matches a vanishing
        # sibling keeps that sibling's id (revision bumped, action logged) —
        # a rename is not a delete + new number.
        for child, bucket, letter, counter, h, bh in kids:
            if child.name in rec[bucket] or not bh:
                continue
            gone = sorted(n for n, e in rec[bucket].items()
                          if n not in seen[bucket] and not e.get("deleted")
                          and e.get("bhash") == bh)
            if gone:
                old = gone[0]
                ent = rec[bucket].pop(old)
                ent["revision"] = ent.get("revision", 0) + 1
                ent["hash"], ent["bhash"] = h, bh
                rec[bucket][child.name] = ent
                self._log_action(
                    "renamed",
                    prefix + ent["id"] + rev_suffix(ent["revision"]),
                    f"{old} → {child.name}", rel)

        for child, bucket, letter, counter, h, bh in kids:
            sig = lines[child.lineno - 1].strip().rstrip(":")
            ent = rec[bucket].get(child.name)
            if ent is None:
                idd = f"{letter}{rec[counter]}"
                rec[counter] += 1
                ent = self._new_entity(idd, letter, sig)
                ent["hash"], ent["bhash"] = h, bh
                rec[bucket][child.name] = ent
                self._log_action("added", prefix + idd, child.name, rel)
            else:
                if ent.get("deleted"):
                    ent["deleted"] = False
                    ent["revision"] = ent.get("revision", 0) + 1
                    ent["hash"], ent["bhash"] = h, bh
                    self._log_action(
                        "restored",
                        prefix + ent["id"] + rev_suffix(ent["revision"]),
                        child.name, rel)
                elif ent.get("hash") != h:
                    ent["revision"] = ent.get("revision", 0) + 1
                    ent["hash"], ent["bhash"] = h, bh
                    self._log_action(
                        "revised",
                        prefix + ent["id"] + rev_suffix(ent["revision"]),
                        child.name, rel)
                else:
                    ent["bhash"] = bh            # backfill legacy records
                if letter == "F":
                    ent["signature"] = sig
            self._sync_scope(ent, child, source, lines, prefix + ent["id"], rel)
        for bucket in ("classes", "functions"):
            for name, ent in rec[bucket].items():
                if name not in seen[bucket] and not ent.get("deleted"):
                    ent["deleted"] = True
                    self._log_action("deleted", prefix + ent["id"], name, rel)

    # ── lookups ──
    def designation(self, rel: str, chain: list) -> tuple | None:
        """chain = [("C", "Foo"), ("F", "bar")] → ("P1M1C2F4(a)", record)."""
        mod = self.data["modules"].get(rel)
        if not mod or mod.get("deleted"):
            return None
        parts = ["P1", mod["id"]]
        node = mod
        for kind, name in chain:
            node = node.get("classes" if kind == "C" else "functions", {}).get(name)
            if node is None:
                return None
            parts.append(node["id"] + rev_suffix(node.get("revision", 0)))
        return "".join(parts), node

    def mark_deleted_under(self, rel_prefix: str):
        p = rel_prefix.rstrip("/")
        for rel, mod in self.data["modules"].items():
            if (rel == p or rel.startswith(p + "/")) and not mod.get("deleted"):
                mod["deleted"] = True
                self._log_action("deleted", "P1" + mod["id"], rel, rel)
        self.save()

    def reconcile(self) -> bool:
        """Filesystem is truth: mark registered modules whose files no longer
        exist on disk as deleted. Covers removals that bypass the tree hook
        (console `del`/`rm`, Explorer, external tools)."""
        changed = False
        for rel, mod in self.data["modules"].items():
            if mod.get("deleted"):
                continue
            if not os.path.isfile(os.path.join(self.root, rel.replace("/", os.sep))):
                mod["deleted"] = True
                self._log_action("deleted", "P1" + mod["id"], rel, rel)
                changed = True
        if changed:
            self.save()
        return changed


    def update_current(self, rel: str, source: str):
        """Currency WITHOUT a checkpoint: record the module's current
        content hash and staleness against the last formal sync. Used by
        autosave and the startup scan so the index always knows the disk
        moved — while revision numbers keep reflecting deliberate saves,
        never 30-second ticks."""
        mod = self.data["modules"].get(rel)
        if not mod:
            return
        h = _sha1(source)
        if (mod.get("current_hash") == h
                and mod.get("stale") == (h != mod.get("file_hash"))):
            return
        mod["current_hash"] = h
        mod["stale"] = (h != mod.get("file_hash"))
        self.save()

    def reconcile_files(self, skip_dirs: tuple) -> list:
        """Startup truth pass over the WHOLE project: register .py
        modules that appeared out-of-band (fresh entities start at
        revision 0 — no inflation), and flag registered modules whose
        on-disk content no longer matches their last checkpoint as
        stale (currency only — no revision is minted; the next manual
        save checkpoints them). → list of log lines for the console."""
        notes = []
        seen = set()
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames
                           if d not in skip_dirs
                           and not d.startswith(".")
                           and d != "__pycache__"]
            for fn in filenames:
                if not fn.endswith((".py", ".pyw")):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root).replace(os.sep, "/")
                seen.add(rel)
                try:
                    with open(full, "r", encoding="utf-8",
                              errors="replace") as f:
                        s = f.read()
                except OSError:
                    continue
                mod = self.data["modules"].get(rel)
                if mod is None or mod.get("deleted"):
                    if self.sync_module(rel, s):
                        notes.append(f"registered new module: {rel}")
                    continue
                h = _sha1(s)
                was_stale = mod.get("stale", False)
                mod["current_hash"] = h
                mod["stale"] = (h != mod.get("file_hash", h))
                if mod["stale"] and not was_stale:
                    notes.append(f"stale (edited outside a checkpoint "
                                 f"save): {rel}")
        if notes:
            self.save()
        return notes

    def rename_path(self, old_rel: str, new_rel: str):
        """File or directory rename/move: migrate module keys. Ids, revisions
        and history survive — a rename is not a delete+add."""
        o = old_rel.rstrip("/")
        moved = [(rel, mod) for rel, mod in list(self.data["modules"].items())
                 if rel == o or rel.startswith(o + "/")]
        for rel, mod in moved:
            new_key = new_rel + rel[len(o):]
            del self.data["modules"][rel]
            self.data["modules"][new_key] = mod
            self._log_action("renamed", "P1" + mod["id"],
                             f"{rel} → {new_key}", new_key)
        if moved:
            self.save()

    def log_iteration(self, rel: str, dest_name: str):
        self._log_action("iteration", "", dest_name, rel)
        self.save()

    # ── API annotation ──
    def skeleton_for(self, rel: str) -> dict | None:
        mod = self.data["modules"].get(rel)
        if not mod:
            return None

        def rec_skel(rec):
            return {
                "classes": {n: {"id": e["id"], **rec_skel(e)}
                            for n, e in rec.get("classes", {}).items()
                            if not e.get("deleted")},
                "functions": {n: e["id"]
                              for n, e in rec.get("functions", {}).items()
                              if not e.get("deleted")},
            }
        return {"module": rel, "id": mod["id"], **rec_skel(mod)}

    def apply_annotation(self, rel: str, obj: dict):
        mod = self.data["modules"].get(rel)
        if not mod:
            return
        for k in ("description", "role", "major_components", "interacts_with"):
            if obj.get(k) is not None:
                mod[k] = obj[k]

        def apply_scope(rec, o):
            for name, cd in (o.get("classes") or {}).items():
                ent = rec.get("classes", {}).get(name)
                if not ent or not isinstance(cd, dict):
                    continue
                if cd.get("description") is not None:
                    ent["description"] = cd["description"]
                if cd.get("role") is not None:
                    ent["role"] = cd["role"]
                for mname, mdesc in (cd.get("methods") or {}).items():
                    e2 = ent.get("functions", {}).get(mname)
                    if e2:
                        e2["description"] = (mdesc if isinstance(mdesc, str)
                                             else (mdesc or {}).get("description"))
                apply_scope(ent, cd)
            for name, fd in (o.get("functions") or {}).items():
                ent = rec.get("functions", {}).get(name)
                if not ent:
                    continue
                if isinstance(fd, str):
                    ent["description"] = fd
                elif isinstance(fd, dict) and fd.get("description") is not None:
                    ent["description"] = fd["description"]
        apply_scope(mod, obj)
        self.data["project"]["updated"] = now_iso()
        self.save()


def designation_line_map(source: str, rel: str,
                         manager: DesignationManager | None) -> dict[int, str]:
    """Map every source line to its deepest registered designation.

    Module-level lines receive P1M#, class bodies receive P1M#C#, and function
    bodies receive their complete function/method designation. New unsaved
    definitions inherit their registered parent until the next checkpoint.
    """
    if not (manager and rel):
        return {}
    module = manager.designation(rel, [])
    if not module:
        return {}
    total = max(1, len(source.splitlines()))
    result = {line: module[0] for line in range(1, total + 1)}
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return result

    def walk(node, chain):
        for child in iter_defs(node):
            kind = "C" if isinstance(child, ast.ClassDef) else "F"
            chain2 = chain + [(kind, child.name)]
            found = manager.designation(rel, chain2)
            if found and not found[1].get("deleted"):
                decorators = getattr(child, "decorator_list", ()) or ()
                start = min([child.lineno]
                            + [d.lineno for d in decorators
                               if getattr(d, "lineno", None)])
                end = min(total, getattr(child, "end_lineno", child.lineno))
                for line in range(max(1, start), end + 1):
                    result[line] = found[0]
            walk(child, chain2)

    walk(tree, [])
    return result


# ──────────────────────────────────────────────────────────────────────────
# DependencyDialog — pip package browser for the selected interpreter
# ──────────────────────────────────────────────────────────────────────────

class DependencyDialog(tk.Toplevel):
    def __init__(self, master, interp_getter, project_getter):
        super().__init__(master)
        self.title("Dependencies")
        self.geometry("720x480")
        self.configure(bg=THEME["panel"])
        self.interp_getter = interp_getter
        self.project_getter = project_getter
        self.pkgs: dict[str, str] = {}          # lower-name → version
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._working = False

        top = tk.Frame(self, bg=THEME["panel"])
        top.pack(fill="x", padx=8, pady=(8, 4))
        self.interp_lbl = tk.Label(top, text=self.interp_getter(), bg=THEME["panel"],
                                   fg=THEME["panel_fg"], anchor="w")
        self.interp_lbl.pack(side="left", fill="x", expand=True)
        tk.Button(top, text="Refresh", command=self.refresh, relief="flat",
                  padx=8).pack(side="right")

        cols = ("version", "size")
        self.tree = ttk.Treeview(self, columns=cols, selectmode="browse")
        self.tree.heading("#0", text="Package")
        self.tree.heading("version", text="Version")
        self.tree.heading("size", text="Size")
        self.tree.column("#0", width=300)
        self.tree.column("version", width=120, anchor="w")
        self.tree.column("size", width=100, anchor="e")
        self.tree.pack(fill="both", expand=True, padx=8, pady=4)

        btns = tk.Frame(self, bg=THEME["panel"])
        btns.pack(fill="x", padx=8, pady=4)
        tk.Button(btns, text="Details…", command=self._details, relief="flat",
                  padx=8).pack(side="left")
        tk.Button(btns, text="Uninstall", command=self._uninstall, relief="flat",
                  padx=8).pack(side="left", padx=6)
        tk.Button(btns, text="→ requirements.txt", command=self._add_to_requirements,
                  relief="flat", padx=8).pack(side="left")
        self.install_entry = tk.Entry(btns, width=24)
        attach_context_menu(self.install_entry)
        self.install_entry.pack(side="right", padx=(0, 4))
        self.install_entry.bind("<Return>", lambda e: self._install())
        tk.Button(btns, text="Install", command=self._install, relief="flat",
                  padx=8).pack(side="right", padx=4)

        self.status = tk.Label(self, text="", bg=THEME["panel"], fg=THEME["panel_fg"],
                               anchor="w")
        self.status.pack(fill="x", padx=8, pady=(0, 8))

        self._poll()
        self.refresh()

    # ── background plumbing ──
    def _run_bg(self, args, cb):
        if self._working:
            self.status.config(text="Busy — wait for the current operation.")
            return
        self._working = True

        def work():
            try:
                p = subprocess.run(args, capture_output=True, text=True, timeout=300)
                self._q.put((cb, p))
            except Exception as e:  # noqa: BLE001
                self._q.put((cb, e))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                cb, payload = self._q.get_nowait()
                cb(payload)
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(100, self._poll)

    # ── actions ──
    def refresh(self):
        self.interp_lbl.config(text=self.interp_getter())
        self.status.config(text="Loading package list (sizes are computed)…")
        self._run_bg([self.interp_getter(), "-c", DEP_LIST_SCRIPT], self._on_list)

    def _on_list(self, payload):
        self._working = False
        if isinstance(payload, Exception):
            self.status.config(text=f"Error: {payload}")
            return
        if payload.returncode != 0:
            self.status.config(text=(payload.stderr or "listing failed").strip()[:200])
            return
        try:
            pkgs = json.loads(payload.stdout)
        except json.JSONDecodeError:
            self.status.config(text="Could not parse package list.")
            return
        self.pkgs = {p["name"].lower(): p["version"] for p in pkgs}
        self.tree.delete(*self.tree.get_children())
        total = 0
        for p in sorted(pkgs, key=lambda x: x["name"].lower()):
            total += p["size"]
            self.tree.insert("", "end", text=p["name"],
                             values=(p["version"], human_size(p["size"])))
        self.status.config(text=f"{len(pkgs)} packages · {human_size(total)} total "
                                f"(global to this interpreter, shared by all projects)")

    def _selected(self) -> str | None:
        node = self.tree.focus()
        return self.tree.item(node, "text") if node else None

    def _details(self):
        name = self._selected()
        if not name:
            return
        self.status.config(text=f"pip show {name}…")
        self._run_bg([self.interp_getter(), "-m", "pip", "show", "-f", name],
                     lambda p: self._on_details(name, p))

    def _on_details(self, name, payload):
        self._working = False
        self.status.config(text="")
        text = payload if isinstance(payload, Exception) else \
            (payload.stdout or payload.stderr or "no output")
        win = tk.Toplevel(self)
        win.title(f"Details — {name}")
        win.geometry("560x420")
        t = tk.Text(win, bg=THEME["bg"], fg=THEME["fg"], font=FONT, padx=8, wrap="word")
        t.pack(fill="both", expand=True)
        t.insert("1.0", str(text))
        t.configure(state="disabled")
        attach_context_menu(t, read_only=True)

    def _uninstall(self):
        name = self._selected()
        if not name:
            return
        if not messagebox.askyesno("Uninstall", f"Uninstall {name}?", parent=self):
            return
        self.status.config(text=f"Uninstalling {name}…")
        self._run_bg([self.interp_getter(), "-m", "pip", "uninstall", "-y", name],
                     self._on_pip_done)

    def _install(self):
        raw = self.install_entry.get().strip()
        if not raw:
            return
        # Packages are global to the interpreter — don't blindly reinstall.
        already = []
        for token in raw.split():
            base = re.split(r"[<>=!~;\[\s]", token, 1)[0]
            if base.lower() in self.pkgs:
                already.append(f"{base} {self.pkgs[base.lower()]}")
        if already:
            msg = ("Already installed for this interpreter (site-packages are "
                   "global to the interpreter — shared across all projects, no "
                   "need to reinstall per project):\n\n  "
                   + "\n  ".join(already)
                   + "\n\nReinstall / upgrade anyway?")
            if not messagebox.askyesno("Already installed", msg, parent=self):
                self.status.config(text="Install skipped — already available.")
                return
        self.install_entry.delete(0, "end")
        self.status.config(text=f"Installing {raw}…")
        self._run_bg([self.interp_getter(), "-m", "pip", "install", *raw.split()],
                     self._on_pip_done)

    def _add_to_requirements(self):
        """'Add to project': pin the selected global package in requirements.txt."""
        name = self._selected()
        proj = self.project_getter()
        if not name or not proj:
            self.status.config(text="Select a package and open a project first.")
            return
        ver = self.tree.item(self.tree.focus(), "values")[0]
        req = os.path.join(proj, "requirements.txt")
        if os.path.isfile(req):
            try:
                with open(req, "r", encoding="utf-8") as f:
                    for line in f:
                        base = re.split(r"[<>=!~;\[\s]", line.strip(), 1)[0]
                        if base.lower() == name.lower():
                            self.status.config(text=f"{name} already in requirements.txt")
                            return
            except OSError:
                pass
        entry = f"{name}=={ver}" if ver else name
        try:
            with open(req, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except OSError as e:
            self.status.config(text=f"requirements.txt: {e}")
            return
        self.status.config(text=f"Added to requirements.txt: {entry}")

    def _on_pip_done(self, payload):
        self._working = False
        if isinstance(payload, Exception):
            self.status.config(text=f"Error: {payload}")
            return
        tail = (payload.stdout or payload.stderr or "").strip().splitlines()
        self.status.config(text=tail[-1][:200] if tail else f"[exit {payload.returncode}]")
        self.refresh()


# ──────────────────────────────────────────────────────────────────────────
# ApiSettingsDialog — choose the LLM used to complete designations.json
# ──────────────────────────────────────────────────────────────────────────

class ApiSettingsDialog(tk.Toplevel):
    """Tools ▸ API Settings…: provider / base URL / model(s) / key.
    Several models can share the same key and endpoint — "+ model" adds
    input boxes below Model; the whole list is offered in the Agents tab
    model dropdowns so different agents can use different models
    concurrently. cfg["model"] stays the first entry (the default)."""

    def __init__(self, master, cfg, on_save):
        super().__init__(master)
        self.title("API Settings")
        self.resizable(False, False)
        self.transient(master)
        self.cfg, self.on_save = cfg, on_save

        self.provider = tk.StringVar(value=cfg.get("provider", "anthropic"))
        self.base_url = tk.StringVar(value=cfg.get("base_url", ""))
        self.api_key = tk.StringVar(value=cfg.get("api_key", ""))
        self.max_tokens = tk.StringVar(value=str(cfg.get("max_tokens",
                                                         4000)))
        models = [m for m in (cfg.get("models") or []) if m]
        if not models:
            models = [cfg.get("model", "")]
        self.model_vars = [tk.StringVar(value=m) for m in models]

        tk.Label(self, text="Provider").grid(row=0, column=0, sticky="w",
                                             padx=8, pady=5)
        box = ttk.Combobox(self, textvariable=self.provider,
                           state="readonly", values=list(PROVIDER_PRESETS),
                           width=30)
        box.grid(row=0, column=1, padx=8, pady=5, sticky="w")
        box.bind("<<ComboboxSelected>>", self._preset)

        tk.Label(self, text="Base URL").grid(row=1, column=0, sticky="w",
                                             padx=8, pady=5)
        e_url = tk.Entry(self, textvariable=self.base_url, width=46)
        e_url.grid(row=1, column=1, padx=8, pady=5)
        attach_context_menu(e_url)

        tk.Label(self, text="Model(s)").grid(row=2, column=0, sticky="nw",
                                             padx=8, pady=5)
        self.models_frame = tk.Frame(self)
        self.models_frame.grid(row=2, column=1, sticky="w", padx=8, pady=2)
        self._rebuild_models()

        tk.Label(self, text="API key").grid(row=3, column=0, sticky="w",
                                            padx=8, pady=5)
        e_key = tk.Entry(self, textvariable=self.api_key, width=46,
                         show="•")
        e_key.grid(row=3, column=1, padx=8, pady=5)
        attach_context_menu(e_key)
        tk.Label(self, text="Max tokens").grid(row=4, column=0, sticky="w",
                                               padx=8, pady=5)
        e_mt = tk.Entry(self, textvariable=self.max_tokens, width=46)
        e_mt.grid(row=4, column=1, padx=8, pady=5)
        attach_context_menu(e_mt)

        tk.Label(self, text="Home settings for chat and the agents "
                            "(custom = any OpenAI-compatible endpoint,\n"
                            "e.g. a local llama-server). Extra models "
                            "share this key/URL and appear in the\n"
                            "Agents tab model dropdowns, so agents can "
                            "run different models concurrently.",
                 justify="left", fg="#888888").grid(row=5, column=1,
                                                    sticky="w", padx=8)
        tk.Button(self, text="Save", command=self._save,
                  bg=THEME["accent"], fg="white", relief="flat", padx=12
                  ).grid(row=6, column=1, sticky="e", padx=8, pady=10)

    def _rebuild_models(self):
        for ch in self.models_frame.winfo_children():
            ch.destroy()
        for i, var in enumerate(self.model_vars):
            rf = tk.Frame(self.models_frame)
            rf.pack(fill="x", pady=1)
            me = tk.Entry(rf, textvariable=var, width=40)
            me.pack(side="left")
            attach_context_menu(me)
            if len(self.model_vars) > 1:
                tk.Button(rf, text="−", relief="flat", padx=4,
                          command=lambda j=i: self._del_model(j)
                          ).pack(side="left", padx=(4, 0))
        tk.Button(self.models_frame, text="+ model", relief="flat",
                  padx=6, command=self._add_model).pack(anchor="w",
                                                        pady=(3, 0))

    def _add_model(self):
        self.model_vars.append(tk.StringVar())
        self._rebuild_models()

    def _del_model(self, i):
        if len(self.model_vars) > 1:
            self.model_vars.pop(i)
            self._rebuild_models()

    def _preset(self, _e):
        p = PROVIDER_PRESETS.get(self.provider.get(), {})
        self.base_url.set(p.get("base_url", self.base_url.get()))
        if self.model_vars:
            self.model_vars[0].set(p.get("model",
                                         self.model_vars[0].get()))

    def _save(self):
        try:
            mt = int(self.max_tokens.get())
        except ValueError:
            mt = 4000
        models = [v.get().strip() for v in self.model_vars
                  if v.get().strip()]
        self.cfg.update({"provider": self.provider.get(),
                         "base_url": self.base_url.get().strip(),
                         "model": models[0] if models else "",
                         "models": models,
                         "api_key": self.api_key.get().strip(),
                         "max_tokens": mt})
        if not save_api_config(self.cfg):
            messagebox.showerror(
                "Save failed",
                f"Could not write {API_CONFIG_PATH} — the settings were "
                "NOT persisted (they remain active for this session "
                "only).", parent=self)
            return
        self.on_save()
        self.destroy()


# ──────────────────────────────────────────────────────────────────────────
# InstructionsDialog — Tools ▸ Instructions Setup…
# ──────────────────────────────────────────────────────────────────────────

class InstructionsDialog(tk.Toplevel):
    """Author the mission instructions that drive the agent cycle: typed
    manually or copied in from the chat pane. Saved to
    <project>/agents/instructions_mission.md — the Agent Workspace Mission box
    auto-populates from that file."""

    def __init__(self, master, project_dir, chat_pane, on_save=None):
        super().__init__(master)
        self.title("Instructions Setup")
        self.geometry("640x480")
        self.configure(bg=THEME["panel"])
        self.chat_pane = chat_pane
        self.on_save = on_save
        self.path = os.path.join(project_dir, "agents",
                                 "instructions_mission.md")

        tk.Label(self, text="Mission instructions — written to "
                            "agents/instructions_mission.md",
                 bg=THEME["panel"], fg=THEME["panel_fg"], anchor="w"
                 ).pack(fill="x", padx=8, pady=(8, 2))
        self.text = tk.Text(self, bg=THEME["bg"], fg=THEME["fg"],
                            insertbackground=THEME["fg"], font=FONT,
                            wrap="word", undo=True)
        self.text.pack(fill="both", expand=True, padx=8, pady=4)
        attach_context_menu(self.text)
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.text.insert("1.0", f.read())
        except OSError:
            pass

        row = tk.Frame(self, bg=THEME["panel"])
        row.pack(fill="x", padx=8, pady=(0, 8))
        tk.Button(row, text="Insert from Chat", command=self._from_chat,
                  relief="flat", padx=8).pack(side="left")
        self.status = tk.Label(row, text="", bg=THEME["panel"],
                               fg=THEME["panel_fg"])
        self.status.pack(side="left", padx=8)
        tk.Button(row, text="Save", command=self._save, bg=THEME["accent"],
                  fg="white", relief="flat", padx=12).pack(side="right")

    def _from_chat(self):
        """Last assistant answer if one exists, else the full transcript."""
        msgs = getattr(self.chat_pane, "messages", [])
        block = next((m["content"] for m in reversed(msgs)
                      if m["role"] == "assistant"), "")
        if not block and msgs:
            block = "\n\n".join(f"{m['role']}: {m['content']}" for m in msgs)
        if not block:
            self.status.config(text="chat is empty — nothing to insert")
            return
        self.text.insert("insert", block.rstrip() + "\n")
        self.status.config(text="inserted from chat")

    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    previous = f.read()
            except OSError:
                previous = ""
            current = self.text.get("1.0", "end-1c")
            atomic_write(self.path, current.rstrip() + "\n")
            if current.strip() != previous.strip():
                from agents import EvolutionLog
                change = "\n".join(difflib.unified_diff(
                    previous.splitlines(), current.splitlines(),
                    "mission-before", "mission-after", lineterm=""))
                EvolutionLog(os.path.dirname(os.path.dirname(
                    self.path))).append(
                    designation="P1", level="project",
                    agent="user/instructions", job="mission specification "
                    "updated from Instructions Setup",
                    reason="saved mission editor", diff=change[:8000],
                    verdict="scope_change", verdict_by="user")
        except OSError as e:
            self.status.config(text=f"save failed: {e}")
            return
        self.status.config(text="saved")
        if self.on_save:
            self.on_save(self.path)


# ──────────────────────────────────────────────────────────────────────────
# Editor pane
# ──────────────────────────────────────────────────────────────────────────

class Editor(tk.Frame):
    def __init__(self, master, on_change=None):
        super().__init__(master, bg=THEME["bg"])
        self.on_change = on_change
        self.path = None
        self.dirty = False
        self.is_python = True
        self.lang = "python"
        self.read_only = False
        self._designation_lines: dict[int, str] = {}

        self.gutter = tk.Text(self, width=5, padx=6, takefocus=0, border=0,
                              bg=THEME["gutter_bg"], fg=THEME["gutter_fg"],
                              font=FONT, state="disabled")
        self.gutter.pack(side="left", fill="y")

        self.designation_gutter = tk.Text(
            self, width=22, padx=5, takefocus=0, border=0, wrap="none",
            bg="#25272b", fg=THEME["accent"], font=FONT,
            state="disabled", cursor="arrow")
        self.designation_gutter.pack(side="left", fill="y")

        self.text = tk.Text(self, wrap="none", undo=True, border=0, padx=8,
                            bg=THEME["bg"], fg=THEME["fg"], insertbackground=THEME["fg"],
                            selectbackground=THEME["sel"], font=FONT)
        self.text.pack(side="left", fill="both", expand=True)
        vsb = ttk.Scrollbar(self, command=self._yview)
        vsb.pack(side="right", fill="y")

        def _on_scroll(*a, _vsb=vsb):
            try:
                if _vsb.winfo_exists():
                    _vsb.set(*a)
            except tk.TclError:
                return
            self._sync_gutter()
        self.text.configure(yscrollcommand=_on_scroll)

        for name in ("kw", "str", "num", "comment", "builtin"):
            self.text.tag_configure(name, foreground=THEME[name])

        self._loading = False
        self._hl_after = None            # debounce id: highlight+gutter
        self.text.bind("<<Modified>>", self._on_modified)
        self.text.bind("<Return>", self._auto_indent)
        self.text.bind("<Tab>", self._tab)
        attach_context_menu(self.text)
        self.text.bind("<Control-z>", lambda e: self._edit_do("undo"))
        self.text.bind("<Control-y>", lambda e: self._edit_do("redo"))
        self.text.bind("<Control-Z>", lambda e: self._edit_do("redo"))
        self.text.bind("<<Paste>>",
                       lambda e: self.text.after_idle(self.text.edit_separator),
                       add="+")
        for gutter in (self.gutter, self.designation_gutter):
            gutter.bind("<MouseWheel>", self._gutter_mousewheel)
            gutter.bind("<Button-4>", self._gutter_mousewheel)
            gutter.bind("<Button-5>", self._gutter_mousewheel)
        try:
            self.text.bind("<Shift-Tab>", self._dedent)
            self.text.bind("<ISO_Left_Tab>", self._dedent)
        except tk.TclError:
            pass
        self._redraw_gutter()

    def _on_modified(self, _e=None):
        if self._loading:
            self.text.edit_modified(False)
            return
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self._changed()

    def _yview(self, *args):
        try:
            self.text.yview(*args)
        except tk.TclError:
            return                     # editor already destroyed
        self._sync_gutter()

    def _gutter_mousewheel(self, event):
        if getattr(event, "num", None) == 4:
            units = -3
        elif getattr(event, "num", None) == 5:
            units = 3
        else:
            units = -int(event.delta / 120) * 3 if event.delta else 0
        if units:
            self.text.yview_scroll(units, "units")
            self._sync_gutter()
        return "break"

    def _sync_gutter(self):
        """Late scroll callbacks can fire after a tab is closed — the
        yscrollcommand queue outlives the widgets, so guard every call."""
        try:
            if not (self.text.winfo_exists() and self.gutter.winfo_exists()
                    and self.designation_gutter.winfo_exists()):
                return
            top = self.text.yview()[0]
            self.gutter.yview_moveto(top)
            self.designation_gutter.yview_moveto(top)
        except tk.TclError:
            pass

    def _changed(self, _e=None):
        self.dirty = True
        # Debounced analysis: on big modules, full re-highlight + gutter
        # rebuild on EVERY keystroke is what made typing sluggish. The
        # dirty flag and tab label update immediately; the expensive work
        # coalesces onto one 200 ms idle callback.
        if self._hl_after is not None:
            try:
                self.after_cancel(self._hl_after)
            except (tk.TclError, ValueError):
                pass
        self._hl_after = self.after(200, self._analyze_now)
        if self.on_change:
            self.on_change(self)

    def _analyze_now(self):
        self._hl_after = None
        try:
            if not self.text.winfo_exists():
                return
        except tk.TclError:
            return
        self.highlight()
        self._redraw_gutter()

    def _redraw_gutter(self):
        n = int(self.text.index("end-1c").split(".")[0])
        self.gutter.configure(state="normal")
        self.gutter.delete("1.0", "end")
        self.gutter.insert("1.0", "\n".join(str(i) for i in range(1, n + 1)))
        self.gutter.configure(state="disabled")
        self.designation_gutter.configure(state="normal")
        self.designation_gutter.delete("1.0", "end")
        self.designation_gutter.insert(
            "1.0", "\n".join(self._designation_lines.get(i, "")
                               for i in range(1, n + 1)))
        self.designation_gutter.configure(state="disabled")
        self._sync_gutter()

    def set_designations(self, line_map: dict[int, str]):
        """Set the deepest designation owning every source line."""
        self._designation_lines = dict(line_map or {})
        self._redraw_gutter()

    def highlight(self):
        for tag in ("kw", "str", "num", "comment", "builtin"):
            self.text.tag_remove(tag, "1.0", "end")
        d = LANG_DEFS.get(self.lang)
        if not d:
            return                       # unknown type: plain text
        rx = _lang_regex(self.lang)
        kw, extra = d["kw"], d.get("extra") or set()
        nocase = d.get("nocase", False)
        content = self.text.get("1.0", "end-1c")
        for m in rx.finditer(content):
            kind = m.lastgroup
            s, e = f"1.0+{m.start()}c", f"1.0+{m.end()}c"
            if kind in ("comment", "str", "num"):
                self.text.tag_add(kind, s, e)
            elif kind == "word":
                w = m.group().lower() if nocase else m.group()
                if w in kw:
                    self.text.tag_add("kw", s, e)
                elif w in extra:
                    self.text.tag_add("builtin", s, e)

    def _auto_indent(self, _e):
        if self.read_only:
            return "break"
        line = self.text.get("insert linestart", "insert")
        indent = re.match(r"[ \t]*", line).group()
        tail = line.rstrip()
        if self.is_python and tail.endswith(":"):
            indent += " " * TAB_SPACES
        elif self.lang in ("javascript", "css") and tail.endswith("{"):
            indent += " " * TAB_SPACES
        self.text.insert("insert", "\n" + indent)
        return "break"

    def _tab(self, _e):
        if self.read_only:
            return "break"
        try:                                   # selection → block indent
            first = int(self.text.index("sel.first").split(".")[0])
            last = int(self.text.index("sel.last").split(".")[0])
        except tk.TclError:
            self.text.insert("insert", " " * TAB_SPACES)
            return "break"
        for ln in range(first, last + 1):
            self.text.insert(f"{ln}.0", " " * TAB_SPACES)
        self.text.tag_add("sel", f"{first}.0", f"{last}.end")
        return "break"

    def _dedent(self, _e):
        if self.read_only:
            return "break"
        try:
            first = int(self.text.index("sel.first").split(".")[0])
            last = int(self.text.index("sel.last").split(".")[0])
        except tk.TclError:
            first = last = int(self.text.index("insert").split(".")[0])
        for ln in range(first, last + 1):
            chunk = self.text.get(f"{ln}.0", f"{ln}.{TAB_SPACES}")
            strip = len(chunk) - len(chunk.lstrip(" "))
            if strip:
                self.text.delete(f"{ln}.0", f"{ln}.{strip}")
        return "break"

    def _edit_do(self, which):
        try:
            (self.text.edit_undo if which == "undo"
             else self.text.edit_redo)()
        except tk.TclError:
            pass
        return "break"

    def set_content(self, text, lang="python", read_only=False):
        self._loading = True
        self.lang = lang
        self.is_python = (lang == "python")
        self.read_only = read_only
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        self.text.edit_modified(False)
        self._loading = False
        self.highlight()
        self._redraw_gutter()
        self.dirty = False
        if read_only:
            self.text.configure(state="disabled")   # frozen: selectable, not editable

    def get_content(self):
        return self.text.get("1.0", "end-1c")

    def goto(self, line):
        self.text.see(f"{line}.0")
        self.text.mark_set("insert", f"{line}.0")
        self.text.focus_set()


# ──────────────────────────────────────────────────────────────────────────
# Main IDE
# ──────────────────────────────────────────────────────────────────────────

class IDE:
    def __init__(self, root, start_path=None):
        self.root = root
        root.title("Iterize IDE")
        self.ui = UIState()
        persist_geometry(root, self.ui, "main.window", "1240x800")
        root.configure(bg=THEME["panel"])
        self._style()

        self.project_dir = None
        self.marks = {"active": "", "frozen": []}
        self.vm: VersionManager | None = None
        self.desig: DesignationManager | None = None
        self.api_cfg = load_api_config()
        self.out_q: "queue.Queue[tuple]" = queue.Queue()
        self.procman = ProcessManager(self.out_q)
        self.tabs: dict[str, Editor] = {}
        self.interpreter = resolve_python_interpreter()
        self.file_clip = None            # {"path": ..., "op": "copy"|"cut"}
        self.struct_state: dict[str, dict[str, bool]] = {}   # path → {qual: open?}
        self._struct_quals: list[tuple[str, str]] = []
        self.agent_spec = None            # shared agents/spec.json store
        self._find_term = None
        self.autosave_on = tk.BooleanVar(value=False)
        self._struct_after = None        # debounce id: Structure rebuild
        self._run_lines: list[str] = []  # last run's output (ring, 400)
        self.orch_win = None             # THE Agent Workspace (singleton)
        self.git_on_save = tk.BooleanVar(
            value=bool(self.ui.get("main.git_on_save")))
        self.git_on_save.trace_add(
            "write", lambda *_: (self.ui.set(
                "main.git_on_save", bool(self.git_on_save.get())),
                self.ui.save()))

        self._build_menu()
        self._build_layout()
        self._refresh_interpreters()
        self._restore_layout()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if start_path and os.path.isdir(start_path):
            self.open_project(start_path)
        elif start_path and os.path.isfile(start_path):
            self.open_project(os.path.dirname(start_path) or ".")
            self.open_file(start_path)

        self.root.after(50, self._drain_output)
        self.root.after(30000, self._autosave_tick)

    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure("Treeview", background=THEME["panel"], fieldbackground=THEME["panel"],
                    foreground=THEME["panel_fg"], borderwidth=0, font=("Segoe UI", 10))
        s.map("Treeview", background=[("selected", THEME["accent"])])

    def _edit_history(self, action):
        """Undo/redo in the focused text editor."""

        widget = self.root.focus_get()

        if not isinstance(widget, tk.Text):
            editor = self.current_editor()
            widget = editor.text if editor else None

        if widget is None:
            return "break"

        try:
            if action == "undo":
                widget.edit_undo()
            else:
                widget.edit_redo()
        except tk.TclError:
            pass

        return "break"

    def _evolution_history(self, action):
        """Persistent recovery for agents/evolution.json."""

        if not self.project_dir:
            self._log("Open a project first.\n", "err")
            return

        from agents import EvolutionLog

        evolution_path = os.path.abspath(
            os.path.join(self.project_dir, "agents", "evolution.json")
        )

        # Do not silently overwrite an unsaved evolution.json editor.
        open_editor = next(
            (
                editor
                for editor in self.tabs.values()
                if editor.path
                   and os.path.abspath(editor.path) == evolution_path
            ),
            None,
        )

        if open_editor and open_editor.dirty:
            if not messagebox.askyesno(
                    "Unsaved evolution.json",
                    "Discard the unsaved editor contents and restore the "
                    "persistent evolution history?",
                    parent=self.root,
            ):
                return

        history = EvolutionLog(self.project_dir)

        if action == "redo":
            ok, message = history.redo()
        else:
            ok, message = history.undo()

        self._log(message + "\n", "ok" if ok else "meta")

        # Refresh evolution.json if it is currently open.
        if ok and open_editor:
            try:
                with open(evolution_path, "r", encoding="utf-8") as file:
                    restored = file.read()

                open_editor.set_content(restored, lang="json")
                open_editor.dirty = False
                self._refresh_tab_labels()
            except OSError as error:
                self._log(f"Could not reload evolution.json: {error}\n", "err")


    # ── menu ──────────────────────────────────────────────────────────────
    def _build_menu(self):
        bar = tk.Menu(self.root)

        fm = tk.Menu(bar, tearoff=0)
        fm.add_command(label="New Project…", command=self._new_project)
        fm.add_command(label="Open Project…", command=self._pick_project, accelerator="Ctrl+Shift+O")
        fm.add_separator()
        fm.add_command(label="New File…", command=lambda: self._tree_new_file(), accelerator="Ctrl+N")
        fm.add_command(label="Save", command=self.save_file, accelerator="Ctrl+S")
        fm.add_command(label="Find in File…", command=self.find_in_editor, accelerator="Ctrl+F")
        fm.add_checkbutton(label="Auto-save (30s)", variable=self.autosave_on)
        fm.add_checkbutton(label="Git commit on checkpoint save",
                           variable=self.git_on_save)
        fm.add_command(label="Save as Iteration", command=self.save_iteration, accelerator="Ctrl+Shift+S")
        fm.add_command(label="Mark as Original (freeze)", command=self.mark_original)
        fm.add_command(label="Close Tab", command=self._close_tab, accelerator="Ctrl+W")
        fm.add_separator()
        fm.add_command(label="Exit", command=self._on_close)
        bar.add_cascade(label="File", menu=fm)
        em = tk.Menu(bar, tearoff=0)

        em.add_command(
            label="Undo",
            command=lambda: self._edit_history("undo"),
            accelerator="Ctrl+Z",
        )

        em.add_command(
            label="Redo",
            command=lambda: self._edit_history("redo"),
            accelerator="Ctrl+Y",
        )

        em.add_separator()

        em.add_command(
            label="Undo evolution.json change",
            command=lambda: self._evolution_history("undo"),
        )

        em.add_command(
            label="Redo evolution.json change",
            command=lambda: self._evolution_history("redo"),
        )

        bar.add_cascade(label="Edit", menu=em)
        rm = tk.Menu(bar, tearoff=0)
        rm.add_command(label="Run", command=self.run_file, accelerator="Ctrl+R")
        rm.add_command(label="Stop", command=self.procman.stop)
        rm.add_command(label="Send Last Error to Agents",
                       command=self._send_error_to_agents)
        rm.add_command(label="Preview HTML in Browser", command=self.run_file)
        bar.add_cascade(label="Run", menu=rm)

        tm = tk.Menu(bar, tearoff=0)
        tm.add_command(label="Dependencies…", command=self._open_dependencies)
        tm.add_separator()
        tm.add_command(label="API Settings…", command=self._open_api_settings)
        tm.add_command(label="Instructions Setup…", command=self._open_instructions)
        tm.add_command(label="View designations.json", command=self._view_designations)
        bar.add_cascade(label="Tools", menu=tm)

        om = tk.Menu(bar, tearoff=0)
        om.add_command(label="Open Orchestrator…", command=self._open_orchestrator)
        bar.add_cascade(label="Orchestrate Agents", menu=om)

        km = tk.Menu(bar, tearoff=0)
        km.add_command(label="Open Graph…", command=self._open_kgraph)
        bar.add_cascade(label="Knowledge Graph", menu=km)

        self.root.config(menu=bar)

        self.root.bind_all("<Control-s>", lambda e: self.save_file())
        self.root.bind_all("<Control-f>", lambda e: self.find_in_editor())
        self.root.bind_all("<F3>", lambda e: self.find_in_editor(again=True))
        self.root.bind_all("<Control-S>", lambda e: self.save_iteration())
        self.root.bind_all("<Control-n>", lambda e: self._tree_new_file())
        self.root.bind_all("<Control-r>", lambda e: self.run_file())
        self.root.bind_all("<Control-w>", lambda e: self._close_tab())
        self.root.bind_all("<Control-O>", lambda e: self._pick_project())
        self.root.bind_all(
            "<Control-z>",
            lambda event: self._edit_history("undo"),
            add="+",
        )

        self.root.bind_all(
            "<Control-y>",
            lambda event: self._edit_history("redo"),
            add="+",
        )

    # ── layout ────────────────────────────────────────────────────────────
    def _build_layout(self):
        shell = tk.Frame(self.root, bg=THEME["panel"])
        shell.pack(fill="both", expand=True)

        # slim always-visible strip: the collapse/expand tab for the chat pane
        strip = tk.Frame(shell, bg=THEME["panel"], width=24)
        strip.pack(side="right", fill="y")
        strip.pack_propagate(False)
        self._chat_btn = tk.Button(strip, text="◀", command=self._toggle_chat,
                                   relief="flat", bg=THEME["panel"],
                                   fg=THEME["panel_fg"])
        self._chat_btn.pack(side="top", pady=(4, 2))
        tk.Label(strip, text="c\nh\na\nt", bg=THEME["panel"], fg=THEME["gutter_fg"],
                 font=("Segoe UI", 8)).pack(side="top")

        outer = tk.PanedWindow(shell, orient="horizontal", sashwidth=5,
                               bg=THEME["panel"], border=0)
        outer.pack(side="left", fill="both", expand=True)
        self._outer = outer

        left = tk.PanedWindow(outer, orient="vertical", sashwidth=5,
                              bg=THEME["panel"], border=0, width=330)
        outer.add(left, minsize=260)

        proj = tk.Frame(left, bg=THEME["panel"])
        tk.Label(proj, text="Project", bg=THEME["panel"], fg=THEME["panel_fg"],
                 anchor="w", font=("Segoe UI", 9, "bold")).pack(fill="x", padx=6, pady=3)
        self.tree = ttk.Treeview(proj, show="tree")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<<TreeviewOpen>>", self._on_tree_expand)
        self.tree.bind("<Button-3>", self._tree_menu)
        self.tree.bind("<Control-v>", lambda e: self._tree_paste())
        self._build_tree_menu()
        left.add(proj, minsize=160)

        struct = tk.Frame(left, bg=THEME["panel"])
        hdr = tk.Frame(struct, bg=THEME["panel"])
        hdr.pack(fill="x")
        tk.Label(hdr, text="Structure", bg=THEME["panel"], fg=THEME["panel_fg"],
                 anchor="w", font=("Segoe UI", 9, "bold")).pack(side="left", padx=6, pady=3)
        tk.Button(hdr, text="Snapshot", command=self.snapshot, relief="flat",
                  padx=8).pack(side="right", padx=4, pady=1)
        self.structure = ttk.Treeview(struct, show="tree",
                                      columns=("st", "line", "qual", "kind",
                                               "rel"),
                                      displaycolumns=("st",))
        self.structure.column("#0", width=290)
        self.structure.column("st", width=34, anchor="center", stretch=False)
        self.structure.tag_configure("chk_open", foreground=THEME["open_chk"])
        self.structure.pack(fill="both", expand=True)
        self.structure.bind("<<TreeviewSelect>>", self._on_structure_select)
        self.structure.bind("<Button-1>", self._struct_click)
        self.structure.bind("<Motion>", self._struct_motion)
        self.structure.bind("<Leave>", lambda e: self._tip.hide())
        self._tip = Tooltip(self.structure)
        left.add(struct, minsize=140)

        right = tk.PanedWindow(outer, orient="vertical", sashwidth=5, bg=THEME["panel"], border=0)
        outer.add(right)

        self.notebook = ttk.Notebook(right)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self._refresh_structure())
        self.notebook.bind("<Button-2>", self._tab_click_close)   # middle-click closes
        self.notebook.bind("<Button-3>", self._tab_menu)          # right-click tab menu
        self._build_tab_menu()
        right.add(self.notebook, minsize=240)

        bottom = tk.Frame(right, bg=THEME["console_bg"])
        ctl = tk.Frame(bottom, bg=THEME["panel"]); ctl.pack(fill="x")
        tk.Label(ctl, text="Interpreter", bg=THEME["panel"], fg=THEME["panel_fg"]).pack(side="left", padx=(6, 4))
        self.interp_var = tk.StringVar(value=self.interpreter)
        self.interp_box = ttk.Combobox(ctl, textvariable=self.interp_var, width=40, state="readonly")
        self.interp_box.pack(side="left", pady=4)
        self.interp_box.bind("<<ComboboxSelected>>", self._on_interp_change)
        tk.Button(ctl, text="▶ Run", command=self.run_file, bg=THEME["accent"], fg="white",
                  relief="flat", padx=10).pack(side="left", padx=4, pady=4)
        tk.Button(ctl, text="■ Stop", command=self.procman.stop, relief="flat", padx=8).pack(side="left")
        tk.Button(ctl, text="Packages", command=self._open_dependencies, relief="flat",
                  padx=8).pack(side="left", padx=4)
        tk.Button(ctl, text="Fix Error → Agents",
                  command=self._send_error_to_agents, relief="flat",
                  padx=8).pack(side="left", padx=4)
        tk.Button(ctl, text="Clear console", command=lambda: self.console.clear(),
                  relief="flat", padx=8).pack(side="right", padx=4)

        self.console = Console(bottom, on_submit=self._console_submit)
        self.console.pack(fill="both", expand=True)
        right.add(bottom, minsize=150)

        # chat pane exists once; toggled in/out of the paned window
        self.chat = ChatPane(outer, on_send=self._chat_send)
        self.chat_visible = False
        # sash persistence: any sash drag ends in a ButtonRelease on the
        # paned window — record all positions then (and on close)
        self._panes = {"outer": outer, "left": left, "right": right}
        for pw in self._panes.values():
            pw.bind("<ButtonRelease-1>",
                    lambda e: self.root.after_idle(self._save_layout),
                    add="+")

    def _save_layout(self):
        try:
            sashes = {}
            for name, pw in self._panes.items():
                coords = []
                i = 0
                while True:
                    try:
                        coords.append(list(pw.sash_coord(i)))
                    except tk.TclError:
                        break
                    i += 1
                sashes[name] = coords
            self.ui.set("main.sashes", sashes)
            self.ui.set("main.chat_visible", self.chat_visible)
            self.ui.save()
        except tk.TclError:
            pass

    def _restore_layout(self):
        """Apply persisted sash positions and chat visibility after the
        widgets have real sizes (hence after_idle + a short delay)."""
        def apply():
            sashes = self.ui.get("main.sashes") or {}
            for name, coords in sashes.items():
                pw = self._panes.get(name)
                if not pw:
                    continue
                for i, xy in enumerate(coords):
                    try:
                        pw.sash_place(i, int(xy[0]), int(xy[1]))
                    except (tk.TclError, ValueError, TypeError,
                            IndexError):
                        pass
            if self.ui.get("main.chat_visible") and not self.chat_visible:
                self._toggle_chat()
        self.root.after(150, apply)

    def _toggle_chat(self):
        if self.chat_visible:
            self._outer.forget(self.chat)
            self._chat_btn.config(text="◀")
        else:
            self._outer.add(self.chat, minsize=300, width=380)
            self._chat_btn.config(text="▶")
        self.chat_visible = not self.chat_visible
        self._save_layout()

    def _chat_send(self, system, messages):
        cfg = dict(self.api_cfg)

        def work():
            try:
                reply = api_chat(cfg, messages, system=system)
                self.out_q.put(("callback",
                                lambda: (self.chat.receive(reply),
                                         self._save_chat_md())))
            except Exception as e:  # noqa: BLE001
                self.out_q.put(("callback", lambda m=str(e): self.chat.error(m)))
        threading.Thread(target=work, daemon=True).start()

    # ── console routing ───────────────────────────────────────────────────
    def _console_submit(self, line):
        if self.procman.running():
            self.console.write(line + "\n", "stdin")     # echo, then feed stdin
            self.procman.write_stdin(line + "\n")
            return
        if not line.strip():
            return
        self.console.write(f"$ {line}\n", "meta")
        self.procman.spawn(line, cwd=self.project_dir or os.getcwd(), shell=True)

    def _drain_output(self):
        try:
            while True:
                tag, payload = self.out_q.get_nowait()
                if tag == "callback":
                    payload()
                else:
                    if isinstance(payload, str):
                        self._run_lines.extend(payload.splitlines())
                        del self._run_lines[:-400]
                    self.console.write(payload, tag if tag != "out" else None)
        except queue.Empty:
            pass
        self.root.after(50, self._drain_output)

    def _log(self, text, tag=None):
        self.console.write(text, tag)

    # ── interpreter / venv ────────────────────────────────────────────────
    def _refresh_interpreters(self):
        found = []

        system_python = resolve_python_interpreter()
        if system_python:
            found.append(system_python)

        if self.project_dir:
            for venv in ("venv", ".venv", "env"):
                for sub in ("Scripts/python.exe", "bin/python", "bin/python3"):
                    cand = os.path.join(self.project_dir, venv, sub)
                    if os.path.isfile(cand) and cand not in found:
                        found.append(cand)

        found.append("Browse…")
        self.interp_box["values"] = found

        resolved = resolve_python_interpreter(self.interpreter)

        if resolved:
            self.interpreter = resolved
        else:
            self.interpreter = ""

        self.interp_var.set(self.interpreter)

    def _on_interp_change(self, _e):
        choice = self.interp_var.get()
        if choice == "Browse…":
            p = filedialog.askopenfilename(title="Select python executable")
            if p:
                self.interpreter = p
                vals = list(self.interp_box["values"]); vals.insert(-1, p)
                self.interp_box["values"] = vals
            self.interp_var.set(self.interpreter)
        else:
            self.interpreter = choice

    def _open_dependencies(self):
        DependencyDialog(self.root, lambda: self.interpreter, lambda: self.project_dir)

    def _open_api_settings(self):
        ApiSettingsDialog(self.root, self.api_cfg,
                          lambda: self._log("API settings saved.\n", "meta"))

    def _open_instructions(self):
        if not self.project_dir:
            self._log("Open a project first.\n", "err"); return

        def saved(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                text = ""
            if self.agent_spec:
                self.agent_spec.spec["mission"] = text.strip()
                self.agent_spec.save()
            if self.orch_win:
                try:
                    if self.orch_win.winfo_exists():
                        self.orch_win.sync_mission_spec(text.strip())
                except tk.TclError:
                    pass
            self._log(f"Instructions saved → "
                      f"{os.path.relpath(p, self.project_dir)}\n", "meta")

        InstructionsDialog(self.root, self.project_dir, self.chat,
                           on_save=saved)

    def _open_orchestrator(self):
        if not (self.project_dir and self.desig):
            self._log("Open a project first.\n", "err"); return None
        if self.orch_win is not None:
            try:
                if self.orch_win.winfo_exists():
                    self.orch_win.lift()
                    return self.orch_win
            except tk.TclError:
                pass
            self.orch_win = None
        try:
            from agents import OrchestratorWindow
        except ImportError as e:
            self._log(f"agents.py not found beside this script: {e}\n", "err")
            return None
        self.orch_win = OrchestratorWindow(
            self.root, project_dir=self.project_dir,
            desig=self.desig, api_cfg=self.api_cfg,
            chat_fn=api_chat, theme=THEME,
            store=self.agent_spec,
            on_change=self.refresh_tree,
            open_at=self._open_at,
            get_active=self._agents_get_active,
            apply_active=self._agents_apply_active)
        return self.orch_win

    def _open_kgraph(self):
        if not (self.project_dir and self.desig):
            self._log("Open a project first.\n", "err"); return
        try:
            from kgraph import KGWindow
        except ImportError as e:
            self._log(f"kgraph.py not found beside this script: {e}\n", "err")
            return
        KGWindow(self.root, project_dir=self.project_dir,
                 desig=self.desig, theme=THEME,
                 api_cfg=self.api_cfg, chat_fn=api_chat)

    # ── new project ───────────────────────────────────────────────────────
    def _new_project(self):
        dlg = tk.Toplevel(self.root); dlg.title("New Project"); dlg.transient(self.root)
        dlg.grab_set(); dlg.resizable(False, False)
        loc = tk.StringVar(value=os.path.expanduser("~"))
        name = tk.StringVar(value="my_project")
        mkvenv = tk.BooleanVar(value=True)

        tk.Label(dlg, text="Project name").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        name_e = tk.Entry(dlg, textvariable=name, width=32)
        name_e.grid(row=0, column=1, columnspan=2, padx=8, pady=6)
        attach_context_menu(name_e)
        tk.Label(dlg, text="Location").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        loc_e = tk.Entry(dlg, textvariable=loc, width=26)
        loc_e.grid(row=1, column=1, padx=8, pady=6)
        attach_context_menu(loc_e)
        tk.Button(dlg, text="…", command=lambda: loc.set(filedialog.askdirectory(initialdir=loc.get()) or loc.get())
                  ).grid(row=1, column=2, padx=4)
        tk.Checkbutton(dlg, text="Create virtual environment (venv)", variable=mkvenv
                       ).grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=6)

        def create():
            proj = os.path.join(loc.get(), name.get().strip())
            if not name.get().strip():
                messagebox.showwarning("Name", "Enter a project name.", parent=dlg); return
            if os.path.exists(proj):
                messagebox.showwarning("Exists", "That folder already exists.", parent=dlg); return
            try:
                os.makedirs(proj)
                with open(os.path.join(proj, "main.py"), "w", encoding="utf-8") as f:
                    f.write(MAIN_TEMPLATE.format(name=name.get().strip()))
                with open(os.path.join(proj, ".gitignore"), "w", encoding="utf-8") as f:
                    f.write(GITIGNORE)
                with open(os.path.join(proj, "README.md"), "w", encoding="utf-8") as f:
                    f.write(f"# {name.get().strip()}\n")
            except OSError as e:
                messagebox.showerror("Error", str(e), parent=dlg); return
            dlg.destroy()
            self.open_project(proj)
            self.open_file(os.path.join(proj, "main.py"))
            if mkvenv.get():
                self._log("$ creating venv…\n", "meta")
                python_exe = resolve_python_interpreter()

                if not python_exe:
                    messagebox.showerror(
                        "Python not found",
                        "Iterize could not find an installed Python interpreter.",
                        parent=self.root
                    )
                    return

                self.procman.spawn(
                    [python_exe, "-m", "venv", os.path.join(proj, "venv")],
                    cwd=proj,
                    on_done=self._refresh_interpreters
                )

        name_e.bind("<Return>", lambda e: create())
        loc_e.bind("<Return>", lambda e: create())
        name_e.focus_set()
        tk.Button(dlg, text="Create", command=create, bg=THEME["accent"], fg="white",
                  relief="flat", padx=12).grid(row=3, column=1, sticky="e", pady=10)
        tk.Button(dlg, text="Cancel", command=dlg.destroy, padx=8).grid(row=3, column=2, pady=10)

    # ── project tree ──────────────────────────────────────────────────────
    def _pick_project(self):
        d = filedialog.askdirectory(title="Open project folder")
        if d:
            self.open_project(d)

    def _expanded_paths(self) -> set:
        out = set()

        def rec(node):
            for c in self.tree.get_children(node):
                vals = self.tree.item(c, "values")
                if vals and self.tree.item(c, "open"):
                    out.add(vals[0])
                    rec(c)
        rec("")
        return out

    def _dirty_tabs(self, tabs=None) -> list:
        return [ed for ed in (tabs if tabs is not None
                              else self.tabs.values())
                if ed.dirty and not ed.read_only]

    def _confirm_discard(self, dirty: list, what: str) -> bool:
        """One prompt covering every dirty buffer at risk. True = the
        caller may proceed (after saving, if the user chose Save)."""
        if not dirty:
            return True
        names = ", ".join(os.path.basename(e.path or "untitled")
                          for e in dirty[:6]) + \
            (f" (+{len(dirty) - 6} more)" if len(dirty) > 6 else "")
        r = messagebox.askyesnocancel(
            "Unsaved changes",
            f"{len(dirty)} unsaved tab(s) — {names}.\n\n"
            f"Save them before {what}?\n"
            f"Yes = save all · No = discard · Cancel = abort")
        if r is None:
            return False
        if r:
            for ed in dirty:
                if not self._save_editor(ed):
                    return False          # a failed save aborts the action
        return True

    def _save_open_tabs(self):
        """Per-project session: which files are open + which is focused."""
        if not self.project_dir:
            return
        rels, active = [], ""
        cur = self.current_editor()
        for ed in self.tabs.values():
            rel = self._mark_rel(ed.path) if ed.path else None
            if rel:
                rels.append(rel)
                if ed is cur:
                    active = rel
        self.ui.set(f"tabs::{os.path.abspath(self.project_dir)}",
                    {"open": rels, "active": active})
        self.ui.save()

    def open_project(self, path, keep_state=False):
        switching = bool(self.project_dir
                         and os.path.abspath(self.project_dir)
                         != os.path.abspath(path))
        if switching:
            if not self._confirm_discard(self._dirty_tabs(),
                                         "switching projects"):
                return
            self._save_open_tabs()
            for tid in list(self.tabs.keys()):   # old project's buffers
                self._close_tab_id(tid, prompt=False)
        expanded = self._expanded_paths() if keep_state else set()
        fresh = not keep_state
        self.project_dir = path
        self.vm = VersionManager(path)
        self.desig = DesignationManager(path)
        if self.desig.load_error:
            self._log(self.desig.load_error + "\n", "err")
        self.desig.reconcile()          # fs is truth: catch out-of-band deletes
        if fresh:
            # currency pass: new files registered, out-of-band edits
            # flagged stale — revisions untouched until a manual save
            for note in self.desig.reconcile_files(
                    (ORIGINALS_DIR, ITERATIONS_DIR, SNAPSHOTS_DIR,
                     "agents", "chats", "venv", ".venv", "env")):
                self._log(f"designations: {note}\n", "meta")
        try:
            from agents import AgentSpecStore
            self.agent_spec = AgentSpecStore(path)   # spec shared app-wide
            if getattr(self.agent_spec, "damaged", None):
                self._log(f"agents/spec.json was damaged — preserved as "
                          f"{os.path.basename(self.agent_spec.damaged)}, "
                          f"defaults loaded\n", "err")
        except ImportError:
            self.agent_spec = None
        self.root.title(f"pyedit — {os.path.basename(path)}")
        self._load_marks()
        self.tree.delete(*self.tree.get_children())
        node = self.tree.insert("", "end", text=os.path.basename(path) or path,
                                values=[path], open=True)
        self._populate(node, path, expanded)
        self._refresh_interpreters()
        self._refresh_tab_labels()
        if fresh and not self.tabs:
            self._restore_open_tabs(path)

    def _restore_open_tabs(self, path):
        state = self.ui.get(f"tabs::{os.path.abspath(path)}") or {}
        active_ed = None
        for rel in state.get("open", []):
            p = os.path.join(path, rel.replace("/", os.sep))
            if os.path.isfile(p):
                self.open_file(p)
                if rel == state.get("active"):
                    active_ed = self.current_editor()
        if active_ed:
            try:
                self.notebook.select(active_ed)
            except tk.TclError:
                pass

    def _on_close(self):
        """WM_DELETE_WINDOW / File ▸ Exit: unsaved buffers can no longer
        die silently, and the session (geometry, sashes, open tabs) is
        persisted on the way out."""
        if not self._confirm_discard(self._dirty_tabs(), "exiting"):
            return
        self._save_layout()
        self._save_open_tabs()
        try:
            self.procman.stop()
        except Exception:  # noqa: BLE001 — closing must always succeed
            pass
        self.root.destroy()

    def _open_at(self, path, line=1):
        """Verify-tab jump target: open the file and go to a line."""
        try:
            self.open_file(path)
            ed = self.current_editor()
            if ed:
                ed.goto(int(line))
        except Exception:  # noqa: BLE001 — a jump must never crash the IDE
            pass

    # ── file marks: the designated active file + freeze-in-place ──
    MARKS_FILE = ".pyedit_marks.json"

    def _marks_path(self):
        return os.path.join(self.project_dir, self.MARKS_FILE)

    def _load_marks(self):
        self.marks = {"active": "", "frozen": []}
        try:
            with open(self._marks_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            self.marks["active"] = str(d.get("active", ""))
            self.marks["frozen"] = [str(r) for r in d.get("frozen", [])]
        except (OSError, json.JSONDecodeError):
            pass

    def _save_marks(self):
        try:
            atomic_write_json(self._marks_path(), self.marks)
        except OSError as e:
            self._log(f"Could not save file marks: {e}\n", "err")

    def _mark_rel(self, path) -> str | None:
        if not (self.project_dir and path):
            return None
        rel = os.path.relpath(os.path.abspath(path), self.project_dir)
        return None if rel.startswith("..") else rel.replace(os.sep, "/")

    def active_path(self) -> str | None:
        """Absolute path of the designated active file, or None."""
        rel = getattr(self, "marks", {}).get("active", "")
        if not (rel and self.project_dir):
            return None
        p = os.path.join(self.project_dir, rel.replace("/", os.sep))
        return p if os.path.isfile(p) else None

    def is_mark_frozen(self, path) -> bool:
        rel = self._mark_rel(path)
        return bool(rel and rel in getattr(self, "marks",
                                           {}).get("frozen", []))

    def _tree_make_active(self):
        """Designate the selected file as THE active file — the one you
        alter and the one the agents import; shown as (a) in the tree."""
        p = self._sel_path()
        rel = self._mark_rel(p) if p and os.path.isfile(p) else None
        if not rel:
            self._log("Select a project file to make active.\n", "err")
            return
        if self.is_mark_frozen(p):
            self._log("Refused: file is frozen — unfreeze it first if you "
                      "want to alter it.\n", "err")
            return
        self.marks["active"] = rel
        self._save_marks()
        self.refresh_tree()
        self._refresh_tab_labels()
        self._log(f"Active file → {rel}  (a)\n", "meta")

    def _tree_freeze_mark(self):
        """Freeze the selected file in place: read-only on disk and
        refused by Save/Approve, so originals and kept iterations cannot
        be overwritten. Shown with 🔒 in the tree."""
        p = self._sel_path()
        rel = self._mark_rel(p) if p and os.path.isfile(p) else None
        if not rel:
            self._log("Select a project file to freeze.\n", "err")
            return
        if rel == self.marks.get("active"):
            self.marks["active"] = ""      # frozen can't be the active file
        if rel not in self.marks["frozen"]:
            self.marks["frozen"].append(rel)
        try:
            os.chmod(p, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass
        for ed in self.tabs.values():      # live tabs become read-only
            if ed.path and os.path.abspath(ed.path) == os.path.abspath(p):
                ed.read_only = True
                ed.text.configure(state="disabled")
        self._save_marks()
        self.refresh_tree()
        self._refresh_tab_labels()
        self._log(f"Frozen in place (protected): {rel}\n", "meta")

    def _tree_unfreeze_mark(self):
        p = self._sel_path()
        rel = self._mark_rel(p) if p else None
        if not rel or rel not in self.marks.get("frozen", []):
            self._log("Not a frozen file.\n", "err")
            return
        if not messagebox.askyesno("Unfreeze",
                                   f"Unfreeze '{os.path.basename(p)}'? It "
                                   "becomes writable again."):
            return
        self.marks["frozen"].remove(rel)
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD | stat.S_IRGRP
                     | stat.S_IROTH)
        except OSError:
            pass
        for ed in self.tabs.values():
            if ed.path and os.path.abspath(ed.path) == os.path.abspath(p):
                ed.read_only = False
                ed.text.configure(state="normal")
        self._save_marks()
        self.refresh_tree()
        self._refresh_tab_labels()
        self._log(f"Unfrozen: {rel}\n", "meta")

    def _agents_get_active(self):
        """Agent Workspace 'Import Active File' hook. Prefers the
        designated (a) active file — open buffer if it is in a tab, else
        disk — falling back to the focused editor tab.
        → (name, content, project_rel_or_empty): the rel lets the agent
        cycle map changed functions to designations and run their
        harnesses against the candidate."""
        ap = self.active_path()
        if ap:
            rel = self._project_rel(ap) or ""
            for ed in self.tabs.values():
                if ed.path and os.path.abspath(ed.path) == \
                        os.path.abspath(ap):
                    return os.path.basename(ap), ed.get_content(), rel
            try:
                with open(ap, "r", encoding="utf-8") as f:
                    return os.path.basename(ap), f.read(), rel
            except (OSError, UnicodeDecodeError):
                pass
        ed = self.current_editor()
        if not ed:
            return None
        return (os.path.basename(ed.path) if ed.path else "untitled.py",
                ed.get_content(),
                (self._project_rel(ed.path) or "") if ed.path else "")

    def _agents_apply_active(
            self,
            name,
            content,
            target_rel="",
    ):
        """Write an approved Agents product back to its source file."""

        target_path = None

        # First choice: the exact file originally imported into Agents.
        if self.project_dir and target_rel:
            project_root = os.path.abspath(self.project_dir)

            candidate = os.path.abspath(
                os.path.join(
                    project_root,
                    target_rel.replace("/", os.sep),
                )
            )

            try:
                inside_project = (
                        os.path.commonpath([project_root, candidate])
                        == project_root
                )
            except ValueError:
                inside_project = False

            if inside_project and os.path.isfile(candidate):
                target_path = candidate

        # Second choice: the explicitly designated active file.
        if target_path is None:
            target_path = self.active_path()

        # Third choice: the currently selected saved editor.
        if target_path is None:
            current_editor = self.current_editor()

            if current_editor and current_editor.path:
                target_path = current_editor.path

        # A from-scratch product has no existing file to replace.
        if target_path is None:
            editor = Editor(
                self.notebook,
                on_change=self._on_editor_change,
            )

            editor.path = None
            editor.set_content(
                content,
                lang=lang_for(name),
            )
            editor.dirty = True

            self.notebook.add(
                editor,
                text=self._tab_label(editor),
            )

            self.tabs[str(editor)] = editor
            self.notebook.select(editor)
            editor.text.focus_set()

            self._log(
                f"Agents product opened as new tab '{name}'. "
                f"Use Save to choose its filename.\n",
                "meta",
            )

            return True

        target_path = os.path.abspath(target_path)

        # Never overwrite a protected file.
        if (
                self.is_mark_frozen(target_path)
                or (
                self.vm
                and self.vm.is_frozen(target_path)
        )
        ):
            self._log(
                f"Approval refused: "
                f"{os.path.basename(target_path)} is frozen.\n",
                "err",
            )
            return False

        # Find an existing editor for the target file.
        editor = next(
            (
                item
                for item in self.tabs.values()
                if item.path
                   and os.path.abspath(item.path) == target_path
            ),
            None,
        )

        # Open the target if it does not already have an editor.
        if editor is None:
            self.open_file(target_path)

            editor = next(
                (
                    item
                    for item in self.tabs.values()
                    if item.path
                       and os.path.abspath(item.path) == target_path
                ),
                None,
            )

        if editor is None:
            self._log(
                f"Approval failed: could not open "
                f"{target_path}.\n",
                "err",
            )
            return False

        # Put the approved product into the exact target editor.
        editor.set_content(
            content,
            lang=lang_for(target_path),
        )
        editor.dirty = True

        self.notebook.select(editor)
        self._refresh_tab_labels()

        # Approval now writes the result to the actual file.
        if not self._save_editor(editor):
            self._log(
                f"Approval failed: could not write "
                f"{os.path.basename(target_path)}.\n",
                "err",
            )
            return False

        editor.text.focus_set()
        self._refresh_tab_labels()
        self._refresh_structure()

        try:
            display_path = os.path.relpath(
                target_path,
                self.project_dir,
            )
        except ValueError:
            display_path = target_path

        self._log(
            f"Agents product '{name}' approved and written to "
            f"{display_path}.\n",
            "meta",
        )

        return True
    def refresh_tree(self):
        if self.project_dir:
            self.open_project(self.project_dir, keep_state=True)

    def _project_rel(self, path) -> str | None:
        """Relative module key ('a/b.py'), or None if outside the project or
        inside originals/ / iterations/ / Snapshots/."""
        if not (self.project_dir and path):
            return None
        rel = os.path.relpath(path, self.project_dir)
        if rel.startswith(".."):
            return None
        if rel.split(os.sep)[0] in (ORIGINALS_DIR, ITERATIONS_DIR, SNAPSHOTS_DIR):
            return None
        return rel.replace(os.sep, "/")

    def _file_prefix(self, full: str) -> str:
        if (self.vm and self.vm.is_frozen(full)) or self.is_mark_frozen(full):
            return "🔒 "
        ap = self.active_path()
        if ap and os.path.abspath(full) == os.path.abspath(ap):
            return "(a) "
        if self.vm and self.vm.is_active(full):
            return "a  "
        return "    "

    def _populate(self, parent, path, expanded):
        self.tree.delete(*self.tree.get_children(parent))
        try:
            entries = sorted(os.listdir(path),
                             key=lambda n: (not os.path.isdir(os.path.join(path, n)), n.lower()))
        except OSError:
            return
        for name in entries:
            if name in ("__pycache__", IDE.MARKS_FILE):
                continue
            full = os.path.join(path, name)
            if os.path.isdir(full):
                icon = "🔒 " if (self.vm and os.path.abspath(full) == self.vm.originals) else "📁 "
                is_open = full in expanded
                node = self.tree.insert(parent, "end", text=icon + name,
                                        values=[full], open=is_open)
                if is_open:
                    self._populate(node, full, expanded)
                else:
                    self.tree.insert(node, "end", text="…")
            else:
                self.tree.insert(parent, "end", text=self._file_prefix(full) + name,
                                 values=[full])

    def _on_tree_expand(self, _e):
        node = self.tree.focus()
        vals = self.tree.item(node, "values")
        if not vals:
            return
        path = vals[0]
        kids = self.tree.get_children(node)
        needs_load = os.path.isdir(path) and (
            not kids or not self.tree.item(kids[0], "values"))
        if needs_load:
            self._populate(node, path, set())

    def _on_tree_select(self, _e):
        node = self.tree.focus()
        vals = self.tree.item(node, "values")
        if vals and os.path.isfile(vals[0]):
            self.open_file(vals[0])

    # ── tree right-click file ops ─────────────────────────────────────────
    def _build_tree_menu(self):
        m = tk.Menu(self.tree, tearoff=0)
        m.add_command(label="New File…", command=self._tree_new_file)
        m.add_command(label="New Folder…", command=self._tree_new_folder)
        m.add_command(label="Rename…", command=self._tree_rename)
        m.add_command(label="Delete", command=self._tree_delete)
        m.add_separator()
        m.add_command(label="Make Active  (a)", command=self._tree_make_active)
        m.add_separator()
        m.add_command(label="Freeze File (protect in place)",
                      command=self._tree_freeze_mark)
        m.add_command(label="Unfreeze File", command=self._tree_unfreeze_mark)
        m.add_separator()
        m.add_command(label="Mark as Original (freeze)", command=self._tree_mark_original)
        m.add_command(label="Unfreeze Original…", command=self._tree_unfreeze)
        m.add_command(label="Save as Iteration", command=self._tree_save_iteration)
        m.add_separator()
        m.add_command(label="Cut", command=lambda: self._tree_clip("cut"))
        m.add_command(label="Copy", command=lambda: self._tree_clip("copy"))
        m.add_command(label="Paste", command=self._tree_paste)
        m.add_separator()
        m.add_command(label="Refresh", command=self.refresh_tree)
        self._tree_ctx = m

    def _tree_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if iid:
            self.tree.selection_set(iid); self.tree.focus(iid)
        self._tree_ctx.tk_popup(e.x_root, e.y_root)

    def _sel_path(self):
        node = self.tree.focus()
        vals = self.tree.item(node, "values") if node else None
        return vals[0] if vals else self.project_dir

    def _target_dir(self):
        p = self._sel_path()
        if not p:
            return None
        return p if os.path.isdir(p) else os.path.dirname(p)

    def _guard_archive_dir(self, d) -> bool:
        """No creating/pasting inside the originals/ archive."""
        if self.vm and _within(d, self.vm.originals):
            self._log("Refused: originals/ is a frozen archive — nothing may be "
                      "created or pasted inside it.\n", "err")
            return True
        return False

    def _guard_item(self, p) -> bool:
        """No rename/cut of frozen originals or of the archive dir itself."""
        if self.vm and (os.path.abspath(p) == self.vm.originals or self.vm.is_frozen(p)):
            self._log("Refused: frozen original — use Unfreeze Original… first.\n", "err")
            return True
        if self.is_mark_frozen(p):
            self._log("Refused: frozen file — use Unfreeze File first.\n", "err")
            return True
        return False

    def _valid_name(self, name: str, what: str = "name") -> bool:
        """Reject path traversal and separators in New File / New Folder /
        Rename inputs — '../x' or 'a/b' could escape the project tree or
        silently create nested paths."""
        n = (name or "").strip()
        bad = (not n or n in (".", "..") or n.startswith("..")
               or any(s in n for s in ("/", "\\", os.sep))
               or bool(os.altsep and os.altsep in n))
        if bad:
            self._log(f"Refused: '{name}' is not a plain {what} — no "
                      f"separators or '..' (create folders explicitly, "
                      f"rename in place).\n", "err")
            return False
        return True

    @staticmethod
    def _unique(dest):
        if not os.path.exists(dest):
            return dest
        base, ext = os.path.splitext(dest)
        i = 1
        while os.path.exists(f"{base} ({i}){ext}"):
            i += 1
        return f"{base} ({i}){ext}"

    def _tree_new_file(self):
        d = self._target_dir()
        if not d or self._guard_archive_dir(d):
            return
        name = simpledialog.askstring("New File", "File name:", parent=self.root)
        if not name:
            return
        if not self._valid_name(name, "file name"):
            return
        if not os.path.splitext(name)[1]:
            name += ".py"
        path = os.path.join(d, name)
        try:
            open(path, "a").close()
        except OSError as e:
            self._log(f"{e}\n", "err"); return
        self.refresh_tree()
        self.open_file(path)

    def _tree_new_folder(self):
        d = self._target_dir()
        if not d or self._guard_archive_dir(d):
            return
        name = simpledialog.askstring("New Folder", "Folder name:", parent=self.root)
        if not name:
            return
        if not self._valid_name(name, "folder name"):
            return
        try:
            os.makedirs(os.path.join(d, name))
        except OSError as e:
            self._log(f"{e}\n", "err"); return
        self.refresh_tree()

    def _tree_rename(self):
        p = self._sel_path()
        if not p or p == self.project_dir or self._guard_item(p):
            return
        new = simpledialog.askstring("Rename", "New name:", initialvalue=os.path.basename(p), parent=self.root)
        if not new:
            return
        if not self._valid_name(new, "name"):
            return
        new_path = os.path.join(os.path.dirname(p), new)
        if os.path.exists(new_path):
            self._log(f"Refused: {new} already exists.\n", "err")
            return
        old_rel = self._project_rel(p)
        try:
            os.rename(p, new_path)
        except OSError as e:
            self._log(f"{e}\n", "err"); return
        if self.desig and old_rel:
            new_rel = self._project_rel(new_path)
            if new_rel:
                self.desig.rename_path(old_rel, new_rel)
        # open tabs must follow the rename, or Save silently recreates
        # the OLD path (a rename-then-fork data hazard)
        oldabs = os.path.abspath(p)
        for ed in self.tabs.values():
            if not ed.path:
                continue
            eabs = os.path.abspath(ed.path)
            if eabs == oldabs:
                ed.path = new_path
            elif os.path.isdir(new_path) and \
                    eabs.startswith(oldabs + os.sep):
                ed.path = os.path.join(new_path,
                                       os.path.relpath(eabs, oldabs))
        self._refresh_tab_labels()
        self.refresh_tree()

    def _tree_delete(self):
        p = self._sel_path()
        if not p or p == self.project_dir:
            return
        if self.vm and os.path.abspath(p) == self.vm.originals:
            self._log("Refused: the originals/ archive dir itself cannot be deleted.\n", "err")
            return
        frozen = bool(self.vm and self.vm.is_frozen(p))
        msg = (f"'{os.path.basename(p)}' is a FROZEN ORIGINAL.\n"
               f"Unfreeze and delete it permanently?" if frozen
               else f"Delete {os.path.basename(p)}?")
        if not messagebox.askyesno("Delete", msg):
            return
        try:
            if frozen:
                self.vm.unfreeze(p)
            _rmtree_force(p) if os.path.isdir(p) else os.remove(p)
        except OSError as e:
            self._log(f"{e}\n", "err"); return
        if self.desig:
            rel = self._project_rel(p)
            if rel:
                self.desig.mark_deleted_under(rel)
        # tabs over a deleted path would resurrect it on the next Ctrl+S
        delabs = os.path.abspath(p)
        for tid, ed in list(self.tabs.items()):
            if ed.path and (os.path.abspath(ed.path) == delabs
                            or os.path.abspath(ed.path).startswith(
                                delabs + os.sep)):
                self._close_tab_id(tid, prompt=False)
        self.refresh_tree()

    def _tree_clip(self, op):
        p = self._sel_path()
        if not p or p == self.project_dir:
            return
        if op == "cut" and self._guard_item(p):
            return
        self.file_clip = {"path": p, "op": op}
        self._log(f"{op}: {os.path.basename(p)}\n", "meta")

    def _clipboard_files(self) -> list:
        """External file paste: text paths on the clipboard, or (Windows)
        actual Explorer file copies via CF_HDROP through PowerShell."""
        paths = []
        try:
            txt = self.root.clipboard_get()
        except tk.TclError:
            txt = ""
        for line in txt.splitlines():
            cand = line.strip().strip('"')
            if cand and os.path.exists(cand):
                paths.append(cand)
        if not paths and sys.platform == "win32":
            try:
                p = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-Clipboard -Format FileDropList) | ForEach-Object { $_.FullName }"],
                    capture_output=True, text=True, timeout=10)
                paths = [l.strip() for l in p.stdout.splitlines()
                         if l.strip() and os.path.exists(l.strip())]
            except Exception:  # noqa: BLE001
                pass
        return paths

    def _tree_paste(self):
        d = self._target_dir()
        if not d or self._guard_archive_dir(d):
            return
        sources, op = [], "copy"
        if self.file_clip and os.path.exists(self.file_clip["path"]):
            sources, op = [self.file_clip["path"]], self.file_clip["op"]
        else:
            sources = self._clipboard_files()      # files copied in the OS
        if not sources:
            self._log("Nothing to paste.\n", "err")
            return
        for src in sources:
            dest = self._unique(os.path.join(d, os.path.basename(src)))
            try:
                if op == "copy":
                    shutil.copytree(src, dest) if os.path.isdir(src) else shutil.copy2(src, dest)
                else:
                    shutil.move(src, dest)
                    if self.desig:
                        orel, nrel = self._project_rel(src), self._project_rel(dest)
                        if orel and nrel:
                            self.desig.rename_path(orel, nrel)
            except (OSError, shutil.Error) as e:
                self._log(f"Paste failed for {os.path.basename(src)}: {e}\n", "err")
        if op == "cut":
            self.file_clip = None
        self.refresh_tree()

    def _tree_mark_original(self):
        p = self._sel_path()
        if p and os.path.isfile(p):
            self.mark_original(p)

    def _tree_unfreeze(self):
        p = self._sel_path()
        if not (p and self.vm and self.vm.is_frozen(p)):
            self._log("Select a frozen original (inside originals/) to unfreeze.\n", "err")
            return
        if not messagebox.askyesno(
                "Unfreeze",
                f"Unfreeze '{os.path.basename(p)}'?\n\nIt becomes editable and "
                f"deletable — it is no longer a protected archive copy."):
            return
        try:
            self.vm.unfreeze(p)
        except (OSError, ValueError) as e:
            self._log(f"{e}\n", "err"); return
        for ed in self.tabs.values():        # reload if open read-only in a tab
            if ed.path == p:
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        ed.set_content(f.read(), lang=lang_for(p),
                                       read_only=False)
                except OSError:
                    pass
        self._log(f"Unfrozen: {os.path.basename(p)}\n", "meta")
        self.refresh_tree()
        self._refresh_tab_labels()

    def _tree_save_iteration(self):
        p = self._sel_path()
        if p and os.path.isfile(p):
            self.save_iteration(p)

    # ── versioning (originals / iterations) ───────────────────────────────
    def mark_original(self, path=None):
        if not self.vm:
            self._log("Open a project first.\n", "err"); return
        ed = self.current_editor()
        path = path or (ed.path if ed else None)
        if not path:
            return
        # the archived original must equal the screen, not a stale disk
        # copy — persist any dirty open buffer of this file first
        for e in self.tabs.values():
            if e.path == path and e.dirty and not e.read_only:
                if not self._save_editor(e):
                    return
        try:
            dest = self.vm.mark_original(path)
        except (ValueError, OSError) as e:
            self._log(f"Mark as original failed: {e}\n", "err"); return
        self._log(f"Frozen original archived → {os.path.relpath(dest, self.project_dir)}\n"
                  f"Working copy is now ACTIVE (marked 'a').\n", "meta")
        self.refresh_tree()
        self._refresh_tab_labels()

    def save_iteration(self, path=None):
        if not self.vm:
            self._log("Open a project first.\n", "err"); return
        ed = self.current_editor()
        path = path or (ed.path if ed else None)
        if not path:
            return
        # Persist a dirty open buffer so the iteration matches the screen.
        for e in self.tabs.values():
            if e.path == path and e.dirty and not e.read_only:
                self._save_editor(e)
        try:
            dest = self.vm.save_iteration(path)
        except (ValueError, OSError) as e:
            self._log(f"Save as iteration failed: {e}\n", "err"); return
        self._log(f"Iteration saved → {os.path.relpath(dest, self.project_dir)}\n", "meta")
        # designations.json snapshot with matching name/timestamp
        if self.desig:
            rel = self._project_rel(path) or os.path.basename(path)
            self.desig.log_iteration(rel, os.path.basename(dest))
            json_dest = os.path.splitext(dest)[0] + ".json"
            try:
                shutil.copy2(self.desig.path, json_dest)
                self._log(f"Designations log → "
                          f"{os.path.relpath(json_dest, self.project_dir)}\n", "meta")
            except OSError as e:
                self._log(f"designations copy failed: {e}\n", "err")
        self.refresh_tree()

    # ── tabs / files ──────────────────────────────────────────────────────
    def current_editor(self):
        return self.tabs.get(self.notebook.select())

    def _tab_label(self, ed: Editor) -> str:
        name = os.path.basename(ed.path or "untitled")
        if ed.read_only:
            return "🔒 " + name
        ap = self.active_path()
        if ap and ed.path and os.path.abspath(ed.path) == os.path.abspath(ap):
            active = "(a) "
        else:
            active = ("a " if (self.vm and ed.path
                               and self.vm.is_active(ed.path)) else "")
        return ("*" if ed.dirty else "") + active + name

    def _refresh_tab_labels(self):
        for ed in self.tabs.values():
            try:
                self.notebook.tab(ed, text=self._tab_label(ed))
            except tk.TclError:
                pass

    def open_file(self, path):
        for tab_id, ed in self.tabs.items():
            if ed.path == path:
                self.notebook.select(tab_id); self._refresh_structure(); return
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
        except (OSError, UnicodeDecodeError) as e:
            self._log(f"Cannot open {path}: {e}\n", "err"); return
        ed = Editor(self.notebook, on_change=self._on_editor_change)
        ed.path = path
        frozen = bool((self.vm and self.vm.is_frozen(path))
                      or self.is_mark_frozen(path))
        ed.set_content(content, lang=lang_for(path), read_only=frozen)
        self.notebook.add(ed, text=self._tab_label(ed))
        self.tabs[str(ed)] = ed
        self.notebook.select(ed)
        self._refresh_structure()

    def _on_editor_change(self, ed):
        try:
            self.notebook.tab(ed, text=self._tab_label(ed))
        except tk.TclError:
            pass
        # Structure rebuild = full AST parse — debounced off the
        # keystroke path (400 ms coalesced), immediate elsewhere
        if ed is self.current_editor():
            if self._struct_after is not None:
                try:
                    self.root.after_cancel(self._struct_after)
                except (tk.TclError, ValueError):
                    pass
            self._struct_after = self.root.after(
                400, self._refresh_structure_debounced)

    def _refresh_structure_debounced(self):
        self._struct_after = None
        self._refresh_structure()

    def _save_editor(self, ed: Editor, quiet=False) -> bool:
        if ed.read_only or (ed.path and (
                (self.vm and self.vm.is_frozen(ed.path))
                or self.is_mark_frozen(ed.path))):
            self._log("Refused: file is frozen (protected, read-only) — "
                      "Unfreeze it first.\n", "err")
            return False
        if not ed.path:
            path = filedialog.asksaveasfilename(defaultextension=".py",
                                                filetypes=[("Python", "*.py"), ("All", "*.*")])
            if not path:
                return False
            ed.path = path
            ed.lang = lang_for(path)
            ed.is_python = (ed.lang == "python")
        try:
            atomic_write(ed.path, ed.get_content())
            ed.dirty = False
            self.notebook.tab(ed, text=self._tab_label(ed))
            if not quiet:
                self._log(f"Saved {ed.path}\n", "meta")
        except OSError as e:
            self._log(f"Save failed: {e}\n", "err")
            return False
        # Designation sync (revision bump) happens on MANUAL save only —
        # autosave (quiet) records CURRENCY instead: the index knows the
        # disk moved (stale flag), while revision ids keep reflecting
        # real checkpoints, not 30-second ticks.
        if self.desig and ed.is_python:
            rel = self._project_rel(ed.path)
            if rel:
                if quiet:
                    self.desig.update_current(rel, ed.get_content())
                else:
                    if self.desig.sync_module(rel, ed.get_content()):
                        self._refresh_structure()
                    self._git_checkpoint(rel)
        return True

    def _git_checkpoint(self, rel: str):
        """Opt-in: a manual (checkpoint) save also commits, anchoring the
        designation revision suffix to a real, diffable, revertable
        commit — git is the industrial version of the originals/
        iterations instinct, not a replacement for it."""
        if not (self.git_on_save.get() and self.project_dir):
            return
        if not os.path.isdir(os.path.join(self.project_dir, ".git")):
            self._log("Git checkpoint: no .git here — run 'git init' in "
                      "the console once.\n", "err")
            return

        def work():
            try:
                subprocess.run(["git", "add", rel], cwd=self.project_dir,
                               capture_output=True, text=True, timeout=20)
                p = subprocess.run(
                    ["git", "commit", "-m", f"pyedit checkpoint: {rel}"],
                    cwd=self.project_dir, capture_output=True, text=True,
                    timeout=20)
                if p.returncode == 0:
                    h = subprocess.run(["git", "rev-parse", "--short",
                                        "HEAD"], cwd=self.project_dir,
                                       capture_output=True, text=True,
                                       timeout=10).stdout.strip()
                    self.out_q.put(("meta", f"git checkpoint {h}: "
                                            f"{rel}\n"))
                    if self.desig:
                        mod = self.desig.data["modules"].get(rel)
                        if mod is not None:
                            mod["last_commit"] = h
                            self.desig.save()
                elif "nothing to commit" not in (p.stdout + p.stderr):
                    self.out_q.put(("err", f"git checkpoint failed: "
                                           f"{(p.stderr or p.stdout)[-200:]}\n"))
            except (OSError, subprocess.TimeoutExpired) as e:
                self.out_q.put(("err", f"git checkpoint failed: {e}\n"))
        threading.Thread(target=work, daemon=True).start()

    def save_file(self):
        ed = self.current_editor()
        if ed:
            self._save_editor(ed)

    def _close_tab(self):
        sel = self.notebook.select()
        if sel:
            self._close_tab_id(sel)

    def _close_tab_id(self, tab_id, prompt=True):
        ed = self.tabs.get(tab_id)
        if not ed:
            return
        if prompt and ed.dirty and not messagebox.askyesno("Unsaved", "Discard unsaved changes?"):
            return
        self.tabs.pop(tab_id, None)
        self.notebook.forget(tab_id)
        ed.destroy()
        self._refresh_structure()

    def _tab_at(self, x, y):
        try:
            return self.notebook.tabs()[self.notebook.index(f"@{x},{y}")]
        except (tk.TclError, IndexError):
            return None

    def _tab_click_close(self, e):
        tab_id = self._tab_at(e.x, e.y)
        if tab_id:
            self._close_tab_id(tab_id)

    def _build_tab_menu(self):
        m = tk.Menu(self.notebook, tearoff=0)
        m.add_command(label="Close", command=lambda: self._close_tab_id(self._menu_tab))
        m.add_command(label="Close Others", command=self._close_others)
        m.add_command(label="Close All", command=self._close_all)
        self._tab_ctx = m
        self._menu_tab = None

    def _tab_menu(self, e):
        tab_id = self._tab_at(e.x, e.y)
        if tab_id:
            self._menu_tab = tab_id
            self._tab_ctx.tk_popup(e.x_root, e.y_root)

    def _close_others(self):
        targets = [tid for tid in self.tabs if tid != self._menu_tab]
        dirty = self._dirty_tabs([self.tabs[t] for t in targets])
        if not self._confirm_discard(dirty, "closing the other tabs"):
            return
        for tid in targets:
            self._close_tab_id(tid, prompt=False)

    def _close_all(self):
        if not self._confirm_discard(self._dirty_tabs(),
                                     "closing all tabs"):
            return
        for tid in list(self.tabs.keys()):
            self._close_tab_id(tid, prompt=False)

    # ── structure panel (ids + open/collapse checkboxes) ──────────────────
    def _refresh_structure(self):
        self.structure.delete(*self.structure.get_children())
        self._struct_quals = []
        ed = self.current_editor()
        active_rel = (self._project_rel(ed.path)
                      if ed and ed.path and ed.is_python else None)
        active_source = ed.get_content() if ed and ed.is_python else ""
        if ed:
            ed.set_designations(
                designation_line_map(active_source, active_rel, self.desig)
                if ed.is_python else {})
        if not self.desig:
            return

        project = self.desig.data.get("project", {})
        project_id = project.get("id", "P1")
        root = self.structure.insert(
            "", "end",
            text=f"{project_id}  {project.get('name') or os.path.basename(self.project_dir)}",
            values=("", "", "", "P", ""), open=True)

        def numeric_id(value):
            match = re.search(r"(\d+)", str(value or ""))
            return int(match.group(1)) if match else 10 ** 9

        def source_lines(rel):
            # The full hierarchy comes from designations.json. Parse only the
            # active buffer during live typing; other modules resolve their
            # selected line lazily when clicked.
            if rel != active_rel:
                return {}, False
            source = active_source
            try:
                tree = ast.parse(source)
            except SyntaxError:
                return {}, True
            lines = {}

            def walk_ast(node, qprefix=""):
                for child in iter_defs(node):
                    qual = qprefix + child.name
                    lines[qual] = child.lineno
                    walk_ast(child, qual + ".")

            walk_ast(tree)
            return lines, False

        def add_scope(rec, parent, rel, prefix, qprefix, lines, states):
            children = []
            for name, ent in rec.get("classes", {}).items():
                if not ent.get("deleted"):
                    children.append(("C", name, ent))
            for name, ent in rec.get("functions", {}).items():
                if not ent.get("deleted"):
                    children.append(("F", name, ent))

            def order(row):
                kind, name, ent = row
                qual = qprefix + name
                line = lines.get(qual, 0)
                return (0 if line else 1, line or numeric_id(ent.get("id")),
                        kind, name.lower())

            for kind, name, ent in sorted(children, key=order):
                qual = qprefix + name
                designation = (prefix + ent.get("id", "")
                               + rev_suffix(ent.get("revision", 0)))
                checked = states.get(qual, False)
                line = lines.get(qual, 0)
                glyph = "C" if kind == "C" else "ƒ"
                iid = self.structure.insert(
                    parent, "end", text=f"{designation}  {glyph} {name}",
                    values=("☑" if checked else "☐", line, qual, kind, rel),
                    open=True, tags=("chk_open",) if checked else ())
                self._struct_quals.append((rel, qual))
                add_scope(ent, iid, rel, designation, qual + ".",
                          lines, states)

        modules = [(rel, mod) for rel, mod in
                   self.desig.data.get("modules", {}).items()
                   if not mod.get("deleted")]
        modules.sort(key=lambda row: (numeric_id(row[1].get("id")), row[0]))
        for rel, mod in modules:
            designation = project_id + mod.get("id", "")
            module_node = self.structure.insert(
                root, "end", text=f"{designation}  M {rel}",
                values=("", 1, "", "M", rel), open=True)
            lines, syntax_error = source_lines(rel)
            if syntax_error:
                self.structure.insert(
                    module_node, "end", text="(syntax error in current source)",
                    values=("", "", "", "", rel))
            path = os.path.join(self.project_dir, rel.replace("/", os.sep))
            states = self.struct_state.setdefault(path, {})
            add_scope(mod, module_node, rel, designation, "", lines, states)

    def _struct_click(self, e):
        if self.structure.identify("region", e.x, e.y) != "cell":
            return
        if self.structure.identify_column(e.x) != "#1":
            return
        iid = self.structure.identify_row(e.y)
        if not iid:
            return "break"
        vals = self.structure.item(iid, "values")
        if len(vals) < 5 or not vals[2]:
            return "break"
        qual, rel = vals[2], vals[4]
        path = os.path.join(self.project_dir, rel.replace("/", os.sep))
        st = self.struct_state.setdefault(path, {})
        new = not st.get(qual, False)
        st[qual] = new
        if new:
            # opening a member opens its ancestors; opening a scope opens members
            parts = qual.split(".")
            for i in range(1, len(parts)):
                st[".".join(parts[:i])] = True
            for item_rel, q in self._struct_quals:
                if item_rel == rel and q.startswith(qual + "."):
                    st[q] = True
        else:
            for item_rel, q in self._struct_quals:
                if item_rel == rel and q.startswith(qual + "."):
                    st[q] = False
        self._refresh_structure()
        return "break"

    def _struct_motion(self, e):
        if (self.structure.identify("region", e.x, e.y) == "cell"
                and self.structure.identify_column(e.x) == "#1"):
            iid = self.structure.identify_row(e.y)
            if iid:
                vals = self.structure.item(iid, "values")
                if len(vals) >= 3 and vals[2]:
                    self._tip.show("open" if vals[0] == "☑" else "collapsed",
                                   e.x_root, e.y_root)
                    return
        self._tip.hide()

    def _on_structure_select(self, _e):
        node = self.structure.focus()
        vals = self.structure.item(node, "values")
        if not (vals and len(vals) >= 5 and vals[4]):
            return
        rel = vals[4]
        path = os.path.join(self.project_dir, rel.replace("/", os.sep))
        self.open_file(path)
        ed = self.current_editor()
        line = int(vals[1]) if str(vals[1]).isdigit() else 0
        if ed and line <= 0 and vals[2]:
            try:
                node = ast.parse(ed.get_content())
                for name in vals[2].split("."):
                    node = next(child for child in iter_defs(node)
                                if child.name == name)
                line = node.lineno
            except (SyntaxError, StopIteration):
                line = 1
        if ed and line > 0:
            ed.goto(line)

    # ── snapshot (.md of open/collapsed structure) ────────────────────────
    def snapshot(self):
        ed = self.current_editor()
        if not ed or not ed.is_python or not ed.path:
            self._log("Snapshot needs an open, saved Python file.\n", "err"); return
        src = ed.get_content()
        try:
            tree = ast.parse(src)
        except SyntaxError as e:
            self._log(f"Snapshot: syntax error: {e}\n", "err"); return
        lines = src.split("\n")
        states = self.struct_state.get(ed.path, {})
        rel = self._project_rel(ed.path)
        any_open = any(states.get(q, False)
                       for item_rel, q in self._struct_quals
                       if item_rel == rel)
        stem = os.path.splitext(os.path.basename(ed.path))[0]
        kind = "open" if any_open else "overview"
        ts = time.strftime("%Y%m%d-%H%M%S")
        base_dir = self.project_dir or os.path.dirname(ed.path)
        snap_dir = os.path.join(base_dir, SNAPSHOTS_DIR)
        try:
            os.makedirs(snap_dir, exist_ok=True)
        except OSError as e:
            self._log(f"Snapshot dir: {e}\n", "err"); return
        out_path = os.path.join(snap_dir, f"{stem}_{kind}_{ts}.md")

        def desig_of(chain):
            if not (self.desig and rel):
                return ""
            d = self.desig.designation(rel, chain)
            return f"  `[{d[0]}]`" if d else ""

        def sig(node):
            s = lines[node.lineno - 1].strip()
            return s[:-1] if s.endswith(":") else s

        def start_line(node):
            decos = getattr(node, "decorator_list", [])
            return min([d.lineno for d in decos] + [node.lineno])

        md = [f"# {os.path.basename(ed.path)} — {kind}",
              f"_{time.strftime('%Y-%m-%d %H:%M:%S')}_", ""]

        def emit_headings(node, chain, depth):
            for child in getattr(node, "body", []):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    k = "C" if isinstance(child, ast.ClassDef) else "F"
                    c2 = chain + [(k, child.name)]
                    md.append("  " * depth + f"- `{sig(child)}`" + desig_of(c2))
                    emit_headings(child, c2, depth + 1)

        if not any_open:
            emit_headings(tree, [], 0)
        else:
            for child in tree.body:
                if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                k = "C" if isinstance(child, ast.ClassDef) else "F"
                qual = child.name
                chain = [(k, child.name)]
                if not states.get(qual, False):
                    md.append(f"- `{sig(child)}`" + desig_of(chain))
                    emit_headings(child, chain, 1)
                    md.append("")
                    continue
                # expanded: full source with collapsed descendants reduced
                s, e = start_line(child), child.end_lineno
                repl = {}   # replacement start line → (end line, def line, indent)

                def collect(n, qprefix):
                    for ch in getattr(n, "body", []):
                        if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                            q = qprefix + "." + ch.name
                            if not states.get(q, False):
                                st_ln = start_line(ch)
                                indent = re.match(r"[ \t]*", lines[ch.lineno - 1]).group()
                                repl[st_ln] = (ch.end_lineno, lines[ch.lineno - 1], indent)
                            else:
                                collect(ch, q)
                collect(child, qual)
                md.append(f"### `{sig(child)}`" + desig_of(chain))
                md.append("```python")
                ln = s
                while ln <= e:
                    if ln in repl:
                        end_ln, defline, indent = repl[ln]
                        md.append(defline)
                        md.append(indent + "    ...")
                        ln = end_ln + 1
                    else:
                        md.append(lines[ln - 1])
                        ln += 1
                md.append("```")
                md.append("")
        try:
            atomic_write(out_path, "\n".join(md) + "\n")
        except OSError as e:
            self._log(f"Snapshot failed: {e}\n", "err"); return
        self._log(f"Snapshot → {os.path.relpath(out_path, base_dir)}\n", "meta")
        self.refresh_tree()

    # ── API annotation of designations.json ───────────────────────────────
    def _view_designations(self):
        if self.desig and os.path.isfile(self.desig.path):
            self.open_file(self.desig.path)
        elif self.desig:
            self.desig.save()
            self.open_file(self.desig.path)
        else:
            self._log("Open a project first.\n", "err")

    def _annotate_module(self):
        ed = self.current_editor()
        if not (self.desig and ed and ed.path and ed.is_python):
            self._log("Open a project .py file first.\n", "err"); return
        rel = self._project_rel(ed.path)
        if not rel:
            self._log("File is outside the project (or in an archive dir).\n", "err"); return
        self.desig.sync_module(rel, ed.get_content())
        skel = self.desig.skeleton_for(rel)
        prompt = ANNOTATE_MODULE_PROMPT % (json.dumps(skel, indent=1),
                                           ed.get_content()[:60000])
        cfg = dict(self.api_cfg)
        self._log(f"Annotating {rel} via {cfg.get('provider')}/{cfg.get('model')}…\n", "meta")

        def work():
            try:
                obj = _extract_json(api_complete(cfg, prompt))
                def apply():
                    self.desig.apply_annotation(rel, obj)
                    self._log(f"designations.json updated for {rel}\n", "meta")
                self.out_q.put(("callback", apply))
            except Exception as e:  # noqa: BLE001
                self.out_q.put(("err", f"Annotate failed: {e}\n"))
        threading.Thread(target=work, daemon=True).start()

    def _annotate_project(self):
        if not self.desig:
            self._log("Open a project first.\n", "err"); return
        mods = [{"module": rel, "id": m["id"], "description": m.get("description")}
                for rel, m in self.desig.data["modules"].items() if not m.get("deleted")]
        if not mods:
            self._log("No modules registered yet — save a .py file first.\n", "err"); return
        prompt = ANNOTATE_PROJECT_PROMPT % json.dumps(mods, indent=1)
        cfg = dict(self.api_cfg)
        self._log(f"Annotating project overview via {cfg.get('provider')}…\n", "meta")

        def work():
            try:
                obj = _extract_json(api_complete(cfg, prompt))
                def apply():
                    pr = self.desig.data["project"]
                    for k in ("description", "how_it_works", "outline"):
                        if obj.get(k) is not None:
                            pr[k] = obj[k]
                    self.desig.save()
                    self._log("designations.json: project overview updated.\n", "meta")
                self.out_q.put(("callback", apply))
            except Exception as e:  # noqa: BLE001
                self.out_q.put(("err", f"Annotate failed: {e}\n"))
        threading.Thread(target=work, daemon=True).start()

    # ── find / autosave ───────────────────────────────────────────────────
    def find_in_editor(self, again=False):
        ed = self.current_editor()
        if not ed:
            return
        if not again or not self._find_term:
            term = simpledialog.askstring("Find", "Find:",
                                          initialvalue=self._find_term or "",
                                          parent=self.root)
            if not term:
                return
            self._find_term = term
        start = "insert+1c" if again else "insert"
        pos = ed.text.search(self._find_term, start, stopindex="end", nocase=True)
        if not pos:                                        # wrap around
            pos = ed.text.search(self._find_term, "1.0", stopindex="end",
                                 nocase=True)
        if not pos:
            self._log(f"'{self._find_term}' not found\n", "err")
            return
        end = f"{pos}+{len(self._find_term)}c"
        ed.text.tag_remove("sel", "1.0", "end")
        ed.text.tag_add("sel", pos, end)
        ed.text.mark_set("insert", end)
        ed.text.see(pos)
        ed.text.focus_set()

    def _autosave_tick(self):
        if self.autosave_on.get():
            for ed in list(self.tabs.values()):
                if ed.dirty and ed.path and not ed.read_only:
                    self._save_editor(ed, quiet=True)
            self._save_chat_md()          # refresh the conversation record
        self.root.after(30000, self._autosave_tick)

    def _save_chat_md(self):
        """Chat record: <project>/chats/chat_<conversation>.md, written on
        EVERY reply (no toggle needed). Each write is the full transcript of
        that conversation — a growing checkpoint, never a truncation — and
        each conversation owns its own file, so records are never
        overwritten across conversations or sessions."""
        if not (self.project_dir and self.chat.messages):
            return
        cdir = os.path.join(self.project_dir, "chats")
        created = not os.path.isdir(cdir)
        try:
            atomic_write(os.path.join(cdir,
                                      f"chat_{self.chat.conv_ts}.md"),
                         self.chat.export_md())
        except OSError:
            return
        if created:
            self.refresh_tree()           # make chats/ visible immediately

    # ── run-error → agents loop ───────────────────────────────────────────
    TB_STARTS = ("Traceback (most recent call last):",
                 "Exception in Tkinter callback")

    def _last_traceback(self) -> str:
        """The last traceback block in the captured run output: from the
        final 'Traceback…'/'Exception in Tkinter callback' marker to the
        end of the buffer (callback tracebacks chain — take everything
        after the first marker of the last error burst)."""
        start = None
        for i, ln in enumerate(self._run_lines):
            if any(ln.startswith(m) for m in self.TB_STARTS):
                start = i
        if start is None:
            return ""
        # widen to the first marker of a contiguous burst (chained
        # Tkinter callback dumps repeat markers back-to-back)
        while start > 0 and any(
                self._run_lines[start - 1].startswith(m)
                for m in self.TB_STARTS):
            start -= 1
        return "\n".join(self._run_lines[start:])[:6000]

    def _tb_file_hint(self, tb: str):
        """First project file named in the traceback →
        (abs_path, rel) or (None, None). Deepest frame wins: the LAST
        project file mentioned is where the exception actually rose."""
        hit = (None, None)
        for m in re.finditer(r'File "([^"]+)", line \d+', tb):
            p = m.group(1)
            rel = self._project_rel(p)
            if rel:
                hit = (os.path.abspath(p), rel)
        return hit

    def _send_error_to_agents(self):
        """The autonomy loop's entry point: last runtime traceback →
        Agent Workspace as oracle evidence, with the file the exception
        rose in auto-imported as the product base. The agents then run
        mission → changeset fix → gates (incl. the run gate, which
        launches the candidate) → review; Approve puts the fix in your
        editor."""
        tb = self._last_traceback()
        if not tb:
            self._log("No traceback captured — run the file and "
                      "reproduce the error first.\n", "err")
            return
        path, rel = self._tb_file_hint(tb)
        name, content = None, None
        if path:
            for ed in self.tabs.values():        # prefer the live buffer
                if ed.path and os.path.abspath(ed.path) == path:
                    name, content = os.path.basename(path), ed.get_content()
                    break
            if content is None:
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        content = f.read()
                    name = os.path.basename(path)
                except (OSError, UnicodeDecodeError):
                    name = content = None
        win = self._open_orchestrator()
        if not win:
            return
        win.receive_error(tb, name=name, content=content, rel=rel or "")

    # ── running ───────────────────────────────────────────────────────────
    def run_file(self):
        if self.procman.running():
            self._log("Already running. Stop it first.\n", "err"); return
        ed = self.current_editor()
        if not ed:
            return
        if ed.lang == "html" and ed.path:
            if not ed.read_only and not self._save_editor(ed):
                return
            import webbrowser
            webbrowser.open("file:///"
                            + os.path.abspath(ed.path).replace(os.sep, "/"))
            self._log(f"Preview → browser: {os.path.basename(ed.path)}\n",
                      "meta")
            return
        if not ed.is_python:
            self._log("Run is for Python/HTML files only.\n", "err"); return
        if not ed.read_only:
            if not self._save_editor(ed):
                return
        if not ed.path:
            return
        self.console.clear()
        self._run_lines.clear()
        self._log(f"$ {self.interpreter} -u {os.path.basename(ed.path)}\n", "meta")
        self._log("(type into the console line below to feed the program's stdin)\n", "meta")
        self.procman.spawn([self.interpreter, "-u", ed.path],
                           cwd=os.path.dirname(ed.path) or ".")



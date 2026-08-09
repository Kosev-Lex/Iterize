"""
common.py — shared helpers for pyedit, agents, and kgraph.

Deduplicates the utilities that had drifted into three private copies:
JSON extraction from LLM replies, timestamping, content hashing, the
right-click context menu, chain-based AST location/segment extraction —
and, since the convergence, the single promote/rollback primitive,
the single harness runner (one execution oracle; the Verify tab and the
Orchestrator's gate 3 are just two callers of it), the atomic_write
primitives every persist path uses, and the shared UIState store for
window/sash persistence, and the API client (load/save config,
api_chat/api_complete) so headless clients need no GUI module.
tkinter is OPTIONAL here: without it the UI helpers degrade to no-ops,
which is what lets kernel.py drive the whole engine from a terminal.
Ships beside the other modules; pure standard library.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import sys
import time
import urllib.request

try:
    import tkinter as tk
except ImportError:          # headless kernel / CLI: UI helpers degrade
    tk = None


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def atomic_write(path: str, text: str, encoding: str = "utf-8"):
    """THE write primitive: temp file in the same directory, fsync,
    os.replace. A crash mid-write leaves the previous file intact —
    never a truncated one. Raises OSError to the caller."""
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp{os.getpid()}")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass


def atomic_write_json(path: str, obj, indent: int = 2):
    atomic_write(path, json.dumps(obj, indent=indent))


def backup_damaged(path: str) -> str | None:
    """Set a damaged (unparseable) state file aside as
    <name>.corrupt_<ts> instead of silently overwriting it, so the user
    can inspect or recover. Returns the backup path or None."""
    try:
        if not os.path.isfile(path):
            return None
        dst = f"{path}.corrupt_{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copy2(path, dst)
        return dst
    except OSError:
        return None

def resolve_python_interpreter(preferred: str | None = None) -> str:
    """Return a real Python interpreter, never the frozen Iterize executable."""

    frozen = bool(getattr(sys, "frozen", False))
    current = os.path.abspath(sys.executable)

    def usable(candidate):
        if not candidate:
            return None

        # Explicit filesystem path.
        if os.path.isfile(candidate):
            resolved = os.path.abspath(candidate)
        else:
            resolved = shutil.which(candidate)
            if not resolved:
                return None
            resolved = os.path.abspath(resolved)

        # In a PyInstaller build sys.executable is Iterize.exe.
        if frozen and os.path.normcase(resolved) == os.path.normcase(current):
            return None

        return resolved

    # Honour a valid selected/project interpreter first.
    selected = usable(preferred)
    if selected:
        return selected

    # Running normally from Python.
    if not frozen:
        return current

    # Packaged application: find an actual Python installation.
    for name in (
        "py.exe",
        "py",
        "python.exe",
        "python",
        "python3.exe",
        "python3",
    ):
        resolved = usable(name)
        if resolved:
            return resolved

    return ""


class UIState:
    """Shared persisted UI state (~/.pyedit_ui.json): window geometries,
    sash positions, chat-pane visibility, per-project open tabs. All
    writes are atomic and debounce-friendly (call save() as often as you
    like; it only touches disk when something changed)."""

    PATH = os.path.join(os.path.expanduser("~"), ".pyedit_ui.json")

    def __init__(self):
        self.data: dict = {}
        self._dirty = False
        try:
            with open(self.PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data = d
        except (OSError, json.JSONDecodeError):
            pass

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def set(self, key: str, value):
        if self.data.get(key) != value:
            self.data[key] = value
            self._dirty = True

    def save(self):
        if not self._dirty:
            return
        try:
            atomic_write_json(self.PATH, self.data)
            self._dirty = False
        except OSError:
            pass


def persist_geometry(win, ui: "UIState", key: str, default: str = ""):
    """Restore a Toplevel/root geometry from UIState and keep it saved:
    <Configure> updates the stored value (debounced 800 ms), and the
    value is flushed by UIState.save() wherever the app already saves."""
    if tk is None:
        return
    geo = ui.get(key) or default
    if geo:
        try:
            win.geometry(geo)
        except tk.TclError:
            pass
    state = {"after": None}

    def commit():
        state["after"] = None

        try:
            if win.state() != "normal":
                return

            ui.set(key, win.winfo_geometry())
            ui.save()

        except tk.TclError:
            pass

    def on_cfg(_e=None):
        if state["after"]:
            try:
                win.after_cancel(state["after"])
            except (tk.TclError, ValueError):
                pass
        try:
            state["after"] = win.after(800, commit)
        except tk.TclError:
            pass
    win.bind("<Configure>", on_cfg, add="+")


def extract_json(text: str) -> dict:
    """First {...} object in an LLM reply, fence/prose tolerant."""
    i, j = text.find("{"), text.rfind("}")
    if i == -1 or j == -1:
        raise ValueError("no JSON object in reply")
    return json.loads(text[i:j + 1])


def attach_context_menu(widget, *, read_only=False):
    """Right-click cut/copy/paste/select-all for Text and Entry widgets.
    No-op when tkinter is absent (headless kernel)."""
    if tk is None:
        return
    menu = tk.Menu(widget, tearoff=0)

    def do_copy():
        try:
            widget.clipboard_clear()
            widget.clipboard_append(widget.selection_get())
        except tk.TclError:
            pass

    def select_all():
        try:
            widget.tag_add("sel", "1.0", "end-1c")
        except (tk.TclError, AttributeError):
            try:
                widget.select_range(0, "end")          # Entry widgets
            except (tk.TclError, AttributeError):
                pass
        return "break"

    if not read_only:
        menu.add_command(label="Cut",
                         command=lambda: widget.event_generate("<<Cut>>"))
    menu.add_command(label="Copy", command=do_copy)
    if not read_only:
        menu.add_command(label="Paste",
                         command=lambda: widget.event_generate("<<Paste>>"))
    menu.add_separator()
    menu.add_command(label="Select All", command=select_all)

    def popup(e):
        widget.focus_set()
        menu.tk_popup(e.x_root, e.y_root)

    widget.bind("<Button-3>", popup)


API_CONFIG_PATH = os.path.join(os.path.expanduser("~"),
                               ".pyedit_config.json")

PROVIDER_PRESETS = {
    "anthropic": {"base_url": "https://api.anthropic.com/v1/messages",
                  "model": "claude-sonnet-4-6"},
    "openai": {"base_url": "https://api.openai.com/v1/chat/completions",
               "model": "gpt-4o-mini"},
    "custom": {"base_url": "http://127.0.0.1:8080/v1/chat/completions",
               "model": "local"},
}


def load_api_config() -> dict:
    cfg = {"provider": "anthropic", **PROVIDER_PRESETS["anthropic"],
           "api_key": "", "max_tokens": 4000}
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    return cfg


def save_api_config(cfg: dict) -> bool:
    """→ True on success. Callers must not announce a save that failed."""
    try:
        atomic_write_json(API_CONFIG_PATH, cfg)
        return True
    except OSError:
        return False


def api_chat(cfg: dict, messages: list, system: str | None = None) -> str:
    """Multi-turn LLM call via urllib. Anthropic messages API or any
    OpenAI-compatible chat endpoint (openai / custom / llama-server).
    messages = [{"role": "user"|"assistant", "content": str}, ...]"""
    provider = cfg.get("provider", "anthropic")
    url, key, model = cfg.get("base_url", ""), cfg.get("api_key", ""), cfg.get("model", "")
    if key.startswith("env:"):
        key = os.environ.get(key[4:], "")
    if not key:
        # named-key resolution: key_ref → keys map → ENVIRONMENT VARIABLE
        ref = cfg.get("key_ref", "")
        key = (cfg.get("keys", {}).get(ref, "")
               or os.environ.get(ref, "")
               or next(iter(cfg.get("keys", {}).values()), ""))
        if key.startswith("env:"):
            key = os.environ.get(key[4:], "")
    if not url or not model:
        raise RuntimeError("API not configured (Tools ▸ API Settings… or "
                           "~/.pyedit_config.json).")
    if provider == "anthropic":
        body = {"model": model, "max_tokens": int(cfg.get("max_tokens", 4000)),
                "messages": messages}
        if system:
            body["system"] = system
        headers = {"content-type": "application/json",
                   "x-api-key": key, "anthropic-version": "2023-06-01"}
    else:
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        body = {"model": model, "messages": msgs}
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=int(cfg.get("timeout", 120))) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if provider == "anthropic":
        return "".join(b.get("text", "") for b in data.get("content", []))
    return data["choices"][0]["message"]["content"]


def api_complete(cfg: dict, prompt: str) -> str:
    """Single-turn wrapper."""
    return api_chat(cfg, [{"role": "user", "content": prompt}])


def gate_compile(path: str, interpreter: str | None = None) -> tuple:
    """Oracle gate 2: byte-compile in the given interpreter (cross-platform:
    plain subprocess, no display, no shell). → (ok, stderr_tail)."""
    import subprocess
    interp = resolve_python_interpreter(interpreter)

    if not interp:
        return False, "No Python interpreter found."
    try:
        p = subprocess.run([interp, "-m", "py_compile", path],
                           capture_output=True, text=True, timeout=60)
        return p.returncode == 0, (p.stderr or "").strip()[-1200:]
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def locate_chain(tree, chain):
    """Walk an AST by a designation chain [("C","Foo"),("F","bar")] →
    the node, or None."""
    node = tree
    for _kind, name in chain:
        found = None
        for ch in getattr(node, "body", []):
            if isinstance(ch, (ast.ClassDef, ast.FunctionDef,
                               ast.AsyncFunctionDef)) and ch.name == name:
                found = ch
                break
        if found is None:
            return None
        node = found
    return node


def segment_for(source: str, chain: list, cap: int = 20000) -> str | None:
    """Source segment for a designation chain, or None."""
    try:
        node = locate_chain(ast.parse(source), chain)
    except SyntaxError:
        return None
    if node is None:
        return None
    return (ast.get_source_segment(source, node) or "")[:cap]


def promote(path: str, new_src: str, backup_dir: str, name: str,
            tag: str) -> str:
    """THE promote primitive (agents and verify both call this — behaviour
    can no longer drift between them): back the real file up into
    backup_dir as <name>_<tag>.bak.py, then overwrite it with new_src.
    Returns the backup path — the rollback token."""
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir,
                          re.sub(r"[^\w().\-]", "_", name) + f"_{tag}.bak.py")
    shutil.copy2(path, backup)
    atomic_write(path, new_src)
    return backup


def rollback(backup: str, path: str):
    """Restore a file from the backup token returned by promote()."""
    shutil.copy2(backup, path)


def run_harness_sandbox(root: str, module_rels: list, harness_file: str,
                        interpreter: str | None = None,
                        target_rel: str | None = None,
                        override_src: str | None = None,
                        extra_files: tuple = (),
                        timeout: int = 90,
                        required_ids: tuple | None = None) -> tuple:
    """THE execution oracle: copy the registered module files into a temp
    working directory (target_rel optionally overridden with candidate
    source), add extra_files (e.g. common.py), execute harness_file there
    with a timeout. NOTE: this is FILE isolation only, not a security
    sandbox — harness code runs with normal user privileges (filesystem,
    network, subprocess).

    Harness contract (strictly enforced):
      * one JSON line per tested requirement:
        {"req": "R1", "pass": true|false, "detail": str}
      * "pass" must be a JSON boolean — strings are a contract violation
      * exactly one result per requirement id — duplicates are rejected
      * when required_ids is given, unknown ids are rejected (missing
        ids are permitted: non-executable requirements may be omitted)
      * the process must exit 0 — a nonzero exit invalidates ALL results
    → ({rid: {"pass": bool, "detail": str}}, error_or_empty)."""
    import subprocess
    import tempfile
    interp = resolve_python_interpreter(interpreter)

    if not interp:
        return {}, "No Python interpreter found."
    tmp = tempfile.mkdtemp(prefix="pyedit_harness_")
    try:
        for rel in module_rels:
            srcp = os.path.join(root, rel.replace("/", os.sep))
            dstp = os.path.join(tmp, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dstp) or tmp, exist_ok=True)
            if rel == target_rel and override_src is not None:
                with open(dstp, "w", encoding="utf-8") as f:
                    f.write(override_src)
            elif os.path.isfile(srcp):
                shutil.copy2(srcp, dstp)
        for xf in extra_files:
            if os.path.isfile(xf):
                shutil.copy2(xf, os.path.join(tmp, os.path.basename(xf)))
        hp = os.path.join(tmp, "_harness_test.py")
        shutil.copy2(harness_file, hp)
        try:
            r = subprocess.run([interp, hp], cwd=tmp, capture_output=True,
                               text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return {}, f"harness run failed: {e}"
        results, violations = {}, []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "req" not in o:
                continue
            rid = str(o["req"])
            p = o.get("pass")
            if not isinstance(p, bool):
                # bool("false") is True — strings are never coerced
                violations.append(f"{rid}: non-boolean pass value {p!r}")
                continue
            if rid in results:
                violations.append(f"{rid}: duplicate result")
                continue
            if required_ids is not None and rid not in required_ids:
                violations.append(f"{rid}: unknown requirement id")
                continue
            results[rid] = {"pass": p,
                            "detail": str(o.get("detail", ""))[:400]}
        if r.returncode != 0:
            # a crash after printing passes must not count as success
            return {}, (f"harness exited {r.returncode} — results "
                        f"discarded: "
                        + (r.stderr or r.stdout or "")[-400:])
        if violations:
            return {}, ("harness contract violation — results discarded: "
                        + "; ".join(violations)[:400])
        err = ""
        if not results:
            err = ("harness produced no results: "
                   + (r.stderr or r.stdout or "")[-400:])
        return results, err
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
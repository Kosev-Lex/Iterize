"""Replace the legacy designation UI with one whole-project final check."""


def install(ns):
    import json
    import os
    import queue
    import re
    import threading
    import time

    tk, ttk = ns["tk"], ns["ttk"]
    messagebox = ns["messagebox"]
    VerifyTab = ns["VerifyTab"]
    atomic_write = ns["atomic_write"]
    extract_json = ns["extract_json"]
    now_iso = ns["now_iso"]
    attach_menu = ns["attach_context_menu"]
    agents_dir, verify_sub = ns["AGENTS_DIR"], ns["VERIF_SUB"]

    def read_text(path, cap=None):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            return text if cap is None else text[:cap]
        except OSError:
            return ""

    def project_inputs(tab):
        mission_path = os.path.join(tab.project_dir, agents_dir,
                                    "instructions_mission.md")
        mission = read_text(mission_path, 100000).strip()
        entries = ns["EvolutionLog"](tab.project_dir).entries
        changes = []
        for entry in entries:
            if (entry.get("verdict") != "scope_change"
                    and "mission specification" not in
                    str(entry.get("job", "")).lower()):
                continue
            additions = [line[1:].strip()
                         for line in str(entry.get("diff") or "").splitlines()
                         if line.startswith("+")
                         and not line.startswith("+++") and line[1:].strip()]
            changes.append({
                "when": entry.get("ts", ""),
                "source": entry.get("agent", ""),
                "reason": entry.get("reason", ""),
                "added_or_changed": additions[:24],
            })
        changes = changes[-80:]

        modules, remaining, truncated = [], 220000, []
        registered = 0
        for rel, meta in sorted(tab.desig.data.get("modules", {}).items()):
            if meta.get("deleted"):
                continue
            registered += 1
            source = read_text(os.path.join(
                tab.project_dir, rel.replace("/", os.sep)))
            if not source:
                continue
            take = min(len(source), max(remaining, 0))
            modules.append(f"--- FILE: {rel} ---\n{source[:take]}")
            remaining -= take
            if take < len(source):
                truncated.append(rel)
            if remaining <= 0:
                break
        return {
            "mission": mission,
            "changes": changes,
            "source": "\n\n".join(modules),
            "truncated": truncated,
            "registered": registered,
            "included": len(modules),
            "evolution_entries": len(entries),
        }

    system = (
        "You are the final whole-project verifier. Build a concise intended "
        "specification from the canonical mission plus later scope changes, "
        "then compare the supplied current source with that specification. "
        "Do not invent requirements. Return ONLY JSON. Use incorrect when "
        "the source shows a requirement is absent or contradicted. Use "
        "uncertain when runtime or unseen-source evidence is required."
    )

    prompt_template = """Aggregate the before-state contract and subsequent
scope changes into one concise final specification, then perform the after-state
check against the current code.

Return ONLY:
{"aggregated_spec":"# Aggregated project specification\\n...",
 "checks":[{"id":"R1","requirement":"intended behaviour",
            "verdict":"correct|incorrect|uncertain",
            "evidence":"specific file, symbol, line, or precise reason"}],
 "summary":"short overall assessment"}

CANONICAL MISSION:
%(mission)s

LATER SCOPE CHANGES FROM EVOLUTION LOG:
%(changes)s

CURRENT PROJECT SOURCE:
%(source)s

SOURCE COVERAGE:
%(coverage)s
"""

    def label(tab, parent, text, bold=False):
        return tk.Label(parent, text=text, bg=tab.bg, fg=tab.fg,
                        font=("Segoe UI", 9,
                              "bold" if bold else "normal"))

    def init(self, master, project_dir, desig, api_cfg, chat_fn,
             spec, theme=None, open_at=None):
        theme = theme or {}
        self.bg = theme.get("panel", "#2b2d30")
        self.fg = theme.get("panel_fg", "#bbbbbb")
        self.tbg = theme.get("bg", "#1e1f22")
        self.tfg = theme.get("fg", "#dcdcdc")
        self.accent = theme.get("accent", "#3574f0")
        tk.Frame.__init__(self, master, bg=self.bg)
        self.project_dir = project_dir
        self.desig = desig
        self.api_cfg = api_cfg
        self.chat_fn = chat_fn
        self.spec = spec
        self.open_at = open_at
        self._q = queue.Queue()
        self._busy = False

        header = tk.Frame(self, bg=self.bg)
        header.pack(fill="x", padx=8, pady=(8, 4))
        tk.Button(header, text="▶ Build Spec + Verify Code",
                  command=self._run_project_check, bg=self.accent,
                  fg="white", relief="flat", padx=12).pack(side="left")
        self.run_btn = header.winfo_children()[-1]
        tk.Button(header, text="Refresh", command=self._load_existing,
                  relief="flat", padx=9).pack(side="left", padx=5)
        self.source_state = label(self, header, "")
        self.source_state.pack(side="left", padx=8)
        label(self, header, "Uses default/main LLM", bold=True).pack(
            side="right")

        stats = tk.LabelFrame(self, text="Final comparison",
                              bg=self.bg, fg=self.fg)
        stats.pack(fill="x", padx=8, pady=(0, 5))
        self.score_lbl = label(self, stats, "Not checked", bold=True)
        self.score_lbl.pack(side="left", padx=8, pady=5)
        self.correct_lbl = label(self, stats, "Correct: —")
        self.correct_lbl.pack(side="left", padx=12)
        self.incorrect_lbl = label(self, stats, "Incorrect: —")
        self.incorrect_lbl.pack(side="left", padx=12)
        self.uncertain_lbl = label(self, stats, "Uncertain: —")
        self.uncertain_lbl.pack(side="left", padx=12)

        panes = tk.PanedWindow(self, orient="vertical", sashwidth=6,
                               bg=self.bg, border=0)
        panes.pack(fill="both", expand=True, padx=8, pady=(0, 5))

        spec_frame = tk.LabelFrame(
            panes, text="Aggregated intended specification",
            bg=self.bg, fg=self.fg)
        panes.add(spec_frame, minsize=180)
        self.aggregate = tk.Text(spec_frame, bg=self.tbg, fg=self.tfg,
                                 wrap="word", state="disabled",
                                 font=("Consolas", 9), padx=6)
        self.aggregate.pack(fill="both", expand=True, padx=4, pady=4)
        attach_menu(self.aggregate, read_only=True)

        findings_frame = tk.LabelFrame(
            panes, text="Requirement findings", bg=self.bg, fg=self.fg)
        panes.add(findings_frame, minsize=210)
        self.findings = ttk.Treeview(
            findings_frame,
            columns=("verdict", "requirement", "evidence"),
            selectmode="browse")
        self.findings.heading("#0", text="ID")
        self.findings.heading("verdict", text="Verdict")
        self.findings.heading("requirement", text="Requirement")
        self.findings.heading("evidence", text="Evidence")
        self.findings.column("#0", width=80, stretch=False)
        self.findings.column("verdict", width=100, stretch=False)
        self.findings.column("requirement", width=370)
        self.findings.column("evidence", width=560)
        yscroll = ttk.Scrollbar(findings_frame, orient="vertical",
                                command=self.findings.yview)
        xscroll = ttk.Scrollbar(findings_frame, orient="horizontal",
                                command=self.findings.xview)
        self.findings.configure(yscrollcommand=yscroll.set,
                                xscrollcommand=xscroll.set)
        yscroll.pack(side="right", fill="y")
        xscroll.pack(side="bottom", fill="x")
        self.findings.pack(fill="both", expand=True)
        self.findings.tag_configure("correct", foreground="#6aab73")
        self.findings.tag_configure("incorrect", foreground="#f75464")
        self.findings.tag_configure("uncertain", foreground="#f0a732")

        summary_frame = tk.LabelFrame(
            panes, text="Summary", bg=self.bg, fg=self.fg)
        panes.add(summary_frame, minsize=90)
        self.summary = tk.Text(summary_frame, height=5, bg=self.tbg,
                               fg=self.tfg, wrap="word", state="disabled",
                               padx=6)
        self.summary.pack(fill="both", expand=True, padx=4, pady=4)
        attach_menu(self.summary, read_only=True)

        self.log = tk.Text(self, height=3, bg=self.tbg, fg=self.tfg,
                           wrap="word", state="disabled", padx=6)
        self.log.pack(fill="x", padx=8, pady=(0, 8))
        self.log.tag_configure("meta", foreground=self.accent)
        self.log.tag_configure("ok", foreground="#6aab73")
        self.log.tag_configure("err", foreground="#f75464")
        attach_menu(self.log, read_only=True)

        self._load_existing()
        self.after(120, self._poll_project_check)

    VerifyTab.__init__ = init

    def put(widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def out(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    VerifyTab._out = out

    def render_report(self, report):
        counts = report.get("counts") or {}
        correct = int(counts.get("correct", 0))
        incorrect = int(counts.get("incorrect", 0))
        uncertain = int(counts.get("uncertain", 0))
        self.score_lbl.config(text=f"{int(report.get('score', 0))}% correct",
                              fg=("#6aab73" if not incorrect else "#f0a732"))
        self.correct_lbl.config(text=f"Correct: {correct}")
        self.incorrect_lbl.config(text=f"Incorrect: {incorrect}")
        self.uncertain_lbl.config(text=f"Uncertain: {uncertain}")
        self.findings.delete(*self.findings.get_children())
        for check in report.get("checks") or []:
            verdict = check.get("verdict", "uncertain")
            self.findings.insert(
                "", "end", text=check.get("id", "?"),
                values=(verdict.upper(), check.get("requirement", ""),
                        check.get("evidence", "")), tags=(verdict,))
        put(self.summary, report.get("summary", "") or
            "No summary was returned.")

    VerifyTab._render_project_report = render_report

    def load_existing(self):
        inputs = project_inputs(self)
        self.source_state.config(
            text=(f"Mission: {'ready' if inputs['mission'] else 'missing'} · "
                  f"modules: {inputs['registered']} · "
                  f"evolution entries: {inputs['evolution_entries']}"))
        outdir = os.path.join(self.project_dir, agents_dir, verify_sub)
        aggregated = read_text(os.path.join(outdir, "aggregated_spec.md"))
        if not aggregated:
            aggregated = (inputs["mission"] or
                          "Confirm instructions_mission.md in Planning first.")
        put(self.aggregate, aggregated)
        latest = read_text(os.path.join(outdir,
                                        "latest_project_report.json"))
        if latest:
            try:
                self._render_project_report(json.loads(latest))
            except json.JSONDecodeError:
                put(self.summary, "The saved project report is unreadable. "
                    "Run the check again.")

    VerifyTab._load_existing = load_existing

    def run_project_check(self):
        if self._busy:
            return
        inputs = project_inputs(self)
        if not inputs["mission"]:
            self._out("Confirm agents/instructions_mission.md in Planning "
                      "before verification.\n", "err")
            return
        if not inputs["source"]:
            self._out("No registered project source is available.\n", "err")
            return
        cfg = dict(self.api_cfg)
        if not messagebox.askokcancel(
                "Confirm final verification API call",
                "The default/main model will receive the canonical mission, "
                "recorded scope changes, and registered project source.\n\n"
                f"Provider: {cfg.get('provider', '(unset)')}\n"
                f"Model: {cfg.get('model', '(unset)')}\n"
                f"Endpoint: {cfg.get('base_url', '(unset)')}\n"
                f"Key environment: {cfg.get('api_key_env', '(none)')}",
                parent=self):
            return
        coverage = ("All registered module source supplied."
                    if not inputs["truncated"] else
                    "Source context limit reached. Treat unseen portions as "
                    "uncertain: " + ", ".join(inputs["truncated"]))
        prompt = prompt_template % {
            "mission": inputs["mission"],
            "changes": json.dumps(inputs["changes"], indent=2)
                       if inputs["changes"] else
                       "(no later scope changes recorded)",
            "source": inputs["source"], "coverage": coverage,
        }
        self._busy = True
        self.run_btn.config(state="disabled", text="checking…")
        self.score_lbl.config(text="Checking…", fg=self.accent)
        self._out("Aggregating intended scope and comparing current code…\n",
                  "meta")

        def worker():
            try:
                raw = self.chat_fn(
                    cfg, [{"role": "user", "content": prompt}],
                    system=system)
                result = extract_json(raw)
                aliases = {"pass": "correct", "present": "correct",
                           "fail": "incorrect", "missing": "incorrect",
                           "unknown": "uncertain"}
                checks = []
                for index, item in enumerate(result.get("checks") or [], 1):
                    if not isinstance(item, dict):
                        continue
                    verdict = aliases.get(
                        str(item.get("verdict", "uncertain")).lower(),
                        str(item.get("verdict", "uncertain")).lower())
                    if verdict not in ("correct", "incorrect", "uncertain"):
                        verdict = "uncertain"
                    checks.append({
                        "id": str(item.get("id") or f"R{index}"),
                        "requirement": str(item.get("requirement") or ""),
                        "verdict": verdict,
                        "evidence": str(item.get("evidence") or ""),
                    })
                if not checks:
                    raise RuntimeError("the model returned no requirement checks")
                counts = {key: sum(x["verdict"] == key for x in checks)
                          for key in ("correct", "incorrect", "uncertain")}
                score = round(100 * counts["correct"] / len(checks))
                aggregated = re.sub(
                    r"^```(?:markdown|md)?\s*|\s*```$", "",
                    str(result.get("aggregated_spec") or
                        inputs["mission"]).strip(), flags=re.I | re.S)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                report = {
                    "_schema": "pyedit.project-verification/1",
                    "started": stamp, "finished": now_iso(),
                    "score": score, "counts": counts, "checks": checks,
                    "summary": str(result.get("summary") or ""),
                    "source_truncated": inputs["truncated"],
                }
                outdir = os.path.join(self.project_dir, agents_dir,
                                      verify_sub)
                os.makedirs(outdir, exist_ok=True)
                atomic_write(os.path.join(outdir, "aggregated_spec.md"),
                             aggregated.rstrip() + "\n")
                encoded = json.dumps(report, indent=2) + "\n"
                atomic_write(os.path.join(outdir,
                                          f"project_{stamp}.json"), encoded)
                atomic_write(os.path.join(outdir,
                                          "latest_project_report.json"),
                             encoded)
                self._q.put(("result", (aggregated, report)))
            except Exception as exc:  # noqa: BLE001 - surfaced in UI
                self._q.put(("error", str(exc)))
            finally:
                self._q.put(("done", None))

        threading.Thread(target=worker, daemon=True).start()

    VerifyTab._run_project_check = run_project_check

    def poll_project_check(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "result":
                    aggregated, report = payload
                    put(self.aggregate, aggregated)
                    self._render_project_report(report)
                    bad = report["counts"]["incorrect"]
                    self._out("Final project comparison completed.\n",
                              "ok" if not bad else "meta")
                elif kind == "error":
                    self.score_lbl.config(text="Check failed", fg="#f75464")
                    put(self.summary, f"Verification failed: {payload}")
                    self._out(f"Verification failed: {payload}\n", "err")
                elif kind == "done":
                    self._busy = False
                    self.run_btn.config(
                        state="normal", text="▶ Build Spec + Verify Code")
                    self._load_existing()
        except queue.Empty:
            pass
        try:
            if self.winfo_exists():
                self.after(120, self._poll_project_check)
        except tk.TclError:
            pass

    VerifyTab._poll_project_check = poll_project_check


"""Compatibility-preserving UI convergence installed by pyedit at startup."""

def install(ns):
    import os, re
    tk, messagebox = ns["tk"], ns["messagebox"]
    Theme, Font = ns["THEME"], ns["FONT"]
    Api, Chat, IDE = ns["ApiSettingsDialog"], ns["ChatPane"], ns["IDE"]
    presets = ns["PROVIDER_PRESETS"]
    attach, atomic = ns["attach_context_menu"], ns["atomic_write"]

    api_init = Api.__init__
    def secure_init(self, master, cfg, on_save):
        clean = dict(cfg); clean["api_key"] = clean.get("api_key_env", "")
        api_init(self, master, clean, on_save)
        self.api_key_env = self.api_key
        for child in self.winfo_children():
            if isinstance(child, tk.Label) and child.cget("text") == "API key":
                child.config(text="API key env name")
            if isinstance(child, tk.Entry) and str(child.cget("textvariable")) == str(self.api_key_env):
                child.config(show="")
    Api.__init__ = secure_init

    api_preset = Api._preset
    def secure_preset(self, event):
        api_preset(self, event)
        self.api_key_env.set(presets.get(self.provider.get(), {}).get("api_key_env", ""))
    Api._preset = secure_preset

    def secure_save(self):
        models = [v.get().strip() for v in self.model_vars if v.get().strip()]
        if not models:
            messagebox.showerror("Model required", "Add at least one model.", parent=self); return
        try: mt = int(self.max_tokens.get())
        except ValueError: mt = 4000
        self.cfg.update(provider=self.provider.get(), base_url=self.base_url.get().strip(),
                        model=models[0], models=models,
                        api_key_env=self.api_key_env.get().strip(), max_tokens=mt)
        self.cfg.pop("api_key", None)
        if not ns["save_api_config"](self.cfg):
            messagebox.showerror("Save failed", "Could not persist API settings.", parent=self); return
        self.on_save(); self.destroy()
    Api._save = secure_save

    planning_system = ("Return ONLY a canonical mission specification in Markdown with: Mission, "
        "Desired outcome, Current scope, Requirements (stable R1 IDs), Constraints, Known-good "
        "areas (do not revise), Remaining work, and Verification criteria. Preserve confirmed "
        "scope when cumulative mode is on. This drives Build and is Verify's contract.")
    chat_init = Chat.__init__
    def planning_init(self, *args, **kwargs):
        chat_init(self, *args, **kwargs)
        self.on_spec_confirm = self.on_spec_load = None; self._planning = False
        plan = tk.LabelFrame(self, text="Plan / canonical mission spec", bg=Theme["panel"], fg=Theme["panel_fg"])
        plan.pack(fill="x", padx=4, pady=(0, 4), before=self.answer.master)
        buttons = tk.Frame(plan, bg=Theme["panel"]); buttons.pack(fill="x")
        tk.Button(buttons, text="Draft / update from discussion", command=self.draft_spec, relief="flat").pack(side="left")
        tk.Button(buttons, text="Load confirmed", command=self.load_spec, relief="flat").pack(side="left", padx=4)
        tk.Button(buttons, text="Confirm → Mission", command=self.confirm_spec,
                  bg=Theme["accent"], fg="white", relief="flat").pack(side="right")
        self.spec_text = tk.Text(plan, height=9, bg=Theme["bg"], fg=Theme["fg"],
                                 insertbackground=Theme["fg"], wrap="word", font=Font)
        self.spec_text.pack(fill="x", padx=3, pady=3); attach(self.spec_text)
    Chat.__init__ = planning_init

    def draft(self):
        if self._busy: return
        confirmed = self.on_spec_load() if self.on_spec_load else ""
        history = "\n\n".join(f"{m['role']}: {m['content']}" for m in self.messages[-12:])
        draft = self.spec_text.get("1.0", "end-1c").strip()
        notes = self.instructions.get("1.0", "end-1c").strip()
        mode = "Preserve and extend confirmed scope." if self.cumulative.get() else "Formalise current scope."
        prompt = f"{mode}\n\nCONFIRMED:\n{confirmed or '(none)'}\n\nDRAFT:\n{draft or '(none)'}\n\nDISCUSSION:\n{history or '(none)'}\n\nNOTES:\n{notes or '(none)'}"
        self._planning = self._busy = True
        self.send_btn.config(text="…", state="disabled"); self.busy_lbl.config(text="● formalising specification…")
        self.on_send(planning_system, [{"role": "user", "content": prompt}])
    Chat.draft_spec = draft
    Chat.load_spec = lambda self: (self.spec_text.delete("1.0", "end"),
        self.spec_text.insert("1.0", self.on_spec_load() if self.on_spec_load else ""))
    def confirm(self):
        text = self.spec_text.get("1.0", "end-1c").strip()
        if text and self.on_spec_confirm: self.on_spec_confirm(text)
    Chat.confirm_spec = confirm
    chat_receive = Chat.receive
    def receive(self, text):
        if not self._planning: return chat_receive(self, text)
        text = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
        self.spec_text.delete("1.0", "end"); self.spec_text.insert("1.0", text)
        self._append("\n✓ Draft spec ready to edit and confirm.\n", "you")
        self._planning = False; self._done()
    Chat.receive = receive

    IDE._planning_spec_path = lambda self: (os.path.join(self.project_dir, "agents", "instructions_mission.md") if self.project_dir else "")
    def load_plan(self):
        try:
            with open(self._planning_spec_path(), encoding="utf-8") as f: return f.read()
        except OSError: return ""
    IDE._load_planning_spec = load_plan
    def confirm_plan(self, text):
        if not self.project_dir:
            messagebox.showerror("No project", "Open a project first.", parent=self.root); return
        path = self._planning_spec_path(); os.makedirs(os.path.dirname(path), exist_ok=True)
        previous = self._load_planning_spec()
        atomic(path, text.rstrip() + "\n")
        if text.strip() != previous.strip():
            import difflib
            from agents import EvolutionLog
            change = "\n".join(difflib.unified_diff(
                previous.splitlines(), text.splitlines(),
                "mission-before", "mission-after", lineterm=""))
            EvolutionLog(self.project_dir).append(
                designation="P1", level="project", agent="user/planning",
                job="mission specification updated from Planning",
                reason="confirmed Planning draft", diff=change[:8000],
                verdict="scope_change", verdict_by="user")
        if self.agent_spec: self.agent_spec.spec["mission"] = text; self.agent_spec.save()
        if self.orch_win and self.orch_win.winfo_exists(): self.orch_win.sync_mission_spec(text)
        self._log("Canonical mission spec confirmed; Build and Verify now share it.\n", "meta")
    IDE._confirm_planning_spec = confirm_plan
    ide_init = IDE.__init__
    def ide_enhanced(self, *args, **kwargs):
        ide_init(self, *args, **kwargs)
        self.chat.on_spec_confirm = self._confirm_planning_spec
        self.chat.on_spec_load = self._load_planning_spec
        self.notebook.bind("<Button-1>", self._tab_left_click, add="+")
    IDE.__init__ = ide_enhanced
    open_project = IDE.open_project
    def open_with_plan(self, *args, **kwargs):
        result = open_project(self, *args, **kwargs); self.chat.load_spec(); return result
    IDE.open_project = open_with_plan
    tab_label = IDE._tab_label
    IDE._tab_label = lambda self, ed: tab_label(self, ed) + "  ×"

    def close_click(self, event):
        tab = self._tab_at(event.x, event.y)
        if not tab:
            return

        # Find the actual right edge of the clicked tab. Notebook.bbox()
        # is unreliable here and was making the whole tab act as the ×.
        right = event.x
        notebook_width = self.notebook.winfo_width()

        while right < notebook_width:
            if self._tab_at(right, event.y) != tab:
                break
            right += 1

        # Only the final 24 pixels—the visible × area—close the tab.
        if event.x >= right - 24:
            self.root.after_idle(
                lambda tab_id=tab: self._close_tab_id(tab_id)
            )
            return "break"
    IDE._tab_left_click = close_click

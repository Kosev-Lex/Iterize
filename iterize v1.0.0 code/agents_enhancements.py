"""Recovery and targeted-review convergence installed by agents.py."""
def install(ns):
    import ast, difflib, json, os, re
    from common import PROVIDER_PRESETS, load_api_config
    tk, messagebox = ns["tk"], ns["messagebox"]
    atomic_json, AGENTS_DIR = ns["atomic_write_json"], ns["AGENTS_DIR"]
    ns["PROVIDERS"] = tuple(PROVIDER_PRESETS)
    ns["PROVIDER_URLS"] = {k:v["base_url"] for k,v in PROVIDER_PRESETS.items()}
    key_envs = {k:v.get("api_key_env", "") for k,v in PROVIDER_PRESETS.items()}
    def resolve(api_cfg, spec, role):
        base_provider = api_cfg.get("provider")
        cfg = dict(api_cfg); ov = dict(spec.get("agents", {}).get(spec.get("roles", {}).get(role, {}).get("agent", ""), {}))
        ov.pop("api_key", None); ov.pop("keys", None); ref = ov.pop("key_ref", "")
        cfg.update({k:v for k,v in ov.items() if v}); cfg.pop("api_key", None); cfg.pop("keys", None)
        provider = cfg.get("provider")
        inherited = str(cfg.get("api_key_env") or "").strip()
        expected = key_envs.get(provider, "")
        known = {x for x in key_envs.values() if x}
        if (not ref and provider != base_provider and inherited in known
                and inherited != expected):
            inherited = ""
        cfg["api_key_env"] = ref or inherited or expected
        return cfg
    ns["resolve_agent_cfg"] = resolve
    def focus(source, evidence):
        nums = re.findall(r"line\s+(\d+)", evidence or "")
        if not nums: return []
        try: tree, line = ast.parse(source), int(nums[-1])
        except (SyntaxError, ValueError): return []
        nodes = [n for n in ast.walk(tree) if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and n.lineno <= line <= getattr(n,"end_lineno",n.lineno)]
        if not nodes: return []
        leaf=min(nodes,key=lambda n:getattr(n,"end_lineno",n.lineno)-n.lineno); parents=[n for n in nodes if isinstance(n,ast.ClassDef) and n is not leaf]
        return [((min(parents,key=lambda n:n.end_lineno-n.lineno).name+".") if parents else "")+leaf.name]
    ns["traceback_focus"] = focus
    Base = ns["EvolutionLog"]
    class EvolutionLog(Base):
        def __init__(self, project_dir):
            super().__init__(project_dir); self.history_path=os.path.join(project_dir,AGENTS_DIR,"evolution_history.json")
            try:
                with open(self.history_path,encoding="utf-8") as f:d=json.load(f)
                self.states,self.cursor=d.get("states",[]),int(d.get("cursor",-1))
            except (OSError,ValueError,TypeError,json.JSONDecodeError):self.states,self.cursor=[],-1
            if not self.states:self._record(self.entries)
            elif self.entries!=self.states[self.cursor]:self._record(self.entries)
        def _save_history(self):atomic_json(self.history_path,{"cursor":self.cursor,"states":self.states})
        def _record(self,entries):
            self.states=self.states[:self.cursor+1]
            if not self.states or self.states[-1]!=entries:self.states.append(json.loads(json.dumps(entries)))
            self.states=self.states[-60:];self.cursor=len(self.states)-1;self._save_history()
        def append(self,**entry):super().append(**entry);self._record(self.entries)
        def undo(self):
            with self._lock:
                live=self._read()
                if live!=self.states[self.cursor]:self._record(live)
                if self.cursor<=0:return False,"Nothing to undo"
                self.cursor-=1;self.entries=json.loads(json.dumps(self.states[self.cursor]));atomic_json(self.path,self.entries);self._save_history();return True,f"Restored {len(self.entries)} evolution entries"
        def redo(self):
            with self._lock:
                if self.cursor>=len(self.states)-1:return False,"Nothing to redo"
                self.cursor+=1;self.entries=json.loads(json.dumps(self.states[self.cursor]));atomic_json(self.path,self.entries);self._save_history();return True,f"Reapplied {len(self.entries)} evolution entries"
    ns["EvolutionLog"] = EvolutionLog
    Orchestrator, Window, Live = ns["Orchestrator"], ns["OrchestratorWindow"], ns["LiveViewWindow"]
    ns["REVIEW_PRODUCT_USER"] = ns["REVIEW_PRODUCT_USER"].replace('{"satisfactory": true_or_false,','{"satisfactory": true_or_false,\n "working_percentage": 0_to_100,\n "parts": [{"chain":"Class.method", "designation":"P1M1C1F1", "score":0_to_100, "status":"sound or needs_work", "evidence":"diff line, traceback or gate"}],\n "remaining": ["specific confined repair"],')
    old_call=Orchestrator._call
    def targeted_call(self,role,prompt,*a,**kw):
        result=old_call(self,role,prompt,*a,**kw)
        if role=="builder" and isinstance(result,dict):
            protected=set(self.product.get("protected_chains",[]));result["changes"]={k:v for k,v in (result.get("changes") or {}).items() if str(k) not in protected}
        elif role=="reviewer" and isinstance(result,dict):
            parts=[p for p in result.get("parts",[]) if isinstance(p,dict)];protected=[]
            for p in parts:
                try:score=int(p.get("score",0))
                except (TypeError,ValueError):score=0
                if score>=90 and p.get("status")=="sound" and p.get("chain"):protected.append(str(p["chain"]))
            try:pct=max(0,min(100,int(result.get("working_percentage",0))))
            except (TypeError,ValueError):pct=0
            self.product.update(working_percentage=pct,review_parts=parts,protected_chains=protected,remaining=[str(x) for x in result.get("remaining",[])])
        return result
    Orchestrator._call=targeted_call
    old_review=Orchestrator.step_review
    def scored_review(self,guidance=""):
        answer=old_review(self,guidance);self._ws("reviewer","\nWORKING: %s%%\nSOUND PARTS: %s\nREMAINING:\n%s\n"%(self.product.get("working_percentage",0),", ".join(self.product.get("protected_chains",[])) or "none evidenced","\n".join("- "+x for x in self.product.get("remaining",[])) or "- none"),"append");return answer
    Orchestrator.step_review=scored_review
    def live_source(self,rel,new,flash=True,unit=""):
        old=self.prev if rel==self.current else "";self.current,self.prev=rel,new;self.header.config(text=rel);self.text.configure(state="normal");self.text.delete("1.0","end");self.text.insert("1.0",new);first=None
        if flash and old:
            for op,_i1,_i2,j1,j2 in difflib.SequenceMatcher(None,old.split("\n"),new.split("\n")).get_opcodes():
                if op in ("replace","insert"):
                    first=first or j1+1
                    for ln in range(j1+1,j2+1):self.text.tag_add("chg",f"{ln}.0",f"{ln}.end")
        self.text.configure(state="disabled");self.stat.config(text=f"updated: {unit or rel}" if old else f"loaded: {rel}")
        if first:self.text.see(f"{first}.0")
    Live.load_source=live_source
    old_init=Window.__init__
    def enhanced_init(self,*a,**kw):
        old_init(self,*a,**kw);self._api_consent_signature=None
        for role,box in self.role_boxes.items():box.bind("<<ComboboxSelected>>",lambda _e,r=role:unassign(self,r),add="+")
        def relabel(widget):
            for child in widget.winfo_children():
                if isinstance(child,tk.Label) and child.cget("text")=="key ref":child.config(text="key env")
                relabel(child)
        relabel(self.build_tab)

    Window.__init__=enhanced_init
    def unassign(self,role):
        if self.role_agent[role].get()=="— unassign —":self.role_agent[role].set("");self.store.spec["roles"][role]["agent"]="";self._out(f"{role.capitalize()} unassigned.\n","meta")
    def evo(self,redo):
        log=EvolutionLog(self.project_dir);ok,msg=log.redo() if redo else log.undo();self._out(msg+"\n","ok" if ok else "meta")
    old_refresh=Window._refresh_roster
    def refresh(self):
        old_refresh(self)
        for box in self.role_boxes.values():box["values"]=["— unassign —"]+list(self.store.spec["agents"])
    Window._refresh_roster=refresh;Window._home_cfg=lambda self:(self.api_cfg.update(load_api_config()) or self.api_cfg)
    def confirm_api(self,roles):
        rows=[]
        for role in roles:
            cfg=resolve(self._home_cfg(),self.store.spec,role);rows.append(f"{role}: {cfg.get('provider')} / {cfg.get('model')}\n  {cfg.get('base_url')}\n  key env: {cfg.get('api_key_env') or '(none; local custom only)'}")
        sig="\n".join(rows)
        if sig==self._api_consent_signature:return True
        ok=messagebox.askokcancel("Confirm agent API calls","These endpoints will receive role prompts and relevant project code:\n\n"+sig,parent=self)
        if ok:self._api_consent_signature=sig
        return ok
    Window._confirm_role_apis=confirm_api
    old_run=Window._run
    def run(self):
        self._collect();roles=[r for r in ns["ROLES"] if self.store.spec["roles"][r].get("agent")]
        if roles and not self._confirm_role_apis(roles):self._out("API call cancelled.\n","meta");return
        return old_run(self)
    Window._run=run
    old_role_chat=Window._role_chat_send
    def role_chat(self,role):
        if role != "mission":
            return old_role_chat(self,role)
        if self.role_entry.get(role) and self.role_entry[role].get().strip() and self.store.spec["roles"][role].get("agent"):
            self._collect()
            roles=[r for r in ns["ROLES"] if self.store.spec["roles"][r].get("agent")]
            if not self._confirm_role_apis(roles):self._chat_out(role,"[API call cancelled]\n");return
        return old_role_chat(self,role)
    Window._role_chat_send=role_chat
    old_error=Window.receive_error
    def receive_error(self,tb,name=None,content=None,rel=""):
        found=focus(content if content is not None else self.product.get("source",""),tb);answer=old_error(self,tb,name,content,rel);self.product["focus_chains"]=found
        if found:self._out("Confined repair focus: "+", ".join(found)+"\n","meta")
        return answer
    Window.receive_error=receive_error
    def sync(self,text):
        self.mission.delete("1.0","end");self.mission.insert("1.0",text)
        self.store.spec["mission"]=text;self.store.save()
        self._prime_mission_workspace()
        self._out("Canonical mission specification synchronized from Planning.\n","ok")
    Window.sync_mission_spec=sync
    old_label=Window._product_lbl
    def product_label(self):
        old_label(self)
        if self.live and self.live.winfo_exists() and self.product.get("source"):self.live.load_source(self.product.get("name") or "product.py",self.product["source"])
    Window._product_lbl=product_label

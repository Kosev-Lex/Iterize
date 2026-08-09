"""Secure provider configuration installed by common.py."""
def install(ns):
    import json, os, urllib.request, urllib.error
    presets = {
        "anthropic": {"base_url":"https://api.anthropic.com/v1/messages", "model":"claude-sonnet-4-6", "api_key_env":"ANTHROPIC_API_KEY"},
        "openai": {"base_url":"https://api.openai.com/v1/chat/completions", "model":"gpt-4o-mini", "api_key_env":"OPENAI_API_KEY"},
        "mistral": {"base_url":"https://api.mistral.ai/v1/chat/completions", "model":"mistral-large-latest", "api_key_env":"MISTRAL_API_KEY"},
        "deepseek": {"base_url":"https://api.deepseek.com/chat/completions", "model":"deepseek-chat", "api_key_env":"DEEPSEEK_API_KEY"},
        "qwen": {"base_url":"https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions", "model":"qwen-plus", "api_key_env":"DASHSCOPE_API_KEY"},
        "zai": {"base_url":"https://api.z.ai/api/paas/v4/chat/completions", "model":"glm-4.5", "api_key_env":"ZAI_API_KEY"},
        "custom": {"base_url":"http://127.0.0.1:8080/v1/chat/completions", "model":"local", "api_key_env":""}}
    ns["PROVIDER_PRESETS"].clear(); ns["PROVIDER_PRESETS"].update(presets)

    def load():
        cfg = {
            "provider": "anthropic",
            **presets["anthropic"],
            "models": [presets["anthropic"]["model"]],
            "max_tokens": 4000,
            "timeout": 120,
        }

        saved = {}

        try:
            with open(ns["API_CONFIG_PATH"], encoding="utf-8") as file:
                loaded = json.load(file)

            if isinstance(loaded, dict):
                saved = loaded
                cfg.update(saved)

        except (OSError, json.JSONDecodeError):
            pass

        provider = cfg.get("provider", "anthropic")
        preset = presets.get(provider, presets["custom"])
        expected_env = preset.get("api_key_env", "")

        # Migrate old configuration formats without retaining plaintext keys.
        old_key = str(cfg.pop("api_key", "") or "")
        old_ref = str(cfg.pop("key_ref", "") or "")
        cfg.pop("keys", None)

        stored_env = str(saved.get("api_key_env", "") or "").strip()

        known_provider_envs = {
            details.get("api_key_env", "")
            for details in presets.values()
            if details.get("api_key_env")
        }

        # Correct a provider-default environment variable left over from a
        # different provider—for example ANTHROPIC_API_KEY with OpenAI.
        if (
                stored_env in known_provider_envs
                and stored_env != expected_env
        ):
            stored_env = ""

        migrated_env = (
            old_key[4:]
            if old_key.startswith("env:")
            else old_ref
        )

        cfg["api_key_env"] = (
                stored_env
                or migrated_env
                or expected_env
        )

        models = [
            str(model).strip()
            for model in (cfg.get("models") or [])
            if str(model).strip()
        ]

        cfg["models"] = models or [
            cfg.get("model") or preset.get("model", "")
        ]
        cfg["model"] = cfg["models"][0]

        return cfg

    def save(cfg):
        clean = dict(cfg)
        for key in ("api_key", "keys", "key_ref"): clean.pop(key, None)
        try: ns["atomic_write_json"](ns["API_CONFIG_PATH"], clean); return True
        except OSError: return False
    def chat(cfg, messages, system=None):
        provider, url, model = cfg.get("provider", "anthropic"), cfg.get("base_url", ""), cfg.get("model", "")
        env = str(cfg.get("api_key_env", "") or "").strip(); key = os.environ.get(env, "") if env else ""
        if not url or not model: raise RuntimeError("API not configured.")
        local = url.startswith(("http://127.0.0.1", "http://localhost"))
        if not key and not (provider == "custom" and local): raise RuntimeError(f"API key environment variable '{env or '(not set)'}' is unavailable.")
        if provider == "anthropic":
            body = {"model":model, "max_tokens":int(cfg.get("max_tokens", 4000)), "messages":messages}
            if system: body["system"] = system
            headers = {"content-type":"application/json", "x-api-key":key, "anthropic-version":"2023-06-01"}
        else:
            body = {"model":model, "messages":([{"role":"system", "content":system}] if system else []) + messages}
            headers = {"Content-Type":"application/json"}
            if key: headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=int(cfg.get("timeout", 120))) as resp: data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e: raise RuntimeError(f"API HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e: raise RuntimeError(f"API connection failed: {e.reason}") from e
        return "".join(x.get("text", "") for x in data.get("content", [])) if provider == "anthropic" else data["choices"][0]["message"]["content"]
    ns["load_api_config"], ns["save_api_config"], ns["api_chat"] = load, save, chat

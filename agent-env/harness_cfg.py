"""
Per-session harness homes and command lines for pi, opencode and codex.

Each session gets a fresh HOME (copied from the harness's pristine template home, so caches such
as opencode's provider package are reused) with a config pointing at the trace proxy under the
session's own path. Paths returned are host paths; the runner mounts them into the container.
"""
import json, os, shutil

AGENT_ENV = os.path.dirname(os.path.abspath(__file__))
MODEL = "flash-next"


def write_config(harness, home, base_url):
    if harness == "pi":
        d = os.path.join(home, ".pi", "agent")
        os.makedirs(d, exist_ok = True)
        json.dump({"providers": {"local": {
            "baseUrl": base_url, "api": "openai-completions", "apiKey": "dummy",
            "models": [{"id": MODEL, "reasoning": False, "contextWindow": 262144, "maxTokens": 32768}],
        }}}, open(os.path.join(d, "models.json"), "w"), indent = 1)
        json.dump({"defaultProvider": "local", "defaultModel": MODEL, "quietStartup": True},
                  open(os.path.join(d, "settings.json"), "w"), indent = 1)
    elif harness == "opencode":
        d = os.path.join(home, ".config", "opencode")
        os.makedirs(d, exist_ok = True)
        json.dump({
            "$schema": "https://opencode.ai/config.json",
            "autoupdate": False,
            "share": "disabled",
            "model": f"local/{MODEL}",
            "provider": {"local": {
                "npm": "@ai-sdk/openai-compatible", "name": "local",
                "options": {"baseURL": base_url, "apiKey": "dummy"},
                "models": {MODEL: {"name": MODEL, "limit": {"context": 262144, "output": 32768}}},
            }},
        }, open(os.path.join(d, "opencode.json"), "w"), indent = 1)
    elif harness == "codex":
        d = os.path.join(home, ".codex")
        os.makedirs(d, exist_ok = True)
        open(os.path.join(d, "config.toml"), "w").write(
            f'model = "{MODEL}"\n'
            f'model_provider = "local"\n'
            f'approval_policy = "never"\n'
            f'sandbox_mode = "danger-full-access"\n'
            f'\n[model_providers.local]\n'
            f'name = "local"\n'
            f'base_url = "{base_url}"\n'
            f'env_key = "LOCAL_API_KEY"\n'
            f'wire_api = "chat"\n'
            f'stream_idle_timeout_ms = 1800000\n'
            f'request_max_retries = 4\n'
            f'stream_max_retries = 10\n')
    else:
        raise ValueError(harness)


def make_home(harness, session, base_url, root):
    """Fresh HOME for one session, seeded from <agent-env>/home-<harness> (caches, no secrets)."""
    home = os.path.join(root, session)
    if os.path.exists(home):
        shutil.rmtree(home)
    seed = os.path.join(AGENT_ENV, f"home-{harness}")
    shutil.copytree(seed, home, symlinks = True) if os.path.isdir(seed) else os.makedirs(home)
    write_config(harness, home, base_url)
    return home


def command(harness, prompt):
    """argv run inside the container (harness bins on PATH via /opt/agents/bin)."""
    if harness == "pi":
        return ["pi", "-p", "--provider", "local", "--model", MODEL, "--no-session", prompt]
    if harness == "opencode":
        return ["opencode", "run", "--model", f"local/{MODEL}", prompt]
    if harness == "codex":
        return ["codex", "exec", "--skip-git-repo-check", prompt]
    raise ValueError(harness)


def env(harness, home):
    e = {"HOME": home, "LOCAL_API_KEY": "dummy", "PI_OFFLINE": "1", "NO_UPDATE_NOTIFIER": "1",
         "OPENCODE_DISABLE_AUTOUPDATE": "1", "CI": "1"}
    if harness == "codex":
        e["CODEX_HOME"] = os.path.join(home, ".codex")
    return e

import json
import os
import tempfile
from pathlib import Path


DEFAULTS = {
    "provider": "client", "model": "", "api_base": "", "api_key_env": "WECHAT_ASSISTANT_API_KEY",
    "codex_command": "codex", "wechat_config": "", "history_days": 30,
    "poll_seconds": 60, "analysis_seconds": 600, "batch_size": 120,
    "schedule": ["08:00", "12:00", "18:00"], "notifications": True,
    "port": 8766,
    "background_analysis": False,
}


def data_dir(path=None):
    root = Path(path or os.environ.get("WECHAT_ASSISTANT_HOME", Path.home() / ".wechat-project-assistant"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def load(path=None):
    root = data_dir(path)
    config_file = root / "config.json"
    saved = json.loads(config_file.read_text("utf-8")) if config_file.exists() else {}
    return root, DEFAULTS | saved


def save(root, config):
    # Readers may run in the web worker and MCP process at the same time.
    with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(json.dumps(config, ensure_ascii=False, indent=2).encode('utf-8'))
    try:
        os.replace(temporary, root / 'config.json')
    finally:
        temporary.unlink(missing_ok=True)

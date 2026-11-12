import json
import os
from pathlib import Path


DEFAULTS = {
    "provider": "codex", "model": "", "api_base": "", "api_key_env": "WECHAT_ASSISTANT_API_KEY",
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
    (root / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=2), "utf-8")

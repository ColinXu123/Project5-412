import os
import yaml


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    cfg["model"] = os.getenv("OPENAI_MODEL") or cfg.get("model", "gpt-4.1-mini")
    return cfg

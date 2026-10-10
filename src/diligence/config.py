"""Load the YAML/JSON configuration that drives the agent."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def project_root() -> Path:
    env = os.environ.get("DILIGENCE_ROOT")
    return Path(env) if env else Path(__file__).resolve().parents[2]


def load_env() -> None:
    """Load KEY=value lines from .env without overriding variables already set."""
    env_file = project_root() / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#") and value and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Config:
    root: Path
    settings: dict[str, Any]
    users: dict[str, Any]
    folders: dict[str, Any]
    crm: list[dict[str, Any]]
    sections: dict[str, Any]
    stages: dict[str, Any]
    glossary: dict[str, Any]

    def path(self, key: str) -> Path:
        return self.root / self.settings["paths"][key]

    @property
    def provider_name(self) -> str:
        return self.settings["provider"]

    @property
    def provider(self) -> dict[str, Any]:
        return self.settings["providers"][self.provider_name]

    def model(self, role: str) -> str:
        return self.provider["models"][role]

    def glossary_text(self) -> str:
        terms = self.glossary["terms"]
        lines = [f"- {k}: {v}" for k, v in terms.items()]
        return f"Firm glossary (version {self.glossary['version']}):\n" + "\n".join(lines)


@lru_cache(maxsize=1)
def load_config() -> Config:
    root = project_root()
    settings = _yaml(root / "config" / "settings.yaml")
    users_doc = _yaml(root / settings["paths"]["users"])
    registry = _yaml(root / "config" / "sections.yaml")
    crm_doc = json.loads((root / settings["paths"]["crm"]).read_text(encoding="utf-8"))
    return Config(
        root=root,
        settings=settings,
        users=users_doc["users"],
        folders=users_doc["folders"],
        crm=crm_doc["companies"],
        sections=registry["sections"],
        stages=registry["stages"],
        glossary=_yaml(root / "config" / "glossary.yaml"),
    )

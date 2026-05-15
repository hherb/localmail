"""Config loading and validation for localmail."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class DatabaseConfig(BaseModel):
    dsn: str


class AttachmentsConfig(BaseModel):
    root: Path = Path("~/localmail")

    @field_validator("root", mode="after")
    @classmethod
    def expand(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v))).resolve()


class DaemonConfig(BaseModel):
    idle_renew_seconds: int = 1740
    poll_seconds: int = 300


class GmailOAuthConfig(BaseModel):
    client_secrets_file: Path

    @field_validator("client_secrets_file", mode="after")
    @classmethod
    def expand(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v))).resolve()


class AccountConfig(BaseModel):
    name: str
    email: str
    imap_host: str
    imap_port: int = 993
    auth_method: Literal["password", "oauth2"]
    oauth_provider: Literal["gmail"] | None = None
    folder_allow: list[str] = Field(default_factory=list)
    folder_deny: list[str] = Field(default_factory=list)
    folder_deny_flags: list[str] = Field(default_factory=list)
    poll_seconds: int | None = None


class Config(BaseModel):
    database: DatabaseConfig
    attachments: AttachmentsConfig = AttachmentsConfig()
    daemon: DaemonConfig = DaemonConfig()
    gmail_oauth: GmailOAuthConfig | None = None
    accounts: list[AccountConfig] = Field(default_factory=list)


def default_config_path() -> Path:
    env = os.environ.get("LOCALMAIL_CONFIG")
    if env:
        return Path(os.path.expanduser(env))
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(os.path.expanduser(base)) / "localmail" / "config.toml"


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    with open(path, "rb") as f:
        data = tomllib.load(f)
    return Config.model_validate(data)

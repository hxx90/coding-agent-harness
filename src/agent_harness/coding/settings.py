"""Resolve user, project, environment and command-line configuration once."""

from __future__ import annotations

import json
import os
import shlex
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .types import CodingError, Json


@dataclass(frozen=True)
class ProviderSettings:
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    profile: str = "default"
    timeout: float = 60.0
    temperature: float | None = None
    max_output_tokens: int = 8192
    stream: bool = True


@dataclass(frozen=True)
class Settings:
    project: Path
    data_dir: Path
    provider: ProviderSettings
    mode: str = "build"
    max_turns: int = 40
    max_tokens: int = 250_000
    max_seconds: float = 1800.0
    context_chars: int = 160_000
    command_timeout: float = 120.0
    validation_command: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    sensitive: tuple[str, ...] = ()
    editable: tuple[str, ...] | None = None
    permission_rules: tuple[Json, ...] = ()
    mcp: Json = field(default_factory=dict, repr=False)
    skill_dirs: tuple[Path, ...] = ()

    def public(self) -> Json:
        data = asdict(self)
        data["provider"].pop("api_key")
        data["provider"]["api_key_configured"] = bool(self.provider.api_key)
        for server in data["mcp"].values():
            if isinstance(server, dict) and "env" in server:
                server["env"] = {key: "[REDACTED]" for key in server["env"]}
        return json.loads(json.dumps(data, default=str))


def _table(path: Path) -> Json:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CodingError("configuration", f"Cannot read {path}: {exc}") from exc


def _merge(left: Json, right: Json) -> Json:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _positive(value: Any, name: str, *, integer: bool = True) -> int | float:
    try:
        number = float(value)
        if isinstance(value, bool) or not 0 < number < float("inf"):
            raise ValueError
        if integer and not number.is_integer():
            raise ValueError
        return int(number) if integer else number
    except (TypeError, ValueError, OverflowError) as exc:
        raise CodingError("configuration", f"{name} must be a positive number") from exc


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CodingError("configuration", f"{name} must be an array of strings")
    return tuple(value)


def credential_path(environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    directory = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return directory / "agent-harness" / "credentials.json"


def credentials(environ: Mapping[str, str] | None = None) -> Json:
    path = credential_path(environ)
    if not path.exists():
        return {}
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise TypeError("expected an object")
        return result
    except (OSError, ValueError, TypeError) as exc:
        raise CodingError(
            "configuration", f"Cannot read credentials file {path}"
        ) from exc


def load_settings(
    project: Path,
    overrides: Json | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    env = os.environ if environ is None else environ
    args = {key: value for key, value in (overrides or {}).items() if value is not None}
    root = project.expanduser().resolve()
    if not root.is_dir():
        raise CodingError("configuration", f"Project directory does not exist: {root}")
    config_home = Path(env.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    global_path = Path(
        env.get(
            "AGENT_HARNESS_CONFIG", str(config_home / "agent-harness" / "config.toml")
        )
    )
    user = _table(global_path)
    local = _table(root / ".agent-harness.toml")
    raw = _merge(user, local)
    provider = raw.get("provider", {})
    agent = raw.get("agent", {})
    if not isinstance(provider, dict) or not isinstance(agent, dict):
        raise CodingError("configuration", "provider and agent must be TOML tables")
    profile = str(args.get("profile", provider.get("profile", "default")))
    profiles = raw.get("providers", {})
    if not isinstance(profiles, dict):
        raise CodingError("configuration", "providers must be a TOML table")
    if profile in profiles:
        if not isinstance(profiles[profile], dict):
            raise CodingError(
                "configuration", f"providers.{profile} must be a TOML table"
            )
        provider = _merge(provider, profiles[profile])
    base_url = (
        str(
            args.get(
                "base_url",
                env.get("AGENT_HARNESS_BASE_URL", provider.get("base_url", "")),
            )
        )
        .strip()
        .rstrip("/")
    )
    base_url = base_url.removesuffix("/chat/completions")
    if base_url:
        url = urlsplit(base_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise CodingError(
                "configuration",
                "base_url must be an HTTP(S) URL without credentials, query or fragment",
            )
    model = str(
        args.get("model", env.get("AGENT_HARNESS_MODEL", provider.get("model", "")))
    ).strip()
    key_env = str(
        args.get("api_key_env", provider.get("api_key_env", "AGENT_HARNESS_API_KEY"))
    )
    stored = credentials(env).get(profile, {})
    if not isinstance(stored, dict):
        raise CodingError("configuration", f"Invalid credentials profile: {profile}")
    key = str(env.get(key_env, stored.get("api_key", "")))
    data_dir = (
        Path(
            str(
                args.get(
                    "data_dir",
                    env.get(
                        "AGENT_HARNESS_DATA_DIR", str(Path.home() / ".agent-harness")
                    ),
                )
            )
        )
        .expanduser()
        .resolve()
    )
    if data_dir == root or root in data_dir.parents:
        raise CodingError(
            "configuration", "The data directory must be outside the project"
        )
    mode = str(args.get("mode", agent.get("mode", "build")))
    if mode not in {"plan", "build"}:
        raise CodingError("configuration", "mode must be plan or build")
    temperature = args.get("temperature", provider.get("temperature"))
    if temperature is not None:
        try:
            temperature = float(temperature)
            if not 0 <= temperature <= 2:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise CodingError(
                "configuration", "temperature must be between 0 and 2"
            ) from exc
    stream = args.get("stream", provider.get("stream", True))
    if not isinstance(stream, bool):
        raise CodingError("configuration", "stream must be true or false")

    legacy_path = root / ".agent-harness.json"
    legacy: Json = {}
    if legacy_path.exists():
        try:
            legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
            if not isinstance(legacy, dict) or legacy.get("schema_version") != 1:
                raise ValueError("schema_version must be 1")
        except (OSError, ValueError, TypeError) as exc:
            raise CodingError(
                "configuration", f"Invalid {legacy_path.name}: {exc}"
            ) from exc
    validation = args.get(
        "test_command", agent.get("test_command", legacy.get("validation_command", []))
    )
    if isinstance(validation, str):
        try:
            validation = shlex.split(validation)
        except ValueError as exc:
            raise CodingError("configuration", f"Invalid test_command: {exc}") from exc
    command = list(_strings(validation, "test_command"))
    if command and command[0] in {"python", "python3"}:
        command[0] = sys.executable
    # Project text may tighten permissions, but cannot silently approve execution.
    for config in (user, local):
        if not isinstance(config.get("permissions", {}), dict):
            raise CodingError("configuration", "permissions must be a TOML table")
    rules = user.get("permissions", {}).get("rules", [])
    project_rules = local.get("permissions", {}).get("rules", [])
    if not isinstance(rules, list) or not isinstance(project_rules, list):
        raise CodingError(
            "configuration", "permissions.rules must be an array of tables"
        )
    if any(not isinstance(rule, dict) for rule in [*rules, *project_rules]):
        raise CodingError("configuration", "Every permission rule must be a table")
    if any(rule.get("action") == "allow" for rule in project_rules):
        raise CodingError(
            "configuration",
            "Project permission rules cannot grant allow; use user configuration or --allow",
        )
    rules = [*rules, *project_rules]
    for tool in args.get("allow", []):
        rules.append({"tool": tool, "pattern": "*", "action": "allow"})
    for tool in args.get("deny", []):
        rules.append({"tool": tool, "pattern": "*", "action": "deny"})
    if args.get("yes"):
        rules.insert(0, {"tool": "*", "pattern": "*", "action": "allow"})
    for rule in rules:
        if (
            rule.get("action") not in {"allow", "ask", "deny"}
            or not isinstance(rule.get("tool"), str)
            or not isinstance(rule.get("pattern", "*"), str)
        ):
            raise CodingError(
                "configuration",
                "Permission rules require tool and action=allow|ask|deny",
            )
    mcp = raw.get("mcp", {})
    if not isinstance(mcp, dict):
        raise CodingError("configuration", "mcp must be a table")
    for name, server in mcp.items():
        if not isinstance(server, dict):
            raise CodingError("configuration", f"mcp.{name} must be a table")
        environment = server.get("env", {})
        if not isinstance(environment, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in environment.items()
        ):
            raise CodingError("configuration", f"mcp.{name}.env must contain strings")
    return Settings(
        project=root,
        data_dir=data_dir,
        provider=ProviderSettings(
            base_url=base_url,
            model=model,
            api_key=key,
            profile=profile,
            timeout=float(
                _positive(
                    args.get("timeout", provider.get("timeout", 60)),
                    "timeout",
                    integer=False,
                )
            ),
            temperature=temperature,
            max_output_tokens=int(
                _positive(provider.get("max_output_tokens", 8192), "max_output_tokens")
            ),
            stream=stream,
        ),
        mode=mode,
        max_turns=int(
            _positive(args.get("max_turns", agent.get("max_turns", 40)), "max_turns")
        ),
        max_tokens=int(
            _positive(
                args.get("max_tokens", agent.get("max_tokens", 250_000)), "max_tokens"
            )
        ),
        max_seconds=float(
            _positive(
                args.get("max_seconds", agent.get("max_seconds", 1800)),
                "max_seconds",
                integer=False,
            )
        ),
        context_chars=int(
            _positive(agent.get("context_chars", 160_000), "context_chars")
        ),
        command_timeout=float(
            _positive(
                agent.get("command_timeout", 120), "command_timeout", integer=False
            )
        ),
        validation_command=tuple(command),
        protected=_strings(legacy.get("protected_paths", []), "protected_paths"),
        sensitive=_strings(legacy.get("sensitive_paths", []), "sensitive_paths"),
        editable=_strings(legacy["editable_paths"], "editable_paths")
        if "editable_paths" in legacy
        else None,
        permission_rules=tuple(rules),
        mcp=mcp,
        skill_dirs=(
            root / ".agents" / "skills",
            config_home / "agent-harness" / "skills",
        ),
    )

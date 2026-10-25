"""裁判模型 profile 配置：YAML 配置文件 + 环境变量回退。

凭据不落盘：profile 里的 ``api_key_env`` 只是环境变量名，真正的 key 仍由
``.env`` 或进程环境提供。无配置文件、无选择参数时行为与旧版完全一致
（``MODEL_PROVIDER``/``MODEL_NAME`` 环境变量兜底）。

选择优先级：显式参数（CLI ``--model-profile``）> 环境变量
``JUDGE_MODEL_PROFILE`` > 配置文件 ``default`` > env 兜底。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 默认配置文件位置；可用 AUDITRAIL_JUDGE_MODEL_CONFIG 覆盖。
DEFAULT_CONFIG_PATH = _PROJECT_ROOT / "config" / "judge_models.yaml"

#: 选择启用 profile 的环境变量（优先级低于显式参数、高于配置文件 default）。
PROFILE_ENV = "JUDGE_MODEL_PROFILE"

#: 覆盖配置文件路径的环境变量。
CONFIG_PATH_ENV = "AUDITRAIL_JUDGE_MODEL_CONFIG"

_TOP_KEYS = {"default", "profiles"}
_PROFILE_KEYS = {"provider", "model", "base_url", "api_key_env", "temperature"}


@dataclass(frozen=True)
class JudgeModelSpec:
    """一个已解析的裁判模型选择。``name`` 为空表示来自 env 兜底。"""

    name: str
    provider: str
    model: str
    base_url: str = ""
    api_key_env: str = ""
    source: str = "env"  # "config" | "env"

    @property
    def chat_model_spec(self) -> str:
        return "%s:%s" % (self.provider, self.model)


def _config_loading_allowed() -> bool:
    """与 ``.env`` 加载共用的测试隔离守卫。

    tests/conftest.py 强制 ``AUDITRAIL_LOAD_DOTENV=0``，此时不得读取开发者
    本地的裁判模型配置文件，否则本地 profile（和凭据名）会渗入测试进程。
    """
    falsey = {"0", "false", "no", "off"}
    return str(os.environ.get("AUDITRAIL_LOAD_DOTENV", "true")).strip().lower() not in falsey


def validate_model_config(data: Any) -> dict[str, Any]:
    """校验并归一化配置文件内容。非法结构抛 ``ValueError``。"""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("judge model config must be a mapping")
    unknown = sorted(set(data) - _TOP_KEYS)
    if unknown:
        raise ValueError("judge model config has unknown keys: %s" % ", ".join(unknown))
    default = data.get("default")
    if default is not None and not isinstance(default, str):
        raise ValueError("judge model config 'default' must be a profile name string")
    profiles = data.get("profiles") or {}
    if not isinstance(profiles, dict):
        raise ValueError("judge model config 'profiles' must be a mapping")
    normalized: dict[str, Any] = {"profiles": {}}
    if default:
        normalized["default"] = default.strip()
    for name, item in profiles.items():
        if not isinstance(item, dict):
            raise ValueError("judge model profile %r must be a mapping" % name)
        unknown_keys = sorted(set(item) - _PROFILE_KEYS)
        if unknown_keys:
            raise ValueError(
                "judge model profile %r has unknown keys: %s" % (name, ", ".join(unknown_keys))
            )
        provider = str(item.get("provider") or "").strip()
        model = str(item.get("model") or "").strip()
        if not provider or not model:
            raise ValueError("judge model profile %r requires non-empty 'provider' and 'model'" % name)
        normalized["profiles"][str(name)] = {
            "provider": provider,
            "model": model,
            "base_url": str(item.get("base_url") or "").strip(),
            "api_key_env": str(item.get("api_key_env") or "").strip(),
        }
    if normalized.get("default") and normalized["default"] not in normalized["profiles"]:
        raise ValueError(
            "judge model config default %r is not a defined profile" % normalized["default"]
        )
    return normalized


def load_model_config(path: Optional[Path] = None) -> dict[str, Any]:
    """读取并校验配置文件；文件不存在或加载被守卫禁用时返回 ``{}``。"""
    if not _config_loading_allowed():
        return {}
    candidate = path or Path(os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)
    if not candidate.exists():
        return {}
    import yaml

    try:
        data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError("failed to load judge model config %s: %s" % (candidate, exc)) from exc
    try:
        return validate_model_config(data)
    except ValueError as exc:
        raise ValueError("invalid judge model config %s: %s" % (candidate, exc)) from exc


def resolve_judge_model(
    profile: Optional[str] = None,
    *,
    config: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> JudgeModelSpec:
    """按优先级解析生效的裁判模型。

    优先级：显式 ``profile`` 参数 > ``JUDGE_MODEL_PROFILE`` 环境变量 >
    配置文件 ``default`` > ``MODEL_PROVIDER``/``MODEL_NAME`` env 兜底。
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    cfg: Mapping[str, Any] = load_model_config() if config is None else config
    name = (profile or "").strip() or str(env.get(PROFILE_ENV) or "").strip() or str(
        cfg.get("default") or ""
    ).strip()
    profiles = cfg.get("profiles") or {}
    if name:
        item = profiles.get(name)
        if item is None:
            available = ", ".join(sorted(profiles)) or "(none)"
            raise RuntimeError(
                "judge model profile %r not found; available profiles: %s" % (name, available)
            )
        api_key_env = str(item.get("api_key_env") or "")
        if api_key_env and not str(env.get(api_key_env) or "").strip():
            raise RuntimeError(
                "judge model profile %r requires the API key env var %s to be set" % (name, api_key_env)
            )
        return JudgeModelSpec(
            name=name,
            provider=str(item["provider"]),
            model=str(item["model"]),
            base_url=str(item.get("base_url") or ""),
            api_key_env=api_key_env,
            source="config",
        )
    # env 兜底：与配置文件出现之前的旧行为完全一致。
    return JudgeModelSpec(
        name="",
        provider=str(env.get("MODEL_PROVIDER") or "openai"),
        model=str(env.get("MODEL_NAME") or "gpt-4.1"),
        source="env",
    )


def chat_model_kwargs(spec: JudgeModelSpec, *, environ: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
    """生成 ``init_chat_model`` 的透传 kwargs（base_url / api_key）。"""
    env: Mapping[str, str] = os.environ if environ is None else environ
    kwargs: dict[str, Any] = {}
    if spec.base_url:
        kwargs["base_url"] = spec.base_url
    if spec.api_key_env:
        # resolve_judge_model 已校验存在；这里防御性再查一次。
        value = str(env.get(spec.api_key_env) or "").strip()
        if not value:
            raise RuntimeError(
                "judge model profile %r requires the API key env var %s to be set"
                % (spec.name, spec.api_key_env)
            )
        kwargs["api_key"] = value
    return kwargs


def describe_model_profiles(
    *,
    config: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """列出可选裁判模型 profile 及可用性，供 API/前端展示。

    只暴露 profile 名、provider、model 和可用性；环境变量名与凭据值都不下发。
    """
    env: Mapping[str, str] = os.environ if environ is None else environ
    cfg: Mapping[str, Any] = load_model_config() if config is None else config
    profiles = cfg.get("profiles") or {}
    rows = []
    for name in sorted(profiles):
        item = profiles[name]
        api_key_env = str(item.get("api_key_env") or "")
        missing_key = bool(api_key_env) and not str(env.get(api_key_env) or "").strip()
        rows.append(
            {
                "id": name,
                "provider": str(item["provider"]),
                "model": str(item["model"]),
                "available": not missing_key,
                "disabled_reason": "API key 未配置" if missing_key else None,
            }
        )
    fallback = resolve_judge_model(None, config={}, environ=env)
    return {
        "default": str(cfg.get("default") or ""),
        "selected": str(env.get(PROFILE_ENV) or "").strip(),
        "env_fallback": {"provider": fallback.provider, "model": fallback.model},
        "profiles": rows,
    }

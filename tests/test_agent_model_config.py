"""裁判模型 profile 配置（agents/model_config.py）的确定性测试。

不读开发者真实配置文件、不依赖模型凭据、不要求 .[agent]。
注意 conftest 强制 AUDITRAIL_LOAD_DOTENV=0，因此 load_model_config 默认
被守卫禁用；需要测试文件加载时用 monkeypatch 临时放开并指向 tmp_path。
"""

import pytest

from auditrail.agents.model_config import (
    CONFIG_PATH_ENV,
    PROFILE_ENV,
    chat_model_kwargs,
    load_model_config,
    resolve_judge_model,
    validate_model_config,
)


def _config():
    return validate_model_config(
        {
            "default": "kimi",
            "profiles": {
                "kimi": {
                    "provider": "openai",
                    "model": "kimi-for-coding",
                    "base_url": "https://api.kimi.com/coding/v1",
                    "api_key_env": "OPENAI_API_KEY",
                },
                "ds": {"provider": "deepseek", "model": "deepseek-v4-flash", "api_key_env": "DEEPSEEK_API_KEY"},
            },
        }
    )


def test_validate_rejects_unknown_keys_and_missing_fields():
    with pytest.raises(ValueError, match="unknown keys"):
        validate_model_config({"defalt": "x"})
    with pytest.raises(ValueError, match="unknown keys"):
        validate_model_config({"profiles": {"a": {"provider": "openai", "model": "m", "api_key": "sk-leak"}}})
    with pytest.raises(ValueError, match="requires non-empty"):
        validate_model_config({"profiles": {"a": {"provider": "openai"}}})
    with pytest.raises(ValueError, match="not a defined profile"):
        validate_model_config({"default": "ghost", "profiles": {"a": {"provider": "p", "model": "m"}}})
    with pytest.raises(ValueError, match="must be a mapping"):
        validate_model_config({"profiles": ["not-a-dict"]})


def test_resolve_prefers_explicit_profile_over_env_and_default():
    spec = resolve_judge_model("ds", config=_config(), environ={"DEEPSEEK_API_KEY": "x", PROFILE_ENV: "kimi", "OPENAI_API_KEY": "y"})
    assert spec.name == "ds"
    assert spec.provider == "deepseek"
    assert spec.source == "config"


def test_resolve_env_profile_beats_config_default():
    spec = resolve_judge_model(None, config=_config(), environ={PROFILE_ENV: "ds", "DEEPSEEK_API_KEY": "x"})
    assert spec.name == "ds"


def test_resolve_config_default_when_nothing_selected():
    spec = resolve_judge_model(None, config=_config(), environ={"OPENAI_API_KEY": "y"})
    assert spec.name == "kimi"
    assert spec.chat_model_spec == "openai:kimi-for-coding"
    assert spec.base_url == "https://api.kimi.com/coding/v1"


def test_resolve_env_fallback_without_config():
    spec = resolve_judge_model(None, config={}, environ={"MODEL_PROVIDER": "deepseek", "MODEL_NAME": "deepseek-v4-flash"})
    assert spec.name == ""
    assert spec.source == "env"
    assert spec.chat_model_spec == "deepseek:deepseek-v4-flash"
    # 与旧默认值一致
    spec2 = resolve_judge_model(None, config={}, environ={})
    assert (spec2.provider, spec2.model) == ("openai", "gpt-4.1")


def test_resolve_unknown_profile_lists_available():
    with pytest.raises(RuntimeError, match="available profiles: ds, kimi"):
        resolve_judge_model("ghost", config=_config(), environ={})


def test_resolve_requires_api_key_env_when_declared():
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        resolve_judge_model("kimi", config=_config(), environ={})
    # 未声明 api_key_env 的 profile 不强制检查（回退 provider 默认环境变量）
    cfg = validate_model_config({"profiles": {"plain": {"provider": "openai", "model": "m"}}})
    spec = resolve_judge_model("plain", config=cfg, environ={})
    assert spec.api_key_env == ""


def test_chat_model_kwargs_passes_base_url_and_key():
    spec = resolve_judge_model("kimi", config=_config(), environ={"OPENAI_API_KEY": "sk-test"})
    kwargs = chat_model_kwargs(spec, environ={"OPENAI_API_KEY": "sk-test"})
    assert kwargs == {"base_url": "https://api.kimi.com/coding/v1", "api_key": "sk-test"}
    env_spec = resolve_judge_model(None, config={}, environ={})
    assert chat_model_kwargs(env_spec, environ={}) == {}


def test_load_model_config_guarded_by_dotenv_flag(tmp_path, monkeypatch):
    path = tmp_path / "judge_models.yaml"
    path.write_text("default: a\nprofiles:\n  a:\n    provider: openai\n    model: m\n", encoding="utf-8")
    # conftest 默认 AUDITRAIL_LOAD_DOTENV=0：守卫生效，不读文件
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "0")
    assert load_model_config(path) == {}
    # 显式放开后正常加载
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "1")
    cfg = load_model_config(path)
    assert cfg["default"] == "a"
    assert cfg["profiles"]["a"]["model"] == "m"


def test_load_model_config_invalid_yaml_and_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "1")
    bad = tmp_path / "bad.yaml"
    bad.write_text("profiles: [oops\n", encoding="utf-8")
    with pytest.raises(ValueError, match="failed to load"):
        load_model_config(bad)
    wrong = tmp_path / "wrong.yaml"
    wrong.write_text("default: ghost\nprofiles:\n  a:\n    provider: p\n    model: m\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a defined profile"):
        load_model_config(wrong)


def test_load_model_config_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "1")
    path = tmp_path / "custom.yaml"
    path.write_text("profiles:\n  a:\n    provider: p\n    model: m\n", encoding="utf-8")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(path))
    cfg = load_model_config()
    assert "a" in cfg["profiles"]
    # 不存在的路径静默回退（保持 env 兜底行为）
    monkeypatch.setenv(CONFIG_PATH_ENV, str(tmp_path / "missing.yaml"))
    assert load_model_config() == {}


def test_engine_metadata_records_effective_profile(tmp_path, monkeypatch):
    from auditrail.judges.deepagent_engine import DeepAgentJudgeEngine

    path = tmp_path / "judge_models.yaml"
    path.write_text(
        "default: kimi\nprofiles:\n  kimi:\n    provider: openai\n    model: kimi-for-coding\n    api_key_env: OPENAI_API_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "1")
    monkeypatch.setenv(CONFIG_PATH_ENV, str(path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    metadata = DeepAgentJudgeEngine(model_profile="kimi")._metadata()
    assert metadata["model_profile"] == "kimi"
    assert metadata["model"] == "kimi-for-coding"
    assert metadata["model_source"] == "config"

    # 解析失败（缺 key）时回退到 env metadata，不掩盖运行期错误
    monkeypatch.delenv("OPENAI_API_KEY")
    fallback = DeepAgentJudgeEngine(model_profile="kimi")._metadata()
    assert fallback["model_source"] == "env"

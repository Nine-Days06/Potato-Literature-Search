# tests/test_settings.py
"""
配置层测试：验证 config/settings.py 的环境变量读取逻辑。

背景：原先 provider 型号、并发数、批大小等硬编码在 settings.py 里，
每换一次模型都要改源码。现改为全部可由 config/.env 覆盖，
本文件锁定这些覆盖行为不被回退。
"""

import importlib
import os
import unittest
from unittest import mock

import config.settings as settings


class TestEnvHelpers(unittest.TestCase):
    """三个取值助手：空串当未设置、非法值回退默认"""

    def test_str_returns_env_value(self):
        with mock.patch.dict(os.environ, {"_T_STR": "hello"}, clear=False):
            self.assertEqual(settings._env_str("_T_STR", "def"), "hello")

    def test_str_treats_blank_as_unset(self):
        """`.env` 里写 `KEY=` 等同于不写，否则会把默认值覆盖成空串"""
        for blank in ("", "   ", "\t"):
            with mock.patch.dict(os.environ, {"_T_STR": blank}, clear=False):
                self.assertEqual(settings._env_str("_T_STR", "def"), "def")

    def test_str_missing_uses_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("_T_STR", None)
            self.assertEqual(settings._env_str("_T_STR", "def"), "def")

    def test_int_parses(self):
        with mock.patch.dict(os.environ, {"_T_INT": "42"}, clear=False):
            self.assertEqual(settings._env_int("_T_INT", 7), 42)

    def test_int_invalid_falls_back(self):
        with mock.patch.dict(os.environ, {"_T_INT": "abc"}, clear=False):
            self.assertEqual(settings._env_int("_T_INT", 7), 7)

    def test_int_blank_falls_back(self):
        with mock.patch.dict(os.environ, {"_T_INT": ""}, clear=False):
            self.assertEqual(settings._env_int("_T_INT", 7), 7)

    def test_bool_accepts_common_truthy(self):
        for v in ("1", "true", "TRUE", "yes", "On"):
            with mock.patch.dict(os.environ, {"_T_BOOL": v}, clear=False):
                self.assertTrue(settings._env_bool("_T_BOOL", False), v)

    def test_bool_accepts_falsy(self):
        for v in ("0", "false", "no", "off"):
            with mock.patch.dict(os.environ, {"_T_BOOL": v}, clear=False):
                self.assertFalse(settings._env_bool("_T_BOOL", True), v)

    def test_bool_blank_uses_default(self):
        with mock.patch.dict(os.environ, {"_T_BOOL": ""}, clear=False):
            self.assertTrue(settings._env_bool("_T_BOOL", True))


class TestLLMSettingsFromEnv(unittest.TestCase):
    """真实配置项必须能被环境变量覆盖（reload 模块以重新求值）"""

    def _reload_with(self, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            return importlib.reload(settings)

    def tearDown(self):
        # 还原成 .env / 默认值状态，避免污染其他测试
        importlib.reload(settings)

    def test_provider_switchable(self):
        m = self._reload_with(LLM_PROVIDER="deepseek")
        self.assertEqual(m.LLM_PROVIDER, "deepseek")

    def test_model_overridable(self):
        m = self._reload_with(OPENAI_MODEL="group/other-model")
        self.assertEqual(m.LLM_PROVIDER_CONFIGS["openai"]["model"], "group/other-model")

    def test_concurrency_and_batch_size_overridable(self):
        m = self._reload_with(LLM_CONCURRENCY="8", LLM_BATCH_SIZE="10")
        self.assertEqual(m.LLM_CONCURRENCY, 8)
        self.assertEqual(m.LLM_BATCH_SIZE, 10)

    def test_timeout_flows_into_provider_kwargs(self):
        """LLM_TIMEOUT 必须真正传进 extra_kwargs，否则只是摆设"""
        m = self._reload_with(LLM_TIMEOUT="300")
        self.assertEqual(m.LLM_TIMEOUT, 300)
        self.assertEqual(m.LLM_PROVIDER_CONFIGS["openai"]["extra_kwargs"]["timeout"], 300)
        self.assertEqual(m.LLM_PROVIDER_CONFIGS["deepseek"]["extra_kwargs"]["timeout"], 300)

    def test_max_tokens_flows_into_provider_kwargs(self):
        m = self._reload_with(LLM_MAX_TOKENS="2048")
        for name in ("zhipu", "deepseek", "openai"):
            self.assertEqual(
                m.LLM_PROVIDER_CONFIGS[name]["extra_kwargs"]["max_tokens"], 2048, name)

    def test_invalid_provider_raises_on_import(self):
        """拼错 provider 应在导入时就报错，而不是跑到一半静默失败"""
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "no-such-provider"}, clear=False):
            with self.assertRaises(ValueError):
                importlib.reload(settings)


class TestProviderConfigShape(unittest.TestCase):
    """每个 provider 的配置字典结构完整，_build_client 才能正常消费"""

    REQUIRED = ("api_key_env", "client_type", "model", "base_url",
                "extra_kwargs", "fix_multi_array")

    def test_every_provider_has_required_keys(self):
        for name, cfg in settings.LLM_PROVIDER_CONFIGS.items():
            for key in self.REQUIRED:
                self.assertIn(key, cfg, f"{name} 缺少 {key}")

    def test_default_provider_is_registered(self):
        self.assertIn(settings.LLM_PROVIDER, settings.LLM_PROVIDER_CONFIGS)

    def test_client_type_is_known(self):
        for name, cfg in settings.LLM_PROVIDER_CONFIGS.items():
            self.assertIn(cfg["client_type"], ("openai", "zhipuai"), name)


if __name__ == "__main__":
    unittest.main()

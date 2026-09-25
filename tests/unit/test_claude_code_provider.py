"""Claude Code (subscription) provider: CLI arguments, environment isolation, output/error handling, and the
orchestrator's fallback routing (D-030). The error envelopes are real CLI captures (fixtures/real/PROVENANCE.md)."""
import json
from pathlib import Path

import pytest

from tradingsystem.ai import orchestrator as orch_mod
from tradingsystem.ai.budget import CostGovernor, UsageStore
from tradingsystem.ai.orchestrator import Orchestrator
from tradingsystem.ai.providers import claude_code as cc
from tradingsystem.ai.providers.base import LLMProvider, LLMResult, ProviderError
from tradingsystem.core.settings import AIBudgetCfg, AIProviderCfg, load_settings
from tradingsystem.core.timeutil import now_ms

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "real"
NOW = 1790334600000


@pytest.fixture
def prov(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(cc.tempfile, "gettempdir", lambda: str(tmp_path))
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", effort="medium", free_tier=True, cli_path=str(exe))
    return cc.ClaudeCodeProvider("claude_code", cfg, "sonnet", None)


def real_envelope() -> dict:
    return json.loads((FIX / "claude_code_not_logged_in.json").read_text(encoding="utf-8"))


def test_args_disable_tools_settings_and_mcp(prov):
    schema = {"type": "object", "properties": {"x": {"type": "number", "minimum": 0}}, "required": ["x"]}
    a = prov.build_args("sys.md", schema)
    assert a[a.index("--tools") + 1] == "" and a[a.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in a and "--no-session-persistence" in a
    assert a[a.index("--system-prompt-file") + 1] == "sys.md" and a[a.index("--model") + 1] == "sonnet"
    assert a[a.index("--effort") + 1] == "medium" and "--fallback-model" not in a
    sent = json.loads(a[a.index("--json-schema") + 1])
    assert sent["additionalProperties"] is False and "minimum" not in sent["properties"]["x"]


def test_child_env_keeps_no_secrets_and_no_api_billing():
    environ = {"PATH": "p", "USERPROFILE": "u", "APPDATA": "a", "ANTHROPIC_API_KEY": "sk-ant-x",
               "ANTHROPIC_BASE_URL": "http://proxy", "GOOGLE_API_KEY": "g", "MT5_DEMO_PASSWORD": "pw",
               "DASHBOARD_TOKEN": "t", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s"}
    assert cc.child_env(None, environ) == {"PATH": "p", "USERPROFILE": "u", "APPDATA": "a"}
    assert cc.child_env("oat-token", environ)["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-token"


def test_real_not_logged_in_output_cools_down_and_reports(prov):
    with pytest.raises(ProviderError) as e:
        prov.parse((FIX / "claude_code_not_logged_in.json").read_text(encoding="utf-8"), "", 1)
    assert not e.value.retryable and "not signed in" in str(e.value)
    assert prov.cooldown_until_ms > 0
    assert "not signed in" in prov.unavailable_reason()        # served from the cooldown, no CLI spawned


def test_real_auth_status_logged_out():
    st = json.loads((FIX / "claude_code_auth_status_logged_out.json").read_text(encoding="utf-8"))
    assert "not signed in" in cc.auth_problem("claude_code", st)
    assert cc.auth_problem("claude_code", {**st, "loggedIn": True, "authMethod": "claude.ai"}) is None
    assert "API key" in cc.auth_problem("claude_code", {**st, "loggedIn": True, "authMethod": "api_key"})


def test_usage_limit_sets_cooldown_until_reset(prov):
    reset_s = now_ms() // 1000 + 3 * 3600
    doc = {**real_envelope(), "result": f"Claude AI usage limit reached|{reset_s}", "api_error_status": 429}
    with pytest.raises(ProviderError) as e:
        prov.parse(json.dumps(doc), "", 1)
    assert e.value.rate_limited and not e.value.retryable
    assert prov.cooldown_until_ms == reset_s * 1000 and "usage limit" in prov.unavailable_reason()
    assert cc.limit_reset_ms(f"limit reached|{NOW // 1000 + 60}", NOW) == NOW + 60_000
    assert cc.limit_reset_ms("You've hit your limit · resets 5pm", NOW) == NOW + cc.DEFAULT_COOLDOWN_MS
    assert cc.limit_reset_ms("limit reached|4102444800", NOW) == NOW + cc.DEFAULT_COOLDOWN_MS   # implausibly far


def test_overloaded_is_retryable(prov):
    doc = {**real_envelope(), "result": "API Error: 529 Overloaded", "api_error_status": 529}
    with pytest.raises(ProviderError) as e:
        prov.parse(json.dumps(doc), "", 1)
    assert e.value.retryable and prov.cooldown_until_ms == 0


def test_success_envelope_maps_to_llm_result(prov):
    # structural: the real envelope with a success result filled in (a real success capture replaces this after H11)
    doc = {**real_envelope(), "is_error": False, "subtype": "success", "result": "", "structured_output": {"x": 1},
           "total_cost_usd": 0.0421,
           "usage": {"input_tokens": 12, "cache_creation_input_tokens": 9000, "cache_read_input_tokens": 1500,
                     "output_tokens": 800},
           "modelUsage": {"claude-haiku-4-5": {"outputTokens": 20}, "claude-sonnet-5": {"outputTokens": 780}}}
    r = prov.parse("warning line\n" + json.dumps(doc), "", 0)
    assert r.data == {"x": 1} and json.loads(r.text) == {"x": 1}
    assert r.model == "claude-sonnet-5" and r.input_tokens == 10512 and r.cached_input_tokens == 1500
    assert r.output_tokens == 800 and r.extra["api_equivalent_usd"] == pytest.approx(0.0421)
    assert prov.cost_of(r) == 0.0                                  # flat subscription


def test_missing_cli_is_a_provider_error(tmp_path):
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", cli_path=str(tmp_path / "nope.exe"))
    with pytest.raises(ProviderError):
        cc.ClaudeCodeProvider("claude_code", cfg, "sonnet", None)


# ------------------------------------------------------------------ orchestrator routing
class Stub(LLMProvider):
    def __init__(self, name, why=None):
        super().__init__(name, AIProviderCfg(kind="gemini", model="m", free_tier=True), "m", "k")
        self.why = why

    def unavailable_reason(self):
        return self.why

    async def _call(self, *a):
        return LLMResult(self.name, self.model, text="{}", data={})


def orch_with(tmp_path, monkeypatch, providers, fallback="gemini"):
    s = load_settings(env_path=Path("nope.env"), extra_env={"ACTIVE_AI_PROVIDER": "claude_code"})
    s = s.model_copy(update={"ai": s.ai.model_copy(update={"fallback_provider": fallback})})

    def make(settings, name=None, **kw):
        p = providers[name]
        if isinstance(p, Exception):
            raise p
        return p
    monkeypatch.setattr(orch_mod, "make_provider", make)
    usage = UsageStore(tmp_path / "app.db")
    return Orchestrator(s, None, None, None, usage, CostGovernor(AIBudgetCfg(), usage))


def test_route_active_when_available(tmp_path, monkeypatch):
    o = orch_with(tmp_path, monkeypatch, {"claude_code": Stub("claude_code"), "gemini": Stub("gemini")})
    assert o.provider().name == "claude_code" and o.route == ("claude_code", None)


def test_route_to_fallback_and_back(tmp_path, monkeypatch):
    cl = Stub("claude_code", why="usage limit")
    o = orch_with(tmp_path, monkeypatch, {"claude_code": cl, "gemini": Stub("gemini")})
    assert o.provider().name == "gemini" and o.route == ("gemini", "usage limit")
    cl.why = None
    assert o.provider().name == "claude_code" and o.route == ("claude_code", None)
    assert o.provider("gemini").name == "gemini"                   # explicit names are never rerouted


def test_route_errors_name_both_problems(tmp_path, monkeypatch):
    o = orch_with(tmp_path, monkeypatch, {"claude_code": ProviderError("CLI not found", retryable=False),
                                          "gemini": ProviderError("gemini: missing API key", retryable=False)})
    with pytest.raises(ProviderError) as e:
        o.provider()
    assert "CLI not found" in str(e.value) and "missing API key" in str(e.value)


def test_no_fallback_raises_reason(tmp_path, monkeypatch):
    o = orch_with(tmp_path, monkeypatch, {"claude_code": Stub("claude_code", why="not signed in")}, fallback=None)
    with pytest.raises(ProviderError, match="not signed in"):
        o.provider()


def test_config_rejects_unknown_fallback_and_tolerates_same_as_active():
    with pytest.raises(Exception, match="fallback_provider"):
        load_settings(env_path=Path("nope.env"), extra_env={"AI_FALLBACK_PROVIDER": "nosuch"})
    # the user's .env still says ACTIVE_AI_PROVIDER=gemini while config.yaml has fallback gemini: must load
    s = load_settings(env_path=Path("nope.env"), extra_env={"ACTIVE_AI_PROVIDER": "gemini"})
    assert s.ai.active_provider == "gemini" and s.ai.fallback is None


def test_same_as_active_fallback_is_not_used(tmp_path, monkeypatch):
    o = orch_with(tmp_path, monkeypatch, {"claude_code": Stub("claude_code", why="usage limit")}, fallback="claude_code")
    with pytest.raises(ProviderError, match="usage limit"):
        o.provider()

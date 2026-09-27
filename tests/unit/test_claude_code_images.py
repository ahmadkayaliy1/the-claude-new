"""Chart images through the providers (Phase 3, handoff §3.7.2 "Image transport"): Claude Code CLI arguments and the
stream-json user line, parsing a stream-json log, the one-time user-line shape probe, the repair loop (images on the
first attempt only, ledger rows with role/images/image_tokens_est), text-only providers dropping images, and the
per-role effort. No real CLI is started: a scripted process stands in for it. The stream-json log below is
synthetic in the shape documented for the CLI; a redacted real capture from the Phase 3 live call
(``fixtures/real/claude_code_stream_json_result.jsonl``) is parsed as well once it exists."""
import asyncio
import base64
import copy
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tradingsystem.ai import repair as repair_mod
from tradingsystem.ai.budget import CostGovernor, RateLimiter, UsageStore
from tradingsystem.ai.contract import Recommendation
from tradingsystem.ai.providers import CHARTS_DISABLED, make_provider
from tradingsystem.ai.providers import claude_code as cc
from tradingsystem.ai.providers import gemini as gm
from tradingsystem.ai.providers import openai_chat as oc
from tradingsystem.ai.providers.anthropic_claude import AnthropicProvider
from tradingsystem.ai.providers.base import ImageInput, LLMProvider, LLMResult, ProviderError
from tradingsystem.ai.repair import CHARTS_SEEN_NOTE, NO_CHARTS_NOTE, generate_validated
from tradingsystem.core.settings import AIBudgetCfg, AIProviderCfg, load_settings

from .test_contract import BASE

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "real"
PNG_1W = b"\x89PNG\r\n\x1a\n" + bytes(range(256))          # carries \r and \n bytes: base64 must keep one line
PNG_1D = b"\x89PNG\r\n\x1a\n" + bytes(range(255, -1, -1))
IMGS = [ImageInput("Chart 1w — BTCUSDT BTCUSDT, last 60 closed candles, analysis prices", PNG_1W, token_est=384),
        ImageInput("Chart 1d — BTCUSDT BTCUSDT, last 120 closed candles, analysis prices", PNG_1D, token_est=384)]
SCHEMA = {"type": "object", "title": "Out", "properties": {"x": {"type": "number", "minimum": 0}}, "required": ["x"]}
ANSWER = json.dumps({"x": 1})

INIT = {"type": "system", "subtype": "init", "cwd": "C:\\Temp\\tradingsystem-claude-code", "session_id": "s-1",
        "tools": [], "mcp_servers": [], "model": "claude-sonnet-5", "permissionMode": "default",
        "apiKeySource": "none", "claude_code_version": "2.1.282", "output_style": "default"}
ASSISTANT = {"type": "assistant", "session_id": "s-1", "parent_tool_use_id": None,
             "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
                         "content": [{"type": "text", "text": ANSWER}], "stop_reason": None,
                         "usage": {"input_tokens": 9, "output_tokens": 1450}}}
RATE = {"type": "rate_limit_event", "session_id": "s-1",
        "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 1790340000}}
RESULT = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 41234, "duration_api_ms": 40111,
          "num_turns": 1, "result": ANSWER, "stop_reason": "end_turn", "session_id": "s-1", "total_cost_usd": 0.0873,
          "usage": {"input_tokens": 9, "cache_creation_input_tokens": 3100, "cache_read_input_tokens": 17800,
                    "output_tokens": 1450},
          "modelUsage": {"claude-haiku-4-5": {"outputTokens": 12}, "claude-sonnet-5": {"outputTokens": 1450}}}


def stream(*docs, noise: str = "") -> str:
    lines = [json.dumps(d) for d in docs]
    if noise:
        lines.insert(1, noise)
    return "\n".join(lines) + "\n"


OK_STREAM = stream(INIT, ASSISTANT, RATE, RESULT, noise="[warn] slow start")
PARSER_REJECT = (b"", b"Error parsing streaming input line (type=user, 1234 chars): TypeError\n", 1)


@pytest.fixture
def prov(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    monkeypatch.setattr(cc.tempfile, "gettempdir", lambda: str(tmp_path))
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", effort="medium", free_tier=True, cli_path=str(exe))
    return cc.ClaudeCodeProvider("claude_code", cfg, "sonnet", None)


class ScriptedProc:
    """Stands in for one CLI process: answers its scripted (stdout, stderr, exit code) and keeps what it was fed."""

    def __init__(self, out: bytes, err: bytes, rc: int) -> None:
        self._answer, self.returncode, self.stdin = (out, err, rc), None, None

    async def communicate(self, data=None):
        self.stdin = data
        self.returncode = self._answer[2]
        return self._answer[0], self._answer[1]

    def kill(self):
        self.returncode = -9

    async def wait(self):
        return self.returncode


@pytest.fixture
def cli(prov, monkeypatch):
    """Scripted CLI: ``cli.script`` holds the answers of the next processes; every start is recorded with its
    arguments and the system prompt file it was given (read at start, the provider deletes it afterwards)."""
    rec = SimpleNamespace(script=[], procs=[], args=[], systems=[])

    async def spawn(*args, **kw):
        out, err, rc = rec.script.pop(0)
        rec.args.append(list(args))
        rec.systems.append(Path(args[args.index("--system-prompt-file") + 1]).read_text(encoding="utf-8"))
        rec.procs.append(ScriptedProc(out.encode() if isinstance(out, str) else out, err, rc))
        return rec.procs[-1]
    monkeypatch.setattr(cc.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(cc, "START_STAGGER_S", 0.0)            # no 15 s gap between the probe's two starts
    prov.api_key = "token"                                      # auth trusted → no `claude auth status` process
    prov._auth_problem, prov._auth_checked = None, time.monotonic()
    return rec


def sent_line(proc: ScriptedProc) -> dict:
    raw = proc.stdin.decode("utf-8")
    assert raw.endswith("\n") and raw.count("\n") == 1
    return json.loads(raw)


# ------------------------------------------------------------------ arguments and the user line
def test_build_args_without_images_unchanged(prov):
    assert prov.build_args("sys.md", None) == [
        prov.exe, "-p", "--output-format", "json", "--model", "sonnet", "--system-prompt-file", "sys.md", "--tools", "",
        "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence", "--effort", "medium"]


def test_build_args_with_images_stream_json_and_never_json_schema(prov):
    prov.cfg = prov.cfg.model_copy(update={"structured_output": "native", "fallback_model": "haiku"})
    assert prov.build_args("sys.md", SCHEMA, images=True) == [
        prov.exe, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--model", "sonnet", "--system-prompt-file", "sys.md", "--tools", "", "--strict-mcp-config",
        "--setting-sources", "", "--no-session-persistence", "--effort", "medium", "--fallback-model", "haiku"]
    assert "--json-schema" in prov.build_args("sys.md", SCHEMA)               # text calls keep native mode
    assert "JSON Schema" in prov.system_text("S", SCHEMA, force_prompt=True)  # image calls: schema in the prompt
    assert prov.system_text("S", SCHEMA) == "S"


def test_user_message_line_shapes():
    user = "Cycle — BTCUSDT\nانظر\u2028end"                         # non-ASCII, a newline and U+2028 in the text
    line = cc.user_message_line(user, IMGS)
    assert line.isascii() and line.endswith("\n") and line.count("\n") == 1
    doc = json.loads(line)
    assert doc["type"] == "user" and doc["message"]["role"] == "user" and set(doc) == {"type", "message"}
    blocks = doc["message"]["content"]
    assert [b["type"] for b in blocks] == ["text", "text", "image", "text", "image"]
    assert blocks[0]["text"] == user                                   # the prompt first
    assert blocks[1]["text"] == IMGS[0].label and blocks[3]["text"] == IMGS[1].label   # each caption before its image
    assert base64.b64decode(blocks[2]["source"]["data"]) == PNG_1W
    assert base64.b64decode(blocks[4]["source"]["data"]) == PNG_1D
    assert blocks[2]["source"]["type"] == "base64" and blocks[2]["source"]["media_type"] == "image/png"
    alt = json.loads(cc.user_message_line(user, IMGS, shape="content"))
    assert alt == {"type": "user", "content": blocks}
    with pytest.raises(ValueError):
        cc.user_message_line(user, IMGS, shape="bogus")


# ------------------------------------------------------------------ parsing a stream-json log
def test_parse_stream_json_log(prov):
    r = prov.parse(OK_STREAM, "", 0)
    assert r.text == ANSWER and r.model == "claude-sonnet-5" and r.request_id == "s-1"
    assert r.input_tokens == 9 + 3100 + 17800 and r.cached_input_tokens == 17800 and r.output_tokens == 1450
    assert r.extra["num_turns"] == 1 and r.extra["api_equivalent_usd"] == pytest.approx(0.0873)
    assert r.extra["cache_creation_input_tokens"] == 3100 and r.stop_reason == "end_turn"


def test_parse_stream_json_error_result_is_classified(prov):
    reset_s = int(time.time()) + 3600
    err = {**RESULT, "is_error": True, "subtype": "success", "result": f"Claude AI usage limit reached|{reset_s}"}
    with pytest.raises(ProviderError) as e:
        prov.parse(stream(INIT, err), "", 1)
    assert e.value.rate_limited and prov.cooldown_until_ms == reset_s * 1000


def test_no_result_never_quotes_events(prov):
    """A log that ends without a result (a crash) is an error, but a rate_limit_event saying "allowed" in it must not
    put the provider on a usage-limit cooldown."""
    with pytest.raises(ProviderError) as e:
        prov.parse(stream(INIT, RATE), "", 1)
    assert not e.value.rate_limited and prov.cooldown_until_ms == 0 and "without a result" in str(e.value)


@pytest.mark.skipif(not (FIX / "claude_code_stream_json_result.jsonl").exists(),
                    reason="real capture is added from the Phase 3 live call (last_stream_json.jsonl, redacted)")
def test_parse_real_stream_json_capture(prov):
    r = prov.parse((FIX / "claude_code_stream_json_result.jsonl").read_text(encoding="utf-8"), "", 0)
    assert r.text and r.input_tokens > 0 and r.output_tokens > 0 and r.extra["num_turns"] == 1
    # the Phase 3 live call (6 images): the numbers the ledger recorded, and an answer the contract accepts
    assert (r.input_tokens, r.output_tokens, r.model) == (24864, 2608, "claude-sonnet-5")
    from tradingsystem.ai.contract import Recommendation
    from tradingsystem.ai.providers.base import extract_json
    rec = Recommendation.model_validate(extract_json(r.text))
    assert rec.decision.value == "NO_TRADE" and rec.position_actions == []


def test_input_rejected_rules(monkeypatch):
    assert cc.input_rejected("", "boom", 1.0)                          # fast exit, no model turn
    assert not cc.input_rejected("", "boom", 30.0)                     # slow: may have reached the API
    # a known failure or a clean exit is not a shape problem: it goes through the normal error handling
    assert not cc.input_rejected("", "boom", 1.0, 0)
    assert not cc.input_rejected("", "Not logged in · Please run /login", 1.0, 1)
    assert not cc.input_rejected("", "Claude AI usage limit reached|1790000000", 1.0, 1)
    assert cc.input_rejected("", "boom", 1.0, 1)
    assert cc.input_rejected("", PARSER_REJECT[1].decode(), 30.0)      # slow, but the CLI says it could not read it
    assert not cc.input_rejected(stream(INIT, ASSISTANT), "", 1.0)     # a model turn started
    assert not cc.input_rejected(stream(INIT, RESULT), "", 1.0)


# ------------------------------------------------------------------ the image call and the shape probe
def test_image_call_first_shape_accepted_and_recorded(prov, cli):
    cli.script = [(OK_STREAM, b"", 0)]
    res = asyncio.run(prov.generate(system="SYS", user="u", schema=SCHEMA, images=IMGS))
    assert res.data == {"x": 1} and res.extra["images"] == 2 and res.extra["image_tokens_est"] == 768
    assert "--input-format" in cli.args[0] and "--json-schema" not in cli.args[0]
    assert cli.systems[0].startswith("SYS") and "JSON Schema" in cli.systems[0]
    assert sent_line(cli.procs[0])["message"]["content"][0] == {"type": "text", "text": "u"}
    caps = json.loads((prov.workdir / cc.CAPS_FILE).read_text(encoding="utf-8"))
    assert caps["stream_json_user_shape"] == "message" and caps["cli_version"] == "2.1.282"
    assert (prov.workdir / cc.LAST_STREAM).read_text(encoding="utf-8") == OK_STREAM
    assert not list(prov.workdir.glob("system_*"))


def test_probe_uses_second_shape_persists_it_and_skips_the_probe_next_time(prov, cli):
    cli.script = [PARSER_REJECT, (OK_STREAM, b"", 0)]
    res = asyncio.run(prov.generate(system="SYS", user="u", schema=SCHEMA, images=IMGS))
    assert res.data == {"x": 1} and len(cli.procs) == 2
    first, second = sent_line(cli.procs[0]), sent_line(cli.procs[1])
    assert "message" in first and "message" not in second and second["content"][0]["text"] == "u"
    caps = json.loads((prov.workdir / cc.CAPS_FILE).read_text(encoding="utf-8"))
    assert caps["stream_json_user_shape"] == "content" and caps["cli_version"] == "2.1.282"
    assert not list(prov.workdir.glob(f"{cc.CAPS_FILE}.*.tmp"))       # atomic write left no temp file

    cli.script = [(OK_STREAM, b"", 0)]                                  # next call: the recorded shape, one start
    asyncio.run(prov.generate(system="SYS", user="u2", schema=SCHEMA, images=IMGS))
    assert len(cli.procs) == 3 and sent_line(cli.procs[2])["content"][0]["text"] == "u2"


def test_recorded_shape_refused_after_a_cli_update_switches_back(prov, cli):
    cc.write_capabilities(prov.workdir, {"stream_json_user_shape": "content", "cli_version": "2.1.200"})
    cli.script = [PARSER_REJECT, (OK_STREAM, b"", 0)]
    asyncio.run(prov.generate(system="SYS", user="u", schema=SCHEMA, images=IMGS))
    assert "content" in sent_line(cli.procs[0]) and "message" in sent_line(cli.procs[1])
    assert cc.read_capabilities(prov.workdir)["stream_json_user_shape"] == "message"


def test_both_shapes_refused_disables_images_but_not_text(prov, cli):
    cli.script = [PARSER_REJECT, PARSER_REJECT]
    with pytest.raises(ProviderError) as e:
        asyncio.run(prov.generate(system="SYS", user="u", schema=SCHEMA, images=IMGS))
    assert CHARTS_DISABLED in str(e.value) and not e.value.retryable and len(cli.procs) == 2
    assert not (prov.workdir / cc.CAPS_FILE).exists()
    with pytest.raises(ProviderError, match=CHARTS_DISABLED):          # at once, no third CLI start
        asyncio.run(prov.generate(system="SYS", user="u", schema=SCHEMA, images=IMGS))
    assert len(cli.procs) == 2
    cli.script = [(json.dumps(RESULT), b"", 0)]                          # text calls are unaffected
    res = asyncio.run(prov.generate(system="SYS", user="u", schema=SCHEMA))
    assert res.data == {"x": 1} and "--input-format" not in cli.args[2] and res.extra["images"] == 0
    assert cli.procs[2].stdin == b"u"


def test_slow_failure_is_not_a_shape_rejection(prov, cli, monkeypatch):
    monkeypatch.setattr(cc, "SHAPE_REJECT_WINDOW_S", -1.0)               # every exit counts as slow
    cli.script = [(b"", b"Error: something else broke", 1)]
    with pytest.raises(ProviderError) as e:
        asyncio.run(prov.generate(system="SYS", user="u", schema=SCHEMA, images=IMGS))
    assert CHARTS_DISABLED not in str(e.value) and "something else broke" in str(e.value) and len(cli.procs) == 1


def test_error_result_in_stream_is_not_probed(prov, cli):
    err = {**RESULT, "is_error": True, "result": "API Error: 529 Overloaded", "api_error_status": 529}
    cli.script = [(stream(INIT, err), b"", 1)]
    with pytest.raises(ProviderError) as e:
        asyncio.run(prov.generate(system="SYS", user="u", schema=SCHEMA, images=IMGS))
    assert e.value.retryable and len(cli.procs) == 1


# ------------------------------------------------------------------ repair loop and the ledger
class ImageScripted(LLMProvider):
    supports_images = True

    def __init__(self, outputs):
        super().__init__("claude_code", AIProviderCfg(kind="claude_code", model="sonnet", free_tier=True), "sonnet",
                         None)
        self.outputs, self.calls = list(outputs), []

    async def _call(self, system, user, schema, schema_name, max_output_tokens, *, images=None):
        self.calls.append((user, images))
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return LLMResult(self.name, self.model, text="", data=out, input_tokens=1000, output_tokens=500)


class TextOnly(LLMProvider):
    def __init__(self, outputs):
        super().__init__("gemini", AIProviderCfg(kind="gemini", model="g", free_tier=True), "g", "k")
        self.outputs, self.calls = list(outputs), []

    async def _call(self, system, user, schema, schema_name, max_output_tokens):   # the five-argument form
        self.calls.append(user)
        return LLMResult(self.name, self.model, text="", data=self.outputs.pop(0), input_tokens=10, output_tokens=5)


class SpyGovernor(CostGovernor):
    def __init__(self, usage):
        super().__init__(AIBudgetCfg(), usage)
        self.estimates = []

    def check(self, provider, est_input_tokens, est_output_tokens):
        self.estimates.append(est_input_tokens)


@pytest.fixture
def usage(tmp_path):
    u = UsageStore(tmp_path / "ai_usage.db")
    yield u
    u.close()


def ledger(usage):
    return usage._con.execute("SELECT role, images, image_tokens_est, ok FROM ai_usage ORDER BY id").fetchall()


def run(provider, usage, gov=None, **kw):
    return asyncio.run(generate_validated(provider, Recommendation, system="s", user="u",
                                          limiter=RateLimiter(provider.name, provider.cfg, usage),
                                          governor=gov or CostGovernor(AIBudgetCfg(), usage), usage=usage,
                                          purpose="agent_per_pair", pair="BTCUSDT", **kw))


def test_repair_sends_images_on_the_first_attempt_only(usage):
    bad = copy.deepcopy(BASE)
    bad["stop_loss"] = None
    p = ImageScripted([bad, copy.deepcopy(BASE)])
    gov = SpyGovernor(usage)
    g = run(p, usage, gov, images=IMGS, role="decision", est_input_tokens=5000)
    assert g.ok and g.images_sent == 2 and g.charts_dropped is None
    assert p.calls[0][1] == IMGS and p.calls[1][1] is None
    assert CHARTS_SEEN_NOTE in p.calls[1][0] and "rejected by the validator" in p.calls[1][0]
    assert ledger(usage) == [("decision", 2, 768, 0), ("decision", 0, 0, 1)]
    assert gov.estimates == [5000 + 768, 5000]


def test_retry_after_a_transient_error_keeps_the_images(usage, monkeypatch):
    async def no_wait(_s):
        return None
    monkeypatch.setattr(repair_mod.asyncio, "sleep", no_wait)
    p = ImageScripted([ProviderError("claude_code: API Error: 529 Overloaded", retryable=True), copy.deepcopy(BASE)])
    g = run(p, usage, images=IMGS, role="decision")
    assert g.ok and p.calls[0][1] == IMGS and p.calls[1][1] == IMGS
    assert ledger(usage) == [("decision", 2, 768, 0), ("decision", 2, 768, 1)]


def test_charts_disabled_resends_the_attempt_as_text(usage):
    p = ImageScripted([ProviderError(f"claude_code: {CHARTS_DISABLED} (message: …; content: …)", retryable=False),
                       copy.deepcopy(BASE)])
    g = run(p, usage, images=IMGS, role="escalation")
    assert g.ok and CHARTS_DISABLED in g.charts_dropped and g.images_sent == 0
    assert p.calls[1][1] is None and p.calls[1][0] == "u" + NO_CHARTS_NOTE
    assert ledger(usage) == [("escalation", 0, 0, 1)]                  # the parser refusal made no API request


def test_text_only_provider_drops_images_with_one_warning(usage, caplog):
    p = TextOnly([copy.deepcopy(BASE), {"x": 1}])
    with caplog.at_level(logging.WARNING, logger="tradingsystem.ai.providers.base"):
        g = run(p, usage, images=IMGS, role="decision")
        res = asyncio.run(p.generate(system="s", user="again", images=IMGS))
    assert g.ok and g.images_sent == 0 and res.extra == {"images": 0, "image_tokens_est": 0}
    assert ledger(usage) == [("decision", 0, 0, 1)]
    # the text-only model is told that no chart came with the message (it must not read "Attached: …" as seen)
    assert p.calls[0] == "u" + NO_CHARTS_NOTE and "does not take images" in g.charts_dropped
    assert len([r for r in caplog.records if "dropped" in r.getMessage()]) == 1


def test_image_support_flags():
    assert cc.ClaudeCodeProvider.supports_images and AnthropicProvider.supports_images
    assert not gm.GeminiProvider.supports_images and not oc.OpenAIChatProvider.supports_images


def test_anthropic_sends_image_blocks():
    cfg = AIProviderCfg(kind="anthropic", model="claude-sonnet-5", free_tier=True)
    p = AnthropicProvider("anthropic", cfg, "claude-sonnet-5", "test-key-not-used")
    sent = {}

    async def create(**kw):
        sent.update(kw)
        return SimpleNamespace(stop_reason="end_turn", model="claude-sonnet-5", content=[
            SimpleNamespace(type="text", text=ANSWER)], usage=SimpleNamespace(
            input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0))
    p.client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    res = asyncio.run(p.generate(system="s", user="u", images=IMGS))
    blocks = sent["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["text", "text", "image", "text", "image"] and res.extra["images"] == 2
    asyncio.run(p.generate(system="s", user="plain"))
    assert sent["messages"][0]["content"] == "plain"


# ------------------------------------------------------------------ per-role model and effort
def test_effort_override_reaches_the_cli_args(prov, cli):
    p = cc.ClaudeCodeProvider("claude_code", prov.cfg, "opus", "token", effort="high")
    a = p.build_args("sys.md", None)
    assert a[a.index("--effort") + 1] == "high" and a[a.index("--model") + 1] == "opus"
    assert prov.effort == "medium"                                      # no override: the configured effort
    none = cc.ClaudeCodeProvider("claude_code", prov.cfg.model_copy(update={"effort": None}), "sonnet", "token")
    assert "--effort" not in none.build_args("sys.md", None)
    p._auth_problem, p._auth_checked = None, time.monotonic()
    cli.script = [(OK_STREAM, b"", 0)]
    asyncio.run(p.generate(system="SYS", user="u", schema=SCHEMA, images=IMGS))
    assert cli.args[0][cli.args[0].index("--effort") + 1] == "high"


def test_make_provider_passes_effort_only_to_claude_code(monkeypatch):
    seen = {}

    class Recorder:
        def __init__(self, *a, **kw):
            seen[a[0]] = (a, kw)
    monkeypatch.setattr(cc, "ClaudeCodeProvider", Recorder)
    monkeypatch.setattr(gm, "GeminiProvider", Recorder)
    monkeypatch.setattr(oc, "OpenAIChatProvider", Recorder)
    s = load_settings(env_path=Path("nope.env"))
    make_provider(s, "claude_code", model="opus", effort="max")
    assert seen["claude_code"][0][2] == "opus" and seen["claude_code"][1] == {"effort": "max"}
    make_provider(s, "claude_code")
    assert seen["claude_code"][0][2] == s.provider_model("claude_code") and seen["claude_code"][1] == {"effort": None}
    make_provider(s, "gemini", model="gemini-x", effort="max")
    assert seen["gemini"][0][2] == "gemini-x" and seen["gemini"][1] == {}
    make_provider(s, "openai", effort="high")
    assert seen["openai"][1] == {}


def test_a_new_role_instance_adopts_the_verified_sign_in(monkeypatch):
    monkeypatch.setattr(cc, "find_cli", lambda configured: "claude.exe")
    cfg = AIProviderCfg(kind="claude_code", model="sonnet", free_tier=True)
    dec = cc.ClaudeCodeProvider("claude_code", cfg, "sonnet", None)
    esc = cc.ClaudeCodeProvider("claude_code", cfg, "opus", None, effort="high")
    assert dec.availability_pending and esc.availability_pending
    esc.adopt_sign_in(dec)                                             # nothing verified yet: still checking
    assert esc.availability_pending
    dec._auth_problem, dec._auth_ok_once = None, True                  # the decision instance's check passed
    esc.adopt_sign_in(dec)
    assert not esc.availability_pending and esc._auth_problem is None

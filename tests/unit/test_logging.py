import json
import logging

from tradingsystem.core.logsetup import setup_logging


def test_secrets_are_masked(tmp_path, monkeypatch):
    secret = "AIzaSyD-THIS-IS-A-FAKE-TEST-KEY-1234567890"
    other = "super-secret-password-value"
    monkeypatch.setenv("GOOGLE_API_KEY", secret)
    monkeypatch.setenv("MT5_DEMO_PASSWORD", other)
    log = setup_logging("test", logs_dir=tmp_path, console=False,
                        secret_env_names=["GOOGLE_API_KEY", "MT5_DEMO_PASSWORD"])
    log.info("calling with key=%s and pw=%s", secret, other, extra={"ctx": {"auth": f"Bearer {other}"}})
    try:
        raise RuntimeError(f"failed with {secret}")
    except RuntimeError:
        log.exception("boom")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (tmp_path / "test.jsonl").read_text(encoding="utf-8")
    assert secret not in text and other not in text
    first = json.loads(text.splitlines()[0])
    assert first["msg"].startswith("calling with key=AIza****")
    assert first["ts"].endswith("+00:00")


def test_pattern_masking_without_env(tmp_path):
    log = setup_logging("test2", logs_dir=tmp_path, console=False)
    log.warning("leaked sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    for h in logging.getLogger().handlers:
        h.flush()
    assert "abcdefghijklmnop" not in (tmp_path / "test2.jsonl").read_text(encoding="utf-8")

"""Prompt registry (Phase 4 §3.8 component 1): every render knows the versions of the files it used, and
``register_versions`` records each new system prompt once in ``prompt_versions`` — never failing a cycle."""
import json
import sqlite3

import pytest

from tests.unit.test_prompts import SYS, USER
from tradingsystem.ai.prompts import ROLE_FILES, RenderedPrompt, library_hash, register_versions, render, versions
from tradingsystem.ai.store import DecisionStore


def expected_files(role, own=()):
    sys_file, user_file = ROLE_FILES[role]
    shared = [f"shared/{n}" for v, n in (("persona", "trader_persona"), ("core_rules", "core_rules"),
                                         ("payload_legend", "payload_legend")) if v not in own]
    return {*shared, sys_file.removesuffix(".md"), user_file.removesuffix(".md")}


@pytest.mark.parametrize("role", list(ROLE_FILES))
def test_render_carries_the_versions_of_the_files_it_used(role):
    pr = render(role, SYS, USER)
    lib = versions()
    assert set(pr.versions) == expected_files(role)
    assert pr.versions == {k: lib[k] for k in pr.versions}


def test_a_caller_supplied_block_is_not_counted_as_the_shared_file():
    pr = render("agent_per_pair", {**SYS, "core_rules": "(own rules)"}, USER)
    assert "shared/core_rules" not in pr.versions and "(own rules)" in pr.system
    assert set(pr.versions) == expected_files("agent_per_pair", own=("core_rules",))


def test_versions_do_not_change_identity_or_hash():
    a, b = render("agent_per_pair", SYS, USER), render("agent_per_pair", SYS, USER)
    assert a == b and hash(a) == hash(b)                              # still a frozen, hashable value
    assert RenderedPrompt(a.role, a.system, a.user) == a
    assert a.prompt_hash == RenderedPrompt("x", a.system, "").prompt_hash


def test_register_versions_records_each_system_prompt_once(tmp_path, monkeypatch):
    st = DecisionStore(tmp_path / "app.db", "cfg")
    pr = render("agent_per_pair", SYS, USER)
    writes = []
    orig = st.register_prompt
    monkeypatch.setattr(st, "register_prompt", lambda *a: (writes.append(a), orig(*a))[1])
    register_versions(st, pr, "agent_per_pair")
    register_versions(st, render("agent_per_pair", SYS, {**USER, "payload": '{"x": 1}'}), "agent_per_pair")
    assert len(writes) == 1                                           # same system prompt → no second write
    row = sqlite3.connect(tmp_path / "app.db").execute(
        "SELECT prompt_hash, role, library_hash, versions, git_sha, first_seen_ms FROM prompt_versions").fetchall()
    assert len(row) == 1
    h, role, lib, v, sha, first = row[0]
    assert (h, role, lib, sha) == (pr.prompt_hash, "agent_per_pair", library_hash(), st.git_sha)
    assert json.loads(v) == pr.versions and first > 0
    other = render("risk_reviewer", SYS, USER)
    register_versions(st, other, "risk_reviewer", lib_hash="feedfacefeedface")
    assert sqlite3.connect(tmp_path / "app.db").execute(
        "SELECT library_hash FROM prompt_versions WHERE prompt_hash=?", (other.prompt_hash,)).fetchone()[0] == \
        "feedfacefeedface"
    st.close()


def test_first_sighting_is_kept_across_processes(tmp_path):
    a, b = DecisionStore(tmp_path / "app.db", "cfg"), DecisionStore(tmp_path / "app.db", "cfg")   # engine + executor
    pr = render("agent_per_pair", SYS, USER)
    lib = library_hash()
    register_versions(a, pr, "agent_per_pair")
    first = sqlite3.connect(tmp_path / "app.db").execute("SELECT first_seen_ms FROM prompt_versions").fetchone()[0]
    b.register_prompt(pr.prompt_hash, "agent_per_pair", lib, {"x": 1})
    rows = sqlite3.connect(tmp_path / "app.db").execute(
        "SELECT first_seen_ms, library_hash, versions FROM prompt_versions").fetchall()
    assert rows == [(first, lib, json.dumps(pr.versions, sort_keys=True))]
    assert a.prompt_known(pr.prompt_hash, lib) and b.prompt_known(pr.prompt_hash, lib)
    assert not a.prompt_known(pr.prompt_hash, "0" * 16)
    a.close()
    b.close()


def test_an_instructions_only_edit_is_registered_as_a_new_version(tmp_path, monkeypatch):
    """Editing only the user template keeps the system prompt (and its hash) but changes the library: the registry
    gets a second row with the new versions, and each (prompt, library) is written once per process."""
    import shutil
    from tradingsystem.ai import prompts
    lib_dir = tmp_path / "prompts"
    shutil.copytree(prompts.DIR, lib_dir, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    monkeypatch.setattr(prompts, "DIR", lib_dir)
    st = DecisionStore(tmp_path / "app.db", "cfg")
    writes = []
    orig = st.register_prompt
    monkeypatch.setattr(st, "register_prompt", lambda *a: (writes.append(a), orig(*a))[1])
    old = render("agent_per_pair", SYS, USER)
    old_lib = library_hash()
    register_versions(st, old, "agent_per_pair")
    f = lib_dir / "agent_per_pair" / "instructions.md"
    v = old.versions["agent_per_pair/instructions"]
    f.write_text(f.read_text(encoding="utf-8").replace(f"· version {v} -->", f"· version {v + 1} -->", 1)
                 + "\nOne more instruction line.\n", encoding="utf-8")
    new = render("agent_per_pair", SYS, USER)
    new_lib = library_hash()
    assert new.prompt_hash == old.prompt_hash and new_lib != old_lib            # the system prompt did not change
    assert new.versions["agent_per_pair/instructions"] == v + 1
    register_versions(st, new, "agent_per_pair")
    register_versions(st, new, "agent_per_pair")                               # known now: no third write
    register_versions(st, old, "agent_per_pair", lib_hash=old_lib)             # known too
    assert len(writes) == 2
    rows = sqlite3.connect(tmp_path / "app.db").execute(
        "SELECT prompt_hash, library_hash, versions FROM prompt_versions ORDER BY first_seen_ms, rowid").fetchall()
    assert [(h, lib, json.loads(vs)["agent_per_pair/instructions"]) for h, lib, vs in rows] == [
        (old.prompt_hash, old_lib, v), (old.prompt_hash, new_lib, v + 1)]
    st.close()


def test_register_versions_never_raises(tmp_path):
    pr = render("agent_per_pair", SYS, USER)
    register_versions(None, pr, "agent_per_pair")                     # an orchestrator without a store (tests)

    class Broken:
        def prompt_known(self, h, lib):
            return False

        def register_prompt(self, *a):
            raise sqlite3.OperationalError("database is locked")

    register_versions(Broken(), pr, "agent_per_pair")
    st = DecisionStore(tmp_path / "app.db", "cfg")
    st.close()                                                        # a closed connection → logged, not raised
    register_versions(st, pr, "agent_per_pair")

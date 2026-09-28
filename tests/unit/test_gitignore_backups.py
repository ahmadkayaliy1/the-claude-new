"""The backup folder is git-ignored as a whole (the run lock stays for good and a .zip.tmp exists while a zip is written):
a trailing comment on the same .gitignore line made the pattern match nothing, so the production checkout would show
'?? backups/' after the first backup and every review pack would say DIRTY (checkpoint-A review)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_every_file_of_the_backup_folder_is_ignored():
    paths = ["backups/backup.lock", "backups/20260928T033000Z.zip.tmp", "backups/20260928T033000Z.zip",
             "backups/.staging-1/app.db"]
    r = subprocess.run(["git", "check-ignore", "--no-index", *paths], cwd=ROOT, capture_output=True, text=True)
    assert sorted(r.stdout.split()) == sorted(paths), r.stdout + r.stderr

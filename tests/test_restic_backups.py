"""Machine backups (restic): the backup runs restic with your folders and its tag, a failure is told once, old
snapshots are thinned out only among its own, and the card holds: restic is the only program it may start."""

from __future__ import annotations

import pytest

RESTIC = """#!/bin/sh
echo "$@" >> @LOG@
echo "repo=$RESTIC_REPOSITORY pw=$RESTIC_PASSWORD_FILE" >> @LOG@
case "$1" in
  backup) [ -f "$FAIL" ] && { echo "Fatal: unable to open repository" >&2; exit 1; }
          echo '{"message_type":"status","percent_done":0.5}'
          echo '{"message_type":"summary","files_new":3,"files_changed":1,"data_added":2048,"snapshot_id":"abc123"}' ;;
  snapshots) echo '[{"time":"2026-10-20T02:30:00Z","short_id":"abc123","paths":["/data"]}]' ;;
esac
exit 0
"""


@pytest.fixture
def app(env, load_addon, fake_programs, tmp_path, monkeypatch):
    bin_ = fake_programs({"restic": RESTIC})
    load_addon("restic-backups", env.app)
    data = tmp_path / "my-files"
    data.mkdir()
    pw = tmp_path / "pw"
    pw.write_text("not-a-real-password\n")
    s = env.app.settings
    s.set("restic.repository", str(tmp_path / "repo"))
    s.set("restic.password_file", str(pw))
    s.set("restic.paths", [str(data)])
    monkeypatch.setenv("FAIL", str(tmp_path / "fail"))
    env.on("restic")
    return env.app, bin_ / "restic.log", data


def test_a_backup_runs_restic_with_the_folders_and_says_what_it_did(app, env):
    a, log, data = app
    ok, said = a.modules["restic"].backup("asked")
    assert ok and "3 new, 1 changed, 2.0 KB added" in said
    text = log.read_text()
    assert f"backup --json --tag kurooto {data}" in text and "pw=" in text and "not-a-real" not in text
    assert "Last good backup: 0 min ago" in env.say("/restic")
    assert "abc123" in env.say("/restic snapshots")


def test_a_failed_scheduled_backup_is_told_once_and_shows_in_health(app, env, tmp_path):
    a, _log, _ = app
    (tmp_path / "fail").write_text("x")
    m = a.modules["restic"]
    assert m.backup("scheduled")[0] is False
    assert m.backup("scheduled")[0] is False
    told = [t for t in env.fake.texts() if "restic backup didn't work" in t]
    assert len(told) == 1 and "unable to open repository" in told[0]
    assert m.health_check().probe(False)[0] is False


def test_thinning_out_touches_only_its_own_snapshots(app):
    a, log, _ = app
    a.settings.set("restic.keep", {"daily": 5, "weekly": 0, "monthly": 2})
    assert a.modules["restic"].prune()[0]
    assert "forget --prune --tag kurooto --keep-daily 5 --keep-monthly 2" in log.read_text()


def test_its_jobs_are_the_ones_on_its_card(app):
    a, _log, _ = app
    assert sorted(j.key for j in a.scheduler.jobs("restic:")) == ["restic:backup", "restic:check", "restic:prune"]


def test_it_may_start_restic_and_nothing_else(app, monkeypatch):
    from kurooto.core import fence, proc

    a, _log, _ = app
    m = a.modules["restic"]
    assert m.restic("version").ok
    mod = __import__(type(m).__module__, fromlist=["proc"])
    real = proc.run
    monkeypatch.setattr(mod.proc, "run", lambda args, **kw: real(["sh", "-c", "echo hi"], **kw))  # a shell instead
    with pytest.raises(fence.ProgramFenced):
        m.restic("version")

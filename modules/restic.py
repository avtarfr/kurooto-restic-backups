"""Machine backups with restic: the folders you name, backed up on a schedule into a restic repository you already
have (a drive, sftp, S3, B2, a rest-server: anything restic takes), with old snapshots thinned out and the repository
checked now and then. Kurooto tells you when a backup fails, and when the last good one is getting old.

Kurooto's own Backups feature zips Kurooto's settings and data; this backs up the rest of the computer.

restic does the work and reaches the repository itself: Kurooto never reads the password (restic reads the file you
name) and can't see what restic sends. It only starts `restic`, as its card says.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from kurooto.core import proc
from kurooto.core.config import register_defaults
from kurooto.core.lock import Lock, Timeout
from kurooto.core.state import jload, jsave
from kurooto.modules.base import Command, Incoming, Module

register_defaults(
    "restic",
    {
        "repository": "",  # where restic keeps the backups: a folder, sftp:host:/path, s3:..., b2:..., rest:...
        "password_file": "",  # a file holding the repository's password: restic reads it, Kurooto never does
        "paths": [],  # the folders to back up (full paths)
        "exclude_file": "",  # a restic exclude file, one pattern a line (empty: nothing left out)
        "when": "daily 02:30",  # when the backup runs (it waits for the charger on a laptop on battery)
        "keep": {"daily": 7, "weekly": 4, "monthly": 6},  # snapshots kept after thinning
        "prune": "weekly sun 04:00",  # when old snapshots are thinned out and their space freed
        "check": "weekly sat 05:00",  # when the repository is checked (restic check)
        "stale_days": 2,  # say so when the last good backup is older than this many days
        "timeout_h": 6,  # a backup that runs longer than this is stopped
    },
)

TAG = "kurooto"  # snapshots this add-on makes carry this tag; forget only ever touches those


def ago(ts: float | None) -> str:
    if not ts:
        return "never"
    s = max(0, time.time() - ts)
    if s < 3600:
        return f"{int(s // 60)} min ago"
    if s < 172800:
        return f"{int(s // 3600)} h ago"
    return f"{int(s // 86400)} days ago"


def size(n: float) -> str:
    for unit, step in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= step:
            return f"{n / step:.1f} {unit}"
    return f"{int(n)} B"


class Restic(Module):
    id = "restic"
    title = "Machine backups (restic)"
    description = "Backs up the folders you name with restic, thins out old snapshots and checks the repository."
    privacy = (
        "restic sends your backed-up files, encrypted, to the repository you name; Kurooto starts restic and reads "
        "its summary, never the password."
    )
    heavy = True  # on a laptop on battery, the backup waits for the charger
    processes = ("restic",)
    processes_intermittent = True

    def __init__(self, app):
        super().__init__(app)
        self.state_file = Path(app.paths.state) / "restic.json"
        self._busy = threading.Lock()

    # ----------------------------------------------------------------------------------------------- setup
    def exe(self) -> str | None:
        return proc.which("restic")

    def folders(self) -> list[Path]:
        raw = self.setting("paths") or []
        return [Path(x).expanduser() for x in (raw if isinstance(raw, list) else [raw]) if str(x).strip()]

    def ready(self):
        if not self.exe():
            return False, "restic isn't installed (your system's package manager has it)"
        if not str(self.setting("repository") or "").strip():
            return False, "name the restic repository (Set up)"
        pw = Path(str(self.setting("password_file") or "")).expanduser()
        if not str(self.setting("password_file") or "").strip() or not pw.is_file():
            return False, "name the file that holds the repository's password (Set up)"
        if not self.folders():
            return False, "name the folders to back up (Set up)"
        missing = [str(f) for f in self.folders() if not f.is_dir()]
        if missing:
            return False, f"no such folder: {missing[0]}"
        return True, ""

    def setup_form(self) -> dict:
        return {
            "intro": "Back up folders of this computer with restic into a repository you already made "
            "(`restic init`, or /restic init once these are set). Kurooto never reads the password: restic reads "
            "the file you name.",
            "links": [["restic's documentation", "https://restic.readthedocs.io/"]],
            "fields": [
                {"name": "repository", "label": "Repository", "type": "text", "placeholder": "/mnt/backup/restic"},
                {"name": "password_file", "label": "Password file", "type": "text", "placeholder": "full path"},
                {"name": "paths", "label": "Folders to back up (one a line)", "type": "textarea"},
            ],
        }

    def configure(self, values: dict) -> dict:
        repo = str(values.get("repository") or "").strip()
        pw = str(values.get("password_file") or "").strip()
        paths = [x.strip() for x in str(values.get("paths") or "").splitlines() if x.strip()]
        if not repo or not pw or not paths:
            return {"ok": False, "message": "Fill in the repository, the password file and at least one folder."}
        if not Path(pw).expanduser().is_file():
            return {"ok": False, "message": f"There's no file {pw}."}
        bad = [x for x in paths if not Path(x).expanduser().is_absolute() or not Path(x).expanduser().is_dir()]
        if bad:
            return {"ok": False, "message": f"Not a folder's full path: {bad[0]}."}
        s = self.app.settings
        s.set("restic.repository", repo)
        s.set("restic.password_file", pw)
        s.set("restic.paths", paths)
        return {"ok": True, "message": "Saved. /restic now makes the first backup."}

    def start(self):
        self.every("backup", self.setting("when") or "daily 02:30", lambda p: self.backup("scheduled"))
        if self.setting("prune"):
            self.every("prune", self.setting("prune"), lambda p: self.prune())
        if self.setting("check"):
            self.every("check", self.setting("check"), lambda p: self.check())

    # ------------------------------------------------------------------------------------------------ work
    def _env(self) -> dict:
        return {
            **os.environ,
            "RESTIC_REPOSITORY": str(self.setting("repository") or ""),
            "RESTIC_PASSWORD_FILE": str(Path(str(self.setting("password_file") or "")).expanduser()),
            "RESTIC_PROGRESS_FPS": "0.016",
        }

    def restic(self, *args: str, timeout: float = 600) -> proc.Result:
        exe = self.exe()
        if not exe:
            return proc.Result(127, "", "restic isn't installed")
        return proc.run([exe, *args], env=self._env(), timeout=timeout)

    def _rec(self) -> dict:
        return jload(self.state_file, {}) or {}

    def _save(self, **kw) -> dict:
        rec = {**self._rec(), **kw}
        jsave(self.state_file, rec)
        return rec

    def backup(self, why: str = "asked") -> tuple[bool, str]:
        ok, need = self.ready()
        if not ok:
            return False, need
        try:
            lock = Lock(Path(self.app.paths.state), "restic", timeout=0)
            lock.acquire()
        except Timeout:
            return False, "a restic run is going already"
        try:
            args = ["backup", "--json", "--tag", TAG]
            ex = str(self.setting("exclude_file") or "").strip()
            if ex:
                args += ["--exclude-file", str(Path(ex).expanduser())]
            args += [str(f) for f in self.folders()]
            self._save(last_try=time.time())
            r = self.restic(*args, timeout=float(self.setting("timeout_h") or 6) * 3600)
        finally:
            lock.release()
        summary = {}
        for ln in (r.out or "").splitlines():
            try:
                d = json.loads(ln)
            except ValueError:
                continue
            if isinstance(d, dict) and d.get("message_type") == "summary":
                summary = d
        if r.code == 0 or (r.code == 3 and summary):  # 3: some files couldn't be read, the snapshot was made
            said = (
                f"{summary.get('files_new', 0)} new, {summary.get('files_changed', 0)} changed, "
                f"{size(summary.get('data_added', 0))} added"
                + (" (some files couldn't be read)" if r.code == 3 else "")
            )
            self._save(last_ok=time.time(), last_error="", said=said, snapshot=summary.get("snapshot_id", ""))
            self.app.events.emit("restic_backup", ok=True, why=why)
            return True, f"Backed up: {said}."
        err = (r.err or r.out or "restic failed").strip().splitlines()
        err_line = (err[-1] if err else "restic failed")[:200]
        rec = self._save(last_error=err_line)
        self.app.events.emit("restic_backup", ok=False, why=why, error=err_line)
        if why != "asked" and time.time() - float(rec.get("told") or 0) > 20 * 3600:  # told once a day at most
            self.app.notify(f"The restic backup didn't work: {err_line}", kind="info", why="restic.failed")
            self._save(told=time.time())
        return False, f"The backup didn't work: {err_line}"

    def prune(self) -> tuple[bool, str]:
        keep = self.setting("keep") or {}
        args = ["forget", "--prune", "--tag", TAG]
        for k in ("daily", "weekly", "monthly"):
            n = int(keep.get(k) or 0) if isinstance(keep, dict) else 0
            if n > 0:
                args += [f"--keep-{k}", str(n)]
        if len(args) == 4:
            return False, "nothing to keep is set (restic.keep), so nothing was thinned out"
        r = self.restic(*args, timeout=3 * 3600)
        self._save(last_prune=time.time(), prune_ok=r.ok)
        return r.ok, "Old snapshots thinned out." if r.ok else f"Thinning out didn't work: {(r.err or '')[:200]}"

    def check(self) -> tuple[bool, str]:
        r = self.restic("check", timeout=3 * 3600)
        self._save(last_check=time.time(), check_ok=r.ok)
        if not r.ok:
            self.app.notify(f"restic check found a problem: {(r.err or r.out)[:200]}", kind="info", why="restic.failed")
        return r.ok, "The repository checks out." if r.ok else "restic check found a problem (see /restic)."

    def snapshots(self) -> list[dict]:
        r = self.restic("snapshots", "--json", "--tag", TAG, timeout=120)
        try:
            rows = json.loads(r.out or "[]") if r.ok else []
        except ValueError:
            rows = []
        return rows if isinstance(rows, list) else []

    # ------------------------------------------------------------------------------------------- telling
    def stale(self) -> bool:
        last = self._rec().get("last_ok")
        return not last or time.time() - float(last) > float(self.setting("stale_days") or 2) * 86400

    def status_text(self) -> str:
        rec = self._rec()
        lines = [f"Last good backup: {ago(rec.get('last_ok'))}" + (f" ({rec['said']})" if rec.get("said") else "")]
        if rec.get("last_error") and float(rec.get("last_try") or 0) >= float(rec.get("last_ok") or 0):
            lines.append(f"Last try failed: {rec['last_error']}")
        lines.append(f"Thinned out: {ago(rec.get('last_prune'))}; checked: {ago(rec.get('last_check'))}")
        lines.append(f"Next: {self.setting('when')} · {len(self.folders())} folder(s) → {self.setting('repository')}")
        return "\n".join(lines)

    def status_lines(self) -> list[str]:
        return [f"restic: last good backup {ago(self._rec().get('last_ok'))}"]

    def health_check(self):
        from kurooto.core.health import Check

        def probe(net: bool):
            ok, why = self.ready()
            if not ok:
                return False, why
            if self.stale():
                return False, f"the last good backup was {ago(self._rec().get('last_ok'))}"
            return True, f"last good backup {ago(self._rec().get('last_ok'))}"

        return Check("restic", "Machine backups", module=self.id, probe=probe, help="/restic now makes one")

    def commands(self):
        return [Command("restic", "machine backups: status, now, snapshots, check, init", self._cmd, "[now|...]")]

    def _cmd(self, msg: Incoming, arg: str) -> str:
        a = arg.strip().lower()
        if a in ("", "status"):
            return self.status_text()
        if a == "now":
            if not self._busy.acquire(blocking=False):
                return "A backup is going already."

            def work():
                try:
                    ok, said = self.backup("asked")
                finally:
                    self._busy.release()
                self.app.notify(said, kind="result", why="asked")

            threading.Thread(target=work, name="restic", daemon=True).start()
            return "Backing up now; I'll tell you when it's done."
        if a == "snapshots":
            rows = self.snapshots()[-8:]
            if not rows:
                return "No snapshots from Kurooto yet (or restic couldn't list them)."
            out = []
            for s in reversed(rows):
                when = str(s.get("time", ""))[:16].replace("T", " ")
                out.append(
                    f"{when} · {str(s.get('short_id') or s.get('id', ''))[:8]} · {', '.join(s.get('paths') or [])}"
                )
            return "\n".join(out)
        if a == "check":
            return self.check()[1]
        if a == "init":  # a new repository where you named one: only when you type it
            pw = Path(str(self.setting("password_file") or "")).expanduser()
            if not self.exe() or not str(self.setting("repository") or "").strip() or not pw.is_file():
                return "Set up the repository and the password file first (and install restic)."
            r = self.restic("init", timeout=300)
            return "Made the repository." if r.ok else f"restic init didn't work: {(r.err or r.out)[:200]}"
        return "/restic, /restic now, /restic snapshots, /restic check, /restic init"

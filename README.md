# Machine backups (restic), a Kurooto add-on

Backs up the folders you name with [restic](https://restic.net) on a schedule, thins out old snapshots, checks the
repository now and then, and tells you when a backup fails or the last good one is getting old. Kurooto's own
Backups feature zips Kurooto's settings and data; this backs up the rest of your computer.

## Add it

```
kurooto addons add https://github.com/avtarfr/kurooto-restic-backups --ref v1.0.0
```

or Dashboard → Features → **Add-ons by link**. You see its card first (below); nothing is added until you say yes.
Then Features → **Machine backups (restic)** → **Set up** (repository, password file, folders) → **Turn on**.

You need restic installed and a repository made once (`restic init`, or `/restic init` after Set up).

## Its card

| | |
|---|---|
| Adds | the feature `restic`; the command `/restic`; jobs `restic:backup`, `restic:prune`, `restic:check` |
| May reach | nothing on the internet itself |
| May start | `restic` (goes online by itself to reach your repository: Kurooto can't see what it sends) |
| Runs code | yes: one Python file, run inside Kurooto |

## Using it

| Command | Does |
|---|---|
| `/restic` | the last good backup, the last failure, when it last thinned out and checked |
| `/restic now` | backs up now; you're told when it's done |
| `/restic snapshots` | the last snapshots it made |
| `/restic check` | checks the repository now |
| `/restic init` | makes the repository you named (only when you type it) |

On a laptop on battery, the backup waits for the charger. A failed scheduled backup is told once a day at most; the
health check (dashboard, `kurooto doctor`) says when the last good backup is older than `restic.stale_days`.

## Settings (Dashboard → Settings → restic)

| Setting | Default | Effect |
|---|---|---|
| `restic.repository` | empty | where restic keeps the backups: a folder, `sftp:host:/path`, `s3:...`, `b2:...`, `rest:...` |
| `restic.password_file` | empty | a file holding the repository's password; restic reads it, Kurooto never does |
| `restic.paths` | `[]` | the folders to back up (full paths) |
| `restic.exclude_file` | empty | a restic exclude file, one pattern a line |
| `restic.when` | `daily 02:30` | when the backup runs |
| `restic.keep` | daily 7, weekly 4, monthly 6 | snapshots kept after thinning |
| `restic.prune` | `weekly sun 04:00` | when old snapshots are thinned out (only snapshots tagged `kurooto`) |
| `restic.check` | `weekly sat 05:00` | when the repository is checked |
| `restic.stale_days` | 2 | the health check fails past this many days without a good backup |
| `restic.timeout_h` | 6 | a backup running longer is stopped |

## What leaves your computer

restic sends your backed-up files, encrypted with your repository's password, to the repository you named. Kurooto
only starts restic and reads its summary; `/egress` counts each run as handed to restic.

## Remove it

Features → turn it off, then `kurooto addons remove restic-backups` (`kurooto addons undo restic-backups` puts it
back). Your repository and its snapshots stay as they are.

## Tests

Inside a Kurooto checkout, with this folder at `addons/restic-backups/`: `uv run pytest addons/restic-backups`.
A fake restic stands in; nothing is backed up.

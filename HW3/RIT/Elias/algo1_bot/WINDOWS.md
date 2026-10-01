# Running the bot on the Windows RIT machine

The heat runs from the Windows RIT app on a shared server. The Mac's `rit_competition` folder is
redirected into the Remote Desktop session as `\\tsclient\rit_competition`, and this repo is
cloned there as `\\tsclient\rit_competition\sim02_algo1`. **Do not run Python from that share**:
it is a network drive over RDP (slow imports, slow parquet writes, file locks). Copy to the local
disk, run there, copy `runs\` back if you want the Mac to keep them.

Everything below is PowerShell. Nothing here needs admin rights. **If your window is the Command
Prompt** (prompt looks like `C:\Users\e.roubache>`), `$env:USERPROFILE` is not expanded: write the
home folder literally (`C:\Users\e.roubache`), reopen the window after installing `uv`, and use
`type nul > runs\KILL` for the kill switch. Everything else is identical in both shells.

## 1. Copy the repo to the local disk (once, and again after each `git pull` on the Mac side)

Every block below starts from the repo folder: `cd $env:USERPROFILE\algo1` (cmd: `cd C:\Users\e.roubache\algo1`).

```powershell
robocopy \\tsclient\rit_competition\sim02_algo1 $env:USERPROFILE\algo1 /E /R:2 /W:2 /XD .venv .venv.nosync runs __pycache__ .pytest_cache .git /XF secrets.env
cd $env:USERPROFILE\algo1
```
`D:` and `C:\` root are not writable for a student account on the shared machine (robocopy
"ERROR 5 Access is denied", 10 Sep 2026); the user profile always is. Fallbacks:
`$env:LOCALAPPDATA\algo1`, or `$env:TEMP\algo1` for one session only. `/R:2 /W:2` fails fast
instead of robocopy's default million retries; `/XD` skips the Mac venv.

## 2. Python + venv without admin

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   # user-level uv
uv venv .venv --python 3.12                     # uv downloads a private CPython if none is installed
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python.exe -m pytest -q           # 63 tests should pass
```
If `uv` is blocked by policy: install Python 3.12 from python.org with "Install for me only",
then `python -m venv .venv` and `.venv\Scripts\python.exe -m pip install -r requirements.txt`.

## 3. Secrets

Create `$env:USERPROFILE\algo1\secrets.env` **on the Windows machine** (never on the share):
```
RIT_API_KEY=<key from the API icon in the RIT client status bar>
RIT_HOST=localhost
RIT_PORT=9999
```
`RIT_PORT` must match the port shown in the client's API dialog. Alternative: `$env:RIT_API_KEY="..."`
for the current PowerShell only.

## 4. Commands (replace `.venv.nosync/bin/python` from CLAUDE.md with `.venv\Scripts\python.exe`)

```powershell
cd $env:USERPROFILE\algo1          # cmd: cd C:\Users\e.roubache\algo1
$py = ".\.venv\Scripts\python.exe"
& $py -m algo1 probe                               # HTTP facts, lanes, GET limit
& $py -m algo1 verify --trade                      # ALGO1 checks at tick 0 (100-share pair, self-trade)
& $py -m algo1 record --name tape --seconds 30 ; & $py -m algo1 analyze --name tape
& $py -m algo1 run --dry --name d1 --max-seconds 60
& $py -m algo1 run --name p1 --max-seconds 120     # pairs only
& $py -m algo1 run --name q1 --quotes              # full
& $py -m algo1 report --name q1
```
Second PowerShell window for the monitor:
```powershell
cd $env:USERPROFILE\algo1 ; .\.venv\Scripts\python.exe -m algo1 monitor --name q1 --plots
```
Kill switch (third window or the monitor's): `New-Item -ItemType File runs\KILL` — the engine
cancels everything, flattens and exits. `Ctrl-C` in the engine window does the same.

## 5. Windows notes

- Shared memory: `multiprocessing.shared_memory` uses named file mappings on Windows; the
  monitor must start while the engine runs (the block disappears when the engine exits, which
  is fine — `report` reads the parquet dumps).
- Timer: `time.sleep` granularity is ~15 ms on Windows unless something raised the resolution.
  The engine never sleeps in the loop; the mock steps by elapsed time, so it is unaffected.
- Firewall prompt for `python.exe` on first run: allow on private networks (localhost only).
- Antivirus can slow parquet writes at shutdown; that is after the case ends.
- Paths: the code uses `os.path.join` and forward-slash defaults (`runs/KILL`), both fine on Windows.
- `matplotlib` uses the default TkAgg backend on Windows; if the plots window does not appear,
  run the monitor without `--plots`.

## 6. Bringing results back to the Mac (do this after every demo session)

```powershell
cd $env:USERPROFILE\algo1
robocopy $env:USERPROFILE\algo1\runs \\tsclient\rit_competition\sim02_algo1\runs /E /R:2 /W:2
```
(cmd: `robocopy C:\Users\e.roubache\algo1\runs \\tsclient\rit_competition\sim02_algo1\runs /E /R:2 /W:2`)
On the Mac side the probe / analysis JSONs and run summaries are then moved into `demo/<date>/`
and committed; recordings stay in `runs/` (gitignored).
Then on the Mac: `python -m algo1 report --name q1` in `rit_competition/sim02_algo1`, and paste the
numbers into `PROGRESS.md`. Code changes go the other way: commit in `230X/sim02_algo1`, `git pull`
in `rit_competition/sim02_algo1`, re-run step 1.

## 7. Demo session checklist (a session is ~50 min of case time; spend it trading)

Before the case starts (or at tick 0):
```
cd C:\Users\e.roubache\algo1
robocopy \\tsclient\rit_competition\sim02_algo1 C:\Users\e.roubache\algo1 /E /R:2 /W:2 /XD .venv .venv.nosync runs __pycache__ .pytest_cache .git /XF secrets.env
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m algo1 probe                       # 10 s: is the client up, key ok, RTT sane
```
During the case, window 1 (each run stops itself at the end of the case; `--max-seconds` to stop earlier):
```
.venv\Scripts\python.exe -m algo1 record --name tapeN --seconds 60     # BBO + trades → analyze
.venv\Scripts\python.exe -m algo1 run --name pN                         # pairs only, whole case
.venv\Scripts\python.exe -m algo1 run --name qN --quotes --max-seconds 600
```
Window 2: `.venv\Scripts\python.exe -m algo1 monitor --name pN` (exits by itself when the engine stops).
Window 3, only if the monitor line shows fills climbing with no `X` on the book: `type nul > runs\KILL`.
After each run: `report --name pN`; read the blotter P&L in the client and note it next to the run name.
End of session: `robocopy C:\Users\e.roubache\algo1\runs \\tsclient\rit_competition\sim02_algo1\runs /E /R:2 /W:2`.

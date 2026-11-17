"""`agora update`, /update: pull the latest agora into the install (a git clone)."""
import os, subprocess, sys

from .ws import INSTALL, VENV, WINDOWS


def self_update():
    """git pull --ff-only in the install. Returns (changed, text)."""
    def git(*a):
        return subprocess.run(["git", "-C", INSTALL, *a], capture_output=True, text=True)
    if not os.path.isdir(os.path.join(INSTALL, ".git")):
        return False, f"{INSTALL} isn't a git clone, so it can't update itself; replace it with a fresh clone"
    try:
        old = git("rev-parse", "HEAD").stdout.strip()
    except FileNotFoundError:
        return False, "git isn't installed (or not on PATH), so agora can't update itself"
    r = git("pull", "--ff-only", "-q")
    if r.returncode:
        why = (r.stderr.strip().splitlines() or ["git pull failed"])
        return False, "update failed: " + " ".join(why[-2:]) + f"\n(fix it in {INSTALL}, e.g. git status)"
    new = git("rev-parse", "HEAD").stdout.strip()
    if new == old:
        return False, "agora is already up to date"
    log = git("log", "--oneline", "--no-decorate", f"{old}..{new}").stdout.strip().splitlines()
    text = f"updated agora {old[:7]} → {new[:7]}:\n" + "\n".join("  " + x for x in log[:12]) + (
        f"\n  … and {len(log) - 12} more" if len(log) > 12 else "")
    if "requirements.txt" in git("diff", "--name-only", old, new).stdout.split():
        py = os.path.join(VENV, "Scripts", "python.exe") if WINDOWS else os.path.join(VENV, "bin", "python")
        if os.path.isfile(py):
            p = subprocess.run([py, "-m", "pip", "install", "-q", "-r", os.path.join(INSTALL, "requirements.txt")],
                               capture_output=True, text=True)
            text += "\nrequirements installed" if p.returncode == 0 else \
                "\nrequirements changed but pip failed: " + (p.stderr.strip().splitlines() or ["?"])[-1]
    return True, text


RELAUNCH = 75   # the chat exits with this to be started again on the new code (by the Windows launcher)


def relaunch(argv):
    """Start this command again on the code now on disk. POSIX: exec in place. Windows has no real exec: exit with
    RELAUNCH and the launcher (agora) runs it again; returns False if no launcher waits (the caller says to reopen)."""
    sys.stdout.flush()
    if not WINDOWS:
        os.execv(argv[0], argv)
    if os.environ.get("AGORA_LAUNCHER") == "1":
        sys.exit(RELAUNCH)
    return False

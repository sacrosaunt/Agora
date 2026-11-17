"""Operating-system specifics: file locks, atomic replace, detached processes, the terminal (Linux, macOS, Windows)."""
import argparse, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401

WINDOWS = sys.platform == "win32"

if WINDOWS:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p), ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(_OVERLAPPED)]
    _k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(_OVERLAPPED)]
    _LOCK_EXCLUSIVE, _LOCK_NB, _LOCK_VIOLATION = 0x2, 0x1, 33

    def _ov():   # Windows locks are mandatory: lock one byte far past the end so reads and writes are never blocked
        o = _OVERLAPPED()
        o.OffsetHigh = 0x7FFFFFFF
        return o

    def lock(f, shared=False, block=True):
        """Lock the open file f (exclusive, or shared). With block=False: False when someone else holds it."""
        h = msvcrt.get_osfhandle(f.fileno())
        if _k32.LockFileEx(h, (0 if shared else _LOCK_EXCLUSIVE) | (0 if block else _LOCK_NB), 0, 1, 0,
                           ctypes.byref(_ov())):
            return True
        err = ctypes.get_last_error()
        if not block and err == _LOCK_VIOLATION:
            return False
        raise ctypes.WinError(err)

    def unlock(f):
        _k32.UnlockFileEx(msvcrt.get_osfhandle(f.fileno()), 0, 1, 0, ctypes.byref(_ov()))
else:
    import fcntl

    def lock(f, shared=False, block=True):
        """Lock the open file f (exclusive, or shared). With block=False: False when someone else holds it."""
        try:
            fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if block else fcntl.LOCK_NB))
            return True
        except BlockingIOError:
            return False

    def unlock(f):
        fcntl.flock(f, fcntl.LOCK_UN)


def tmpname(p):
    """A temp name next to p, unique per process and thread: concurrent writers never share (or steal) one."""
    import threading
    return f"{p}.{os.getpid()}-{threading.get_ident()}.tmp"


def replace(src, dst, tries=50):
    """os.replace that waits out Windows' sharing violations (another process reading dst for a moment)."""
    for i in range(tries):
        try:
            return os.replace(src, dst)
        except PermissionError:
            if not WINDOWS or i == tries - 1:
                raise
            time.sleep(0.1)


def rename(src, dst, tries=100):
    """os.rename, retried on Windows while something holds a file in src open (a directory can't move then)."""
    for i in range(tries):
        try:
            return os.rename(src, dst)
        except PermissionError:
            if not WINDOWS or i == tries - 1:
                raise
            time.sleep(0.1)


def rmtree(p):
    """Remove a tree, best effort; on Windows also the read-only files git leaves in .git/objects."""
    def clear(fn, path, _):
        try:
            os.chmod(path, 0o700)
            fn(path)
        except OSError:
            pass
    shutil.rmtree(p, onerror=clear)


def detached():
    """Popen options for a background process that outlives this terminal and ignores its Ctrl-C."""
    if WINDOWS:   # its own hidden console, which the agent CLIs it starts share (no windows pop up)
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(pid, grace=3.0):
    """Kill a process and everything it started. POSIX: its process group (a child started with
    start_new_session=True leads its own), SIGTERM then SIGKILL after grace seconds; Windows: taskkill /T /F.
    Returns False when there was nothing to kill."""
    if WINDOWS:
        return subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True).returncode == 0
    import signal
    try:
        if os.getpgid(pid) != pid:   # not a group leader we started (a stale pid now reused): leave it alone
            return False
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    end = time.time() + grace   # the CLI gets a moment to exit cleanly, then the whole group goes
    while time.time() < end:
        time.sleep(0.1)
        try:
            os.killpg(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return True


def venv_python(venv):
    return os.path.join(venv, "Scripts", "python.exe") if WINDOWS else os.path.join(venv, "bin", "python")


def ansi_terminal():
    """Turn on escape-sequence handling in a Windows console (colours, cursor moves); True when stdout takes them."""
    if not sys.stdout.isatty():
        return False
    if not WINDOWS:
        return True
    try:
        h = _k32.GetStdHandle(-11)
        mode = wintypes.DWORD()
        return bool(_k32.GetConsoleMode(h, ctypes.byref(mode)) and _k32.SetConsoleMode(h, mode.value | 0x4))
    except Exception:
        return False


__all__ = [k for k in list(globals()) if not k.startswith("__")]

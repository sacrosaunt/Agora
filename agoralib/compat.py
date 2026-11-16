"""Operating-system specifics: file locks."""
import argparse, fcntl, json, os, re, shutil, subprocess, sys, tempfile, time  # noqa: F401


def lock(f, shared=False, block=True):
    """Lock the open file f (exclusive, or shared). With block=False: False when someone else holds it."""
    try:
        fcntl.flock(f, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if block else fcntl.LOCK_NB))
        return True
    except BlockingIOError:
        return False


__all__ = [k for k in list(globals()) if not k.startswith("__")]

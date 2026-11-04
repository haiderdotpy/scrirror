import subprocess
from contextlib import contextmanager


def _adb(*args):
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=10)
        return r.returncode == 0, r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, ""


@contextmanager
def phone_link(port=8080, stay_awake=True):
    ok, _ = _adb("reverse", f"tcp:{port}", f"tcp:{port}")
    print("adb reverse:", "ok" if ok else "FAILED (phone connected and authorized?)")

    original = None
    if ok and stay_awake:
        got, value = _adb("shell", "settings", "get", "global", "stay_on_while_plugged_in")
        original = int(value) if got and value.isdigit() else 0
        # the setting is a bitmask: 1 = AC, 2 = USB, 4 = wireless
        _adb("shell", "settings", "put", "global", "stay_on_while_plugged_in", str(original | 2))

    try:
        yield ok
    finally:
        if original is not None:
            _adb("shell", "settings", "put", "global", "stay_on_while_plugged_in", str(original))
        if ok:
            _adb("reverse", "--remove", f"tcp:{port}")

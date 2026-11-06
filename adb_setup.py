import subprocess
from contextlib import contextmanager


def _adb(*args):
    try:
        r = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=10)
        return r.returncode == 0, r.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False, ""


def get_phone_resolution(landscape=True):
    """Return the phone's screen size as (width, height), or None on failure.

    Prefers an "Override size" line from `adb shell wm size` over the physical
    size, and swaps axes so width >= height when landscape=True.
    """
    ok, output = _adb("shell", "wm", "size")
    if not ok:
        return None

    override = physical = None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("Override size:"):
            override = line.split(":", 1)[1].strip()
        elif line.startswith("Physical size:"):
            physical = line.split(":", 1)[1].strip()

    value = override or physical
    if not value:
        return None

    try:
        width_str, height_str = value.lower().split("x", 1)
        width, height = int(width_str), int(height_str)
    except ValueError:
        return None

    if width <= 0 or height <= 0:
        return None

    if landscape and height > width:
        width, height = height, width

    return width, height


def normalize_resolution(width, height, max_long_side=2560):
    """Downscale to max_long_side and round both dimensions down to even."""
    longest = max(width, height)
    if longest > max_long_side:
        scale = max_long_side / longest
        width = int(width * scale)
        height = int(height * scale)

    width = max(2, width - (width % 2))
    height = max(2, height - (height % 2))
    return width, height


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

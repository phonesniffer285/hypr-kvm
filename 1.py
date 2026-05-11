#!/usr/bin/env python3

import sys
import time
import json
import subprocess
import shutil
import socket
import os
import threading

DEBUG = "--debug" in sys.argv

def debug(msg):
    if DEBUG:
        print(f"[DEBUG] {msg}")

def check_dependencies():
    if not shutil.which("ydotool"):
        print("❌ ydotool not found. Please install it: sudo pacman -S ydotool")
        sys.exit(1)
    status = subprocess.run(
        ["systemctl", "--user", "is-active", "ydotool"],
        capture_output=True, text=True
    ).stdout.strip()
    if status != "active":
        print("❌ ydotool daemon is not active. Please start it: systemctl --user enable --now ydotool")
        sys.exit(1)

# ─── Configuration ────────────────────────────────────────────────────────────
WINDOW_TITLE  = "Android-Input"
LOG           = "/tmp/scrcpy-kvm.log"
NULL          = subprocess.DEVNULL
EDGE_DEBOUNCE = 3        # consecutive polls required to confirm edge cross
POLL_INTERVAL = 0.016    # ~60 Hz

# ─── Helpers ──────────────────────────────────────────────────────────────────
def notify(icon, title, msg, urgency="normal"):
    subprocess.Popen(
        ["notify-send", "-u", urgency, "-a", "Android KVM", title, f"{icon} {msg}"],
        stdout=NULL, stderr=NULL
    )
    debug(f"Notify: {icon} {msg}")

# ─── RCtrl Listener (evdev) ───────────────────────────────────────────────────
# Signals the main loop to return to PC when RCtrl is pressed while on Android.
# Runs in a daemon thread so it dies automatically when the main process exits.

_rctrl_event = threading.Event()

def _rctrl_listener(stop_flag: threading.Event):
    """
    Watches all /dev/input/event* devices for KEY_RIGHTCTRL (keycode 97).
    Sets _rctrl_event on keydown so the main loop can act on it.
    Falls back gracefully if python-evdev is not installed.
    """
    try:
        import evdev
        from evdev import ecodes
    except ImportError:
        debug("python-evdev not found — RCtrl detection disabled. "
              "Install with: pip install evdev  (or: sudo pacman -S python-evdev)")
        return

    KEY_RIGHTCTRL = ecodes.KEY_RIGHTCTRL

    def open_keyboards():
        boards = []
        for path in evdev.list_devices():
            try:
                dev = evdev.InputDevice(path)
                cap = dev.capabilities()
                if ecodes.EV_KEY in cap and KEY_RIGHTCTRL in cap[ecodes.EV_KEY]:
                    boards.append(dev)
                    debug(f"Monitoring {dev.name} ({path}) for RCtrl")
            except Exception:
                pass
        return boards

    keyboards = open_keyboards()
    if not keyboards:
        debug("No keyboard devices found for RCtrl monitoring.")
        return

    import selectors
    sel = selectors.DefaultSelector()
    for kb in keyboards:
        sel.register(kb, selectors.EVENT_READ)

    while not stop_flag.is_set():
        ready = sel.select(timeout=0.5)
        for key, _ in ready:
            dev = key.fileobj
            try:
                for event in dev.read():
                    if (event.type == ecodes.EV_KEY
                            and event.code == KEY_RIGHTCTRL
                            and event.value == 1):   # 1 = keydown
                        debug("RCtrl keydown detected")
                        _rctrl_event.set()
            except Exception:
                pass

    sel.close()
    for kb in keyboards:
        try:
            kb.close()
        except Exception:
            pass

# ─── Hyprland IPC ─────────────────────────────────────────────────────────────
def _find_hypr_socket():
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        return None
    xdg = os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")
    for p in [f"{xdg}/hypr/{sig}/.socket.sock", f"/tmp/hypr/{sig}/.socket.sock"]:
        if os.path.exists(p):
            return p
    return None

_HYPR_SOCK = _find_hypr_socket()

def hyprctl_request(cmd):
    if _HYPR_SOCK:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(_HYPR_SOCK)
                s.sendall(f"-j/{cmd}".encode())
                chunks = []
                while True:
                    data = s.recv(8192)
                    if not data:
                        break
                    chunks.append(data)
                return b"".join(chunks).decode()
        except Exception:
            pass
    return subprocess.check_output(["hyprctl", "-j", cmd]).decode()

def get_active_workspace():
    try:
        return json.loads(hyprctl_request("activeworkspace")).get("id", 1)
    except Exception:
        return 1

def get_monitors():
    try:
        return json.loads(hyprctl_request("monitors"))
    except Exception:
        return []

_mon_cache = {"data": [], "ts": 0.0}
_MON_TTL   = 30.0

def cached_monitors():
    now = time.monotonic()
    if not _mon_cache["data"] or now - _mon_cache["ts"] > _MON_TTL:
        _mon_cache["data"] = get_monitors()
        _mon_cache["ts"]   = now
    return _mon_cache["data"]

def invalidate_monitor_cache():
    _mon_cache["ts"] = 0.0

def get_focused_monitor_bounds():
    for m in cached_monitors():
        if m.get("focused"):
            scale = m.get("scale", 1.0)
            return int(m["width"] / scale), int(m["height"] / scale), int(m["x"]), int(m["y"])
    return 1920, 1080, 0, 0

def get_right_edge():
    mons = cached_monitors()
    if not mons:
        return 1920
    return max(m["x"] + int(m["width"] / m.get("scale", 1.0)) for m in mons)

def get_cursor_pos():
    try:
        data = json.loads(hyprctl_request("cursorpos"))
        return int(data.get("x", 0)), int(data.get("y", 0))
    except Exception:
        try:
            out = subprocess.check_output(["hyprctl", "cursorpos"]).decode()
            x_str, y_str = out.strip().split(",")
            return int(x_str.strip()), int(y_str.strip())
        except Exception:
            return 0, 0

# ─── Network / ADB ────────────────────────────────────────────────────────────
def check_network():
    if subprocess.run("ip link show eno1 | grep -q 'state UP'", shell=True).returncode != 0:
        notify("❌", "Android KVM", "eno1 is down. Plug in the dock.", "critical")
        sys.exit(1)

    phone_ip = subprocess.run(
        "ip route show dev eno1 | awk '/via/{print $3; exit}'",
        shell=True, capture_output=True, text=True
    ).stdout.strip()

    if not phone_ip:
        phone_ip = subprocess.run(
            "ip neighbor show dev eno1 | awk 'NR==1{print $1}'",
            shell=True, capture_output=True, text=True
        ).stdout.strip()

    if not phone_ip:
        notify("❌", "Android KVM", "Phone not found on eno1. Is Tethering ON?", "critical")
        sys.exit(1)

    notify("🔗", "Android KVM", f"Phone at {phone_ip} — connecting...", "low")
    return phone_ip

def connect_adb(phone_ip):
    subprocess.run(["adb", "disconnect", f"{phone_ip}:5555"], stdout=NULL, stderr=NULL)
    result = subprocess.run(["adb", "connect", f"{phone_ip}:5555"], capture_output=True, text=True)
    out = result.stdout + result.stderr
    if "connected" not in out.lower():
        notify("❌", "Android KVM", f"ADB failed: {out.strip()}", "critical")
        sys.exit(1)
    notify("✅", "Android KVM", "ADB connected.", "low")

# ─── Window Rules ─────────────────────────────────────────────────────────────
def setup_hyprland_rules():
    title_match = f"title:^({WINDOW_TITLE})$"
    class_match = "class:^(scrcpy)$"

    invisible = [
        f"noanim,{m}"                               for m in (title_match, class_match)
    ] + [
        f"opacity 0.0 override 0.0 override,{m}"   for m in (title_match, class_match)
    ] + [
        f"noborder,{title_match}",
        f"noblur,{title_match}",
        f"noshadow,{title_match}",
    ]

    placement = [
        f"move -9999 -9999,{title_match}",
        f"move -9999 -9999,{class_match}",
        f"size 200 200,{title_match}",
        f"float,{title_match}",
    ]

    workspace = [
        f"workspace special:kvm silent,{title_match}",
        f"workspace special:kvm silent,{class_match}",
    ]

    focus_suppression = [
        f"suppressevent activate activatefocus,{title_match}",
        f"suppressevent activate activatefocus,{class_match}",
        f"noinitialfocus,{title_match}",
        f"noinitialfocus,{class_match}",
    ]

    for rule in invisible + placement + workspace + focus_suppression:
        subprocess.run(["hyprctl", "keyword", "windowrulev2", rule], stdout=NULL, stderr=NULL)

# ─── scrcpy Launch ────────────────────────────────────────────────────────────
def launch_scrcpy_backbone(phone_ip):
    setup_hyprland_rules()
    cmd = [
        "scrcpy",
        f"--serial={phone_ip}:5555",
        "--no-video",
        "--keyboard=uhid",
        "--mouse=uhid",
        "--mouse-bind=++++:++++",
        "--stay-awake",
        f"--window-title={WINDOW_TITLE}",
        "--shortcut-mod=rctrl",
    ]
    log_file = open(LOG, "w")
    proc = subprocess.Popen(cmd, stdout=log_file, stderr=log_file)
    return proc, log_file

# ─── KVM Switch Actions ───────────────────────────────────────────────────────
def switch_to_android(active_ws, center_x, center_y):
    debug(f"Activating Android — ws={active_ws}, warp=({center_x},{center_y})")
    batch = (
        f"dispatch movetoworkspacesilent {active_ws},title:^({WINDOW_TITLE})$ ; "
        f"dispatch focuswindow title:^({WINDOW_TITLE})$ ; "
        f"dispatch movecursor {center_x} {center_y}"
    )
    subprocess.run(["hyprctl", "--batch", batch], stdout=NULL, stderr=NULL)
    time.sleep(0.04)
    subprocess.run(["ydotool", "click", "0xC0"], stdout=NULL, stderr=NULL)

def switch_to_pc():
    debug("Returning to PC")
    subprocess.run(
        ["hyprctl", "dispatch", "movetoworkspacesilent",
         f"special:kvm,title:^({WINDOW_TITLE})$"],
        stdout=NULL, stderr=NULL
    )

# ─── Main Loop ────────────────────────────────────────────────────────────────
def main():
    if DEBUG:
        print("🛠️  DEBUG MODE ENABLED")

    check_dependencies()
    phone_ip = check_network()
    connect_adb(phone_ip)
    scrcpy_proc, log_file = launch_scrcpy_backbone(phone_ip)

    time.sleep(1.5)
    notify("🚀", "Android KVM", "Seamless KVM active! Move mouse to right edge.", "normal")

    # Start RCtrl listener thread
    _stop_flag = threading.Event()
    listener_thread = threading.Thread(
        target=_rctrl_listener, args=(_stop_flag,), daemon=True
    )
    listener_thread.start()

    is_on_android = False
    right_edge    = get_right_edge()
    EDGE_TRIGGER  = right_edge - 2
    edge_count    = 0

    debug(f"Right edge={right_edge}, trigger at x≥{EDGE_TRIGGER}, debounce={EDGE_DEBOUNCE}")

    try:
        while scrcpy_proc.poll() is None:
            x, y = get_cursor_pos()

            # ── PC → Android ────────────────────────────────────────────────
            if not is_on_android:
                if x >= EDGE_TRIGGER:
                    edge_count += 1
                    debug(f"Edge count {edge_count}/{EDGE_DEBOUNCE} at x={x}")
                    if edge_count >= EDGE_DEBOUNCE:
                        is_on_android = True
                        edge_count    = 0
                        _rctrl_event.clear()   # discard any stale RCtrl presses
                        notify("📱", "Android KVM", "Switched to Android! (RCtrl to return)", "low")
                        active_ws           = get_active_workspace()
                        w, h, mx, my        = get_focused_monitor_bounds()
                        switch_to_android(active_ws, mx + w // 2, my + h // 2)
                else:
                    edge_count = 0

            # ── Android → PC (RCtrl — primary) ──────────────────────────────
            elif _rctrl_event.is_set():
                _rctrl_event.clear()
                is_on_android = False
                edge_count    = 0
                switch_to_pc()
                notify("💻", "Android KVM", "Returned to PC (RCtrl)", "low")

            # ── Android → PC (cursor — fallback) ────────────────────────────
            elif is_on_android:
                w, h, mx, my = get_focused_monitor_bounds()
                if x < (mx + w // 2) - 250:
                    is_on_android = False
                    edge_count    = 0
                    switch_to_pc()
                    notify("💻", "Android KVM", "Returned to PC", "low")

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        debug("Keyboard interrupt — shutting down.")
    finally:
        _stop_flag.set()
        scrcpy_proc.terminate()
        log_file.close()
        subprocess.run(["adb", "disconnect", f"{phone_ip}:5555"], stdout=NULL, stderr=NULL)
        notify("🧹", "Android KVM", "Session ended cleanly.", "low")

if __name__ == "__main__":
    main()

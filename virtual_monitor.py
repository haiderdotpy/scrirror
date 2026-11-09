import os
import subprocess
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst
from streamer import FrameHub, start_http_server, start_pipeline
from video_codecs import create_codec

Gst.init(None)

DEBUG = bool(os.environ.get("SCRIRROR_DEBUG"))

SCREENCAST_BUS = "org.gnome.Mutter.ScreenCast"
SCREENCAST_PATH = "/org/gnome/Mutter/ScreenCast"
SCREENCAST_IFACE = "org.gnome.Mutter.ScreenCast"

DISPLAY_CONFIG_BUS = "org.gnome.Mutter.DisplayConfig"
DISPLAY_CONFIG_PATH = "/org/gnome/Mutter/DisplayConfig"
DISPLAY_CONFIG_IFACE = "org.gnome.Mutter.DisplayConfig"

APPLY_METHOD_TEMPORARY = 1
PLACEMENT_ATTEMPTS = 25
PLACEMENT_INTERVAL_MS = 200


def _now():
    return time.strftime("%H:%M:%S")


def _proxy(bus, bus_name, path, iface):
    return Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.NONE, None, bus_name, path, iface, None)


def _node_is_video_source(node_id):
    """Return True/False for node liveness, or None if pw-cli is unavailable."""
    try:
        result = subprocess.run(
            ["pw-cli", "info", str(node_id)],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    output = result.stdout
    return "media.class" in output and (
        "Stream/Output/Video" in output or "gnome-shell" in output
    )


def _get_display_state(display):
    result = display.call_sync(
        "GetCurrentState", None, Gio.DBusCallFlags.NONE, -1, None)
    return result.unpack()


def _mode_dimensions(monitors):
    """Map connector -> (mode_id, width, height) for the current mode."""
    dimensions = {}
    for ident, modes, props in monitors:
        connector = ident[0]
        chosen = None
        for mode in modes:
            mode_id, mode_w, mode_h, _refresh, _pscale, _scales, mode_props = mode
            if mode_props.get("is-current"):
                chosen = (mode_id, mode_w, mode_h)
                break
        if chosen is None:
            for mode in modes:
                mode_id, mode_w, mode_h, _refresh, _pscale, _scales, mode_props = mode
                if mode_props.get("is-preferred"):
                    chosen = (mode_id, mode_w, mode_h)
                    break
        if chosen is None and modes:
            chosen = tuple(modes[0][:3])
        if chosen is not None:
            dimensions[connector] = chosen
    return dimensions


def _find_virtual_connector(monitors, known_connectors):
    for ident, _modes, _props in monitors:
        if ident[0] not in known_connectors:
            return ident[0]

    for ident, _modes, props in monitors:
        connector, vendor, product, _serial = ident
        display_name = str(props.get("display-name", ""))
        if vendor == "MetaVendor" or "virtual" in product.lower() or "virtual" in display_name.lower():
            return connector
    return None


def _logical_monitor_size(logical_monitor, dimensions):
    _x, _y, scale, transform, _primary, monitors, _props = logical_monitor
    for monitor in monitors:
        connector = monitor[0]
        if connector in dimensions:
            _mode_id, mode_w, mode_h = dimensions[connector]
            if transform in (1, 3):
                mode_w, mode_h = mode_h, mode_w
            return mode_w / scale, mode_h / scale
    return None


def _apply_layout(display, virtual_connector, width, side, retry=True):
    try:
        serial, monitors, logical_monitors, properties = _get_display_state(display)
    except GLib.Error as e:
        print("DisplayConfig GetCurrentState failed:", e.message)
        return False

    dimensions = _mode_dimensions(monitors)

    primary = next((lm for lm in logical_monitors if lm[4]), None)
    if primary is None and logical_monitors:
        primary = logical_monitors[0]
    if primary is None:
        print("No logical monitors found; skipping placement")
        return False

    primary_x, primary_y = primary[0], primary[1]

    virtual_index = None
    for index, logical_monitor in enumerate(logical_monitors):
        if any(monitor[0] == virtual_connector for monitor in logical_monitor[5]):
            virtual_index = index
            break

    virtual_scale = 1.0
    virtual_transform = 0
    if virtual_index is not None:
        virtual_scale = logical_monitors[virtual_index][2]
        virtual_transform = logical_monitors[virtual_index][3]

    virtual_dimensions = dimensions.get(virtual_connector)
    if virtual_dimensions is None:
        print(f"No mode found for virtual connector {virtual_connector}")
        return False

    virtual_mode_w = virtual_dimensions[1]
    if virtual_transform in (1, 3):
        virtual_mode_w = virtual_dimensions[2]
    virtual_logical_width = int(round(virtual_mode_w / virtual_scale))

    if side == "left":
        # Mutter rejects negative logical-monitor positions, so keep the
        # leftmost monitor at x=0 and shift everything else right instead.
        shift_x = virtual_logical_width
        new_x, new_y = 0, primary_y
    else:
        shift_x = 0
        size = _logical_monitor_size(primary, dimensions)
        primary_width = int(round(size[0])) if size else width
        new_x, new_y = primary_x + primary_width, primary_y

    new_logical_monitors = []
    for index, logical_monitor in enumerate(logical_monitors):
        x, y, scale, transform, is_primary, monitors_in_lm, _lm_props = logical_monitor
        monitor_configs = []
        for monitor in monitors_in_lm:
            connector = monitor[0]
            dimensions_for_connector = dimensions.get(connector)
            if dimensions_for_connector is None:
                print(f"No mode found for connector {connector}; skipping placement")
                return False
            monitor_configs.append((connector, dimensions_for_connector[0], {}))

        if index == virtual_index:
            new_logical_monitors.append(
                (new_x, new_y, scale, transform, is_primary, monitor_configs))
        else:
            new_logical_monitors.append(
                (x + shift_x, y, scale, transform, is_primary, monitor_configs))

    if virtual_index is None:
        dimensions_for_connector = dimensions.get(virtual_connector)
        if dimensions_for_connector is None:
            print(f"No mode found for virtual connector {virtual_connector}")
            return False
        new_logical_monitors.append(
            (new_x, new_y, 1.0, 0, False, [(virtual_connector, dimensions_for_connector[0], {})])
        )

    apply_properties = {}
    if "layout-mode" in properties:
        apply_properties["layout-mode"] = GLib.Variant("u", properties["layout-mode"])

    args = GLib.Variant(
        "(uua(iiduba(ssa{sv}))a{sv})",
        (serial, APPLY_METHOD_TEMPORARY, new_logical_monitors, apply_properties),
    )

    try:
        display.call_sync(
            "ApplyMonitorsConfig", args, Gio.DBusCallFlags.NONE, -1, None)
    except GLib.Error as e:
        print("ApplyMonitorsConfig failed:", e.message)
        if retry:
            print("Retrying ApplyMonitorsConfig after re-reading state")
            return _apply_layout(display, virtual_connector, width, side, retry=False)
        return False

    print(
        f"[{_now()}] Placed virtual monitor {virtual_connector} "
        f"on the {side} at ({new_x}, {new_y})"
    )
    return True


def add_virtual_monitor(
    width=1920, height=1080, side="left", codec="h264", jpeg_quality=70
):
    if side not in ("left", "right"):
        raise ValueError("side must be 'left' or 'right'")

    video_codec = create_codec(codec, jpeg_quality=jpeg_quality)
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    loop = GLib.MainLoop()
    hub = FrameHub(max_queue_size=video_codec.queue_depth)
    start_http_server(hub, video_codec)

    screencast = _proxy(bus, SCREENCAST_BUS, SCREENCAST_PATH, SCREENCAST_IFACE)
    display = _proxy(bus, DISPLAY_CONFIG_BUS, DISPLAY_CONFIG_PATH, DISPLAY_CONFIG_IFACE)

    state = {
        "pipeline": None,
        "watch_id": None,
        "node_id": None,
        "session": None,
        "stream": None,
        "session_signal_id": None,
        "stream_signal_id": None,
        "session_closed": False,
        "stopping": False,
        "handling_failure": False,
        "restarts": 0,
        "placement_source_id": None,
        "placement_attempts": 0,
        "suppress_until": 0.0,
        "monitor_recovery_source_id": None,
        "pipeline_generation": 0,
    }

    def on_monitors_changed(_proxy, _sender, signal_name, _params):
        if signal_name != "MonitorsChanged":
            return
        print(f"[{_now()}] DisplayConfig MonitorsChanged")
        if time.monotonic() < state["suppress_until"]:
            print(f"[{_now()}]   (ignoring; caused by our own session/placement)")
            return
        schedule_monitor_recovery()

    display.connect("g-signal", on_monitors_changed)

    try:
        _serial, monitors, _logical, _props = _get_display_state(display)
        known_connectors = {monitor[0][0] for monitor in monitors}
    except GLib.Error as e:
        print("DisplayConfig GetCurrentState failed at startup:", e.message)
        known_connectors = set()

    def stop_pipeline():
        state["pipeline_generation"] += 1
        pipeline = state["pipeline"]
        watch_id = state["watch_id"]
        state["pipeline"] = None
        state["watch_id"] = None
        if watch_id is not None:
            GLib.source_remove(watch_id)
        if pipeline is not None:
            pipeline.set_state(Gst.State.NULL)

    def on_pipeline_playing():
        state["restarts"] = 0

    def on_gst_failure(reason, generation):
        if generation != state["pipeline_generation"]:
            return
        print(f"[{_now()}] GStreamer failure: {reason}")
        GLib.idle_add(handle_failure, reason, generation)

    def start_consumer():
        if state["node_id"] is None:
            print("No PipeWire node id; cannot start consumer")
            return
        stop_pipeline()
        generation = state["pipeline_generation"]

        def on_failure(reason):
            on_gst_failure(reason, generation)

        state["pipeline"], state["watch_id"] = start_pipeline(
            state["node_id"], width, height, hub, video_codec,
            on_failure=on_failure,
            on_playing=on_pipeline_playing,
        )

    def schedule_placement():
        if state["placement_source_id"] is not None:
            GLib.source_remove(state["placement_source_id"])
        state["placement_attempts"] = PLACEMENT_ATTEMPTS
        state["placement_source_id"] = GLib.timeout_add(
            PLACEMENT_INTERVAL_MS, attempt_placement)

    def attempt_placement():
        if state["stopping"]:
            return False
        state["placement_attempts"] -= 1
        try:
            _serial, monitors, _logical, _props = _get_display_state(display)
        except GLib.Error as e:
            print("DisplayConfig GetCurrentState failed:", e.message)
            return state["placement_attempts"] > 0

        virtual_connector = _find_virtual_connector(monitors, known_connectors)
        if virtual_connector is None:
            if state["placement_attempts"] <= 0:
                print("Could not identify the virtual monitor connector; leaving default placement")
            return state["placement_attempts"] > 0

        print(f"[{_now()}] Virtual monitor connector: {virtual_connector}")
        state["suppress_until"] = time.monotonic() + 3.0
        if _apply_layout(display, virtual_connector, width, side):
            return False
        return state["placement_attempts"] > 0

    def teardown_session():
        if state["placement_source_id"] is not None:
            GLib.source_remove(state["placement_source_id"])
            state["placement_source_id"] = None
        session = state["session"]
        state["session"] = None
        state["stream"] = None
        state["node_id"] = None
        if session is not None:
            if state["session_signal_id"] is not None:
                session.disconnect(state["session_signal_id"])
                state["session_signal_id"] = None
            try:
                session.call_sync("Stop", None, Gio.DBusCallFlags.NONE, -1, None)
            except GLib.Error as e:
                if DEBUG:
                    print("Session Stop failed:", e.message)

    def create_session():
        state["session_closed"] = False
        state["suppress_until"] = time.monotonic() + 3.0
        session_path = screencast.call_sync(
            "CreateSession",
            GLib.Variant("(a{sv})", ({},)),
            Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]

        session = _proxy(
            bus, SCREENCAST_BUS, session_path, "org.gnome.Mutter.ScreenCast.Session")
        state["session"] = session
        state["session_signal_id"] = session.connect("g-signal", on_session_signal)

        stream_path = session.call_sync(
            "RecordVirtual",
            GLib.Variant("(a{sv})", ({"cursor-mode": GLib.Variant("u", 1)},)),
            Gio.DBusCallFlags.NONE, -1, None,
        ).unpack()[0]

        stream = _proxy(
            bus, SCREENCAST_BUS, stream_path, "org.gnome.Mutter.ScreenCast.Stream")
        state["stream"] = stream
        state["stream_signal_id"] = stream.connect("g-signal", on_stream_signal)

        session.call_sync("Start", None, Gio.DBusCallFlags.NONE, -1, None)
        print(f"[{_now()}] ScreenCast session started: {session_path}")

    def schedule_monitor_recovery():
        if state["stopping"]:
            return
        if state["monitor_recovery_source_id"] is not None:
            GLib.source_remove(state["monitor_recovery_source_id"])
        print(f"[{_now()}] Scheduling session recovery after monitor change")
        state["monitor_recovery_source_id"] = GLib.timeout_add_seconds(
            1, recover_from_monitor_change)

    def recover_from_monitor_change():
        state["monitor_recovery_source_id"] = None
        if state["stopping"]:
            return False
        print(f"[{_now()}] Monitor configuration changed; recreating session")
        recreate_session()
        return False

    def recreate_session():
        if state["stopping"]:
            return
        print(f"[{_now()}] Recreating ScreenCast session")
        state["suppress_until"] = time.monotonic() + 5.0
        stop_pipeline()
        teardown_session()
        state["restarts"] = 0
        try:
            create_session()
        except GLib.Error as e:
            print(f"[{_now()}] Failed to recreate session:", e.message)
            teardown_session()
            GLib.timeout_add_seconds(2, retry_recreate)

    def retry_recreate():
        if not state["stopping"] and state["session"] is None:
            recreate_session()
        return False

    def handle_failure(reason, generation):
        if generation != state["pipeline_generation"]:
            return False
        if state["stopping"] or state["handling_failure"]:
            return False
        state["handling_failure"] = True
        try:
            stop_pipeline()
            if reason == "node-disappeared":
                print(f"[{_now()}] Source node is gone; recreating session")
                recreate_session()
            elif state["session"] is not None and not state["session_closed"]:
                if state["restarts"] < 1:
                    state["restarts"] += 1
                    print(f"[{_now()}] Restarting GStreamer consumer on existing session")
                    start_consumer()
                else:
                    print(f"[{_now()}] Consumer failed again; recreating session")
                    recreate_session()
            else:
                print(f"[{_now()}] Session unavailable; recreating session")
                recreate_session()
        finally:
            state["handling_failure"] = False
        return False

    def on_session_signal(proxy, _sender, signal_name, _params):
        if proxy is not state["session"]:
            return
        if signal_name == "Closed":
            print(f"[{_now()}] ScreenCast session Closed")
            state["session_closed"] = True
            stop_pipeline()
            if not state["stopping"]:
                GLib.idle_add(recreate_session)

    def on_stream_signal(proxy, _sender, signal_name, params):
        if proxy is not state["stream"]:
            return
        if signal_name == "PipeWireStreamAdded":
            node_id = params.unpack()[0]
            print(f"[{_now()}] PipeWireStreamAdded node_id={node_id}")
            state["node_id"] = node_id
            state["restarts"] = 0
            start_consumer()
            schedule_placement()

    def log_pipeline_stats():
        pipeline = state["pipeline"]
        if pipeline is not None:
            sink = pipeline.get_by_name("sink")
            stats = sink.get_property("stats") if sink is not None else None
            print(f"[{_now()}] appsink stats:", stats.to_string() if stats else "none")
        return True

    def check_source_node():
        node_id = state["node_id"]
        if (
            node_id is None
            or state["pipeline"] is None
            or state["stopping"]
            or state["handling_failure"]
        ):
            return True
        alive = _node_is_video_source(node_id)
        if alive is False:
            print(f"[{_now()}] PipeWire source node {node_id} disappeared")
            on_gst_failure("node-disappeared", state["pipeline_generation"])
        return True

    if DEBUG:
        GLib.timeout_add_seconds(2, log_pipeline_stats)
    GLib.timeout_add_seconds(2, check_source_node)

    try:
        create_session()
    except GLib.Error as e:
        print("Failed to start ScreenCast session:", e.message)
        return

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        state["stopping"] = True
        stop_pipeline()
        teardown_session()

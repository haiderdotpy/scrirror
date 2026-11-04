import os

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst
from streamer import FrameHub, start_http_server, start_pipeline

Gst.init(None)

DEBUG = bool(os.environ.get("SCRIRROR_DEBUG"))


def add_virtual_monitor(width=1920, height=1080):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    loop = GLib.MainLoop()
    pipeline = None

    hub = FrameHub()
    start_http_server(hub) 

    def proxy(path, iface):
        return Gio.DBusProxy.new_sync(
            bus, Gio.DBusProxyFlags.NONE, None,
            "org.gnome.Mutter.ScreenCast", path, iface, None)

    # --- the consumer: runs when Mutter announces the stream ---
    def start_consumer(node_id):
        nonlocal pipeline
        pipeline = start_pipeline(node_id, width, height, hub) 
        
    def on_gst_error(gst_bus, message):
        err, debug = message.parse_error()
        print("GStreamer error:", err.message)
        print("Debug:", debug)
        loop.quit()

    # --- D-Bus signal handler ---
    def on_signal(p, sender, signal_name, params):
        if DEBUG:
            print(signal_name, params.unpack())
        if signal_name == "PipeWireStreamAdded":
            start_consumer(params.unpack()[0])

    # --- Mutter calls (same as before) ---
    sc = proxy("/org/gnome/Mutter/ScreenCast", "org.gnome.Mutter.ScreenCast")
    session_path = sc.call_sync(
        "CreateSession", GLib.Variant("(a{sv})", ({},)), 0, -1, None).unpack()[0]

    session = proxy(session_path, "org.gnome.Mutter.ScreenCast.Session")
    stream_path = session.call_sync(
        "RecordVirtual",
        GLib.Variant("(a{sv})", ({"cursor-mode": GLib.Variant("u", 1)},)),
        0, -1, None).unpack()[0]

    stream = proxy(stream_path, "org.gnome.Mutter.ScreenCast.Stream")
    stream.connect("g-signal", on_signal)

    session.call_sync("Start", None, 0, -1, None)

    try:
        loop.run()
    except KeyboardInterrupt:
        pass
    finally:
        if pipeline:
            pipeline.set_state(Gst.State.NULL)
        session.call_sync("Stop", None, 0, -1, None)

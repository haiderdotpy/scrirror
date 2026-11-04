# scrirror

Use an Android phone as an extra monitor for a GNOME (Wayland) desktop.

The desktop creates a virtual monitor through Mutter, encodes its video as H.264, and streams it to the phone over USB. The phone displays it in Chrome using WebCodecs. No app install is needed on the phone.

**Status:** video streaming works end to end and is responsive. Touch input back to the desktop is not implemented yet. There are no automated tests or CI yet.

## How it works

```
Mutter (D-Bus: org.gnome.Mutter.ScreenCast)
  -> PipeWire node (video of the virtual monitor)
  -> GStreamer consumer (requests resolution, encodes H.264)
  -> appsink -> FrameHub -> HTTP server (127.0.0.1:8080)
  -> adb reverse tcp:8080 -> phone browser (Chrome)
```

1. **Virtual monitor.** `CreateSession` -> `RecordVirtual` -> `Start` over D-Bus (PyGObject, `Gio.DBusProxy`). Mutter emits `PipeWireStreamAdded(node_id)`. The session lives only as long as the D-Bus connection, so the GLib main loop blocks inside `add_virtual_monitor()`.
2. **Capture and encode.** GStreamer attaches to the PipeWire node and encodes with `x264enc` (baseline, `zerolatency`, no B-frames, `byte-stream`/`au` output). The monitor's resolution is set by the consumer's caps, since `RecordVirtual` has no size option.
3. **Fan-out.** `FrameHub` keeps a bounded queue per client and drops to the next keyframe on overflow.
4. **Serve.** A local HTTP server serves a canvas page and a `/stream` endpoint.
5. **Transport.** `adb reverse` forwards the phone's `localhost:8080` to the desktop over USB.
6. **Display.** The page fetches `/stream`, parses frames, and decodes them with a WebCodecs `VideoDecoder` drawn to a `<canvas>`.

### Wire format (`/stream`)

| Field         | Size     | Notes                                              |
| ------------- | -------- | -------------------------------------------------- |
| Length        | 4 bytes  | Big-endian                                         |
| Keyframe flag | 1 byte   | `not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)`    |
| Frame data    | variable | H.264 access unit (Annex-B)                        |

Decoder config on the page: codec `avc1.42E028`, `optimizeForLatency`, no `description`.

## Layout

```
scrirror/desktop_client/
  main.py             entry point
  virtual_monitor.py  Mutter ScreenCast virtual monitor
  streamer.py         GStreamer pipeline, FrameHub, HTTP server
  adb_setup.py        adb reverse + stay-awake, cleaned up on exit
```

## Requirements

- GNOME on Wayland
- Android phone with USB debugging enabled, and `adb` installed
- Chrome on the phone
- `uv`

System packages (Debian/Ubuntu names):

```
libgirepository-2.0-dev
gstreamer1.0-pipewire
gstreamer1.0-plugins-good
gstreamer1.0-plugins-ugly
gir1.2-gstreamer-1.0
gir1.2-gst-plugins-base-1.0
```

## Setup

Use the **system Python**, not Anaconda. Conda's GLib clashes with the system `libgirepository` (`undefined symbol: g_once_init_leave_pointer`). `.python-version` pins 3.12, so `uv sync` alone honors it; only pass `--python /usr/bin/python3` if uv would otherwise pick an interpreter from a conda environment.

```sh
cd scrirror/desktop_client
uv sync
# if conda is active: uv venv --python /usr/bin/python3 && uv sync
```

## Usage

1. Connect the phone over USB and confirm `adb devices` lists it.
2. Start the desktop client:
   ```sh
   uv run main.py
   ```
3. Open `http://localhost:8080` in Chrome on the phone.
4. Tap the canvas to toggle fullscreen.

`adb_setup.py` runs `adb reverse tcp:8080 tcp:8080` and keeps the screen awake while on USB, restoring your original stay-awake setting on exit.

### If the script is hard-killed

Cleanup is skipped, so run these by hand. This disables stay-awake entirely rather than restoring your original value:

```sh
adb shell svc power stayon false
adb reverse --remove tcp:8080
```

## Notes and gotchas

- `localhost` matters: WebCodecs requires a secure context. `http://localhost` via `adb reverse` qualifies; plain `http://<LAN IP>` does not.
- Mutter only emits frames when content changes, so an idle monitor looks frozen.
- The canvas uses `object-fit: contain`, so a phone aspect ratio that differs from the virtual monitor shows black bars.
- The HTTP server handles only `/` (the viewer page) and `/stream`; other paths return 404.
- Without a caps filter, the virtual monitor is 1x1. Resolution is a parameter of `add_virtual_monitor(width, height)`.
- A `framerate=30/1` caps constraint likely caused a "no more input formats" error, since Mutter probably offers variable framerate. It is left out.
- `org.gnome.Mutter.*` interfaces are private and unstable. Introspect against your installed version:
  ```sh
  busctl --user introspect org.gnome.Mutter.ScreenCast /org/gnome/Mutter/ScreenCast
  ```

## Security

There is no authentication. The server binds to `127.0.0.1` and is reachable only through the USB `adb reverse` tunnel. If it is ever exposed over Wi-Fi, add a token check first.

## Roadmap

- Pass the phone's resolution into `add_virtual_monitor` (optionally sent by the page) to avoid black bars.
- Touch input: events back over TCP, injected via `org.gnome.Mutter.RemoteDesktop` linked to the ScreenCast session (`NotifyPointerMotionAbsolute` or the touch methods).
- Measure latency formally; try a VA-API encoder (`vah264enc`) or WebRTC if needed.
- Optional native Android app (MediaCodec) fed by the same byte stream.

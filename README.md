# scrirror

Use an Android phone as an extra monitor for a GNOME (Wayland) desktop.

The desktop creates a virtual monitor through Mutter, encodes its video as H.264 (default) or MJPEG, and streams it to the phone over USB. The phone displays it in Chrome using WebCodecs (H.264) or a plain `<img>` (MJPEG). No app install is needed on the phone.

**Status:** video streaming works end to end and is responsive. Touch input back to the desktop is not implemented yet. There are no automated tests or CI yet.

## How it works

```
Mutter (D-Bus: org.gnome.Mutter.ScreenCast)
  -> PipeWire node (video of the virtual monitor)
  -> GStreamer consumer (requests resolution, encodes H.264)
  -> appsink -> FrameHub -> HTTP server (127.0.0.1:8080)
  -> adb reverse tcp:8080 -> phone browser (Chrome)
```

1. **Virtual monitor.** `CreateSession` -> `RecordVirtual` -> `Start` over D-Bus (PyGObject, `Gio.DBusProxy`). Mutter emits `PipeWireStreamAdded(node_id)`. The session lives only as long as the D-Bus connection, so the GLib main loop blocks inside `add_virtual_monitor()`. The client then uses `DisplayConfig` to place the virtual monitor on the requested side.
2. **Capture and encode.** GStreamer attaches to the PipeWire node and encodes with either `x264enc` (baseline, `zerolatency`, no B-frames, `byte-stream`/`au` output) or `jpegenc` (MJPEG, quality configurable). The monitor's resolution is set by the consumer's caps, since `RecordVirtual` has no size option.
3. **Fan-out.** `FrameHub` keeps a bounded queue per client (60 frames for H.264, 2 for MJPEG) and drops to the next keyframe on overflow. MJPEG marks every frame as a keyframe, so this degrades to keeping the newest frame.
4. **Serve.** A local HTTP server serves a viewer page (canvas for H.264, `<img>` for MJPEG) and a `/stream` endpoint.
5. **Transport.** `adb reverse` forwards the phone's `localhost:8080` to the desktop over USB.
6. **Display.** The H.264 page fetches `/stream`, parses frames, and decodes them with a WebCodecs `VideoDecoder` drawn to a `<canvas>`. The MJPEG page points an `<img>` at `/stream`.

### Wire format (`/stream`)

H.264 (`Content-Type: application/octet-stream`):

| Field         | Size     | Notes                                              |
| ------------- | -------- | -------------------------------------------------- |
| Length        | 4 bytes  | Big-endian                                         |
| Keyframe flag | 1 byte   | `not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)`    |
| Frame data    | variable | H.264 access unit (Annex-B)                        |

MJPEG (`Content-Type: multipart/x-mixed-replace; boundary=frame`): each part is a `--frame` boundary, `Content-Type: image/jpeg`, a per-part `Content-Length`, and the JPEG bytes.

Decoder config on the H.264 page: codec `avc1.420033`, `optimizeForLatency`, no `description`. The page checks `VideoDecoder.isConfigSupported()` and logs the result before configuring.

## Layout

```
scrirror/desktop_client/
  main.py             entry point
  virtual_monitor.py  Mutter ScreenCast virtual monitor
  streamer.py         GStreamer pipeline, FrameHub, HTTP server
  video_codecs.py     H.264/MJPEG pipelines, pages, and framing
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
   uv run main.py                  # H.264 (default)
   uv run main.py --codec mjpeg    # MJPEG, jpegenc quality 70
   uv run main.py --codec mjpeg --jpeg-quality 50
   ```
   It reads the phone's resolution from `adb shell wm size` (an override size wins over the physical size), caps the long side at `MAX_LONG_SIDE`, and rounds both dimensions down to even. Adjust `MAX_LONG_SIDE`, `LANDSCAPE`, or `MONITOR_SIDE` (`"left"`/`"right"`) at the top of `main.py`. `--codec {mjpeg,h264}` selects the encoder; `--jpeg-quality` (0-100, default 70) only affects MJPEG.
3. Open `http://localhost:8080` in Chrome on the phone.
4. Tap the display to toggle fullscreen.

`adb_setup.py` runs `adb reverse tcp:8080 tcp:8080` and keeps the screen awake while on USB, restoring your original stay-awake setting on exit.

### If the script is hard-killed

Cleanup is skipped, so run these by hand. This disables stay-awake entirely rather than restoring your original value:

```sh
adb shell svc power stayon false
adb reverse --remove tcp:8080
```

## Notes and gotchas

- A loopback origin matters: WebCodecs requires a secure context. `http://localhost` and `http://127.0.0.1` via `adb reverse` both qualify; plain `http://<LAN IP>` does not. If Chrome refuses to connect to `localhost`, try `http://127.0.0.1:8080`.
- Mutter only emits frames when content changes, so an idle monitor looks frozen.
- Mutter rejects negative logical-monitor positions. For `MONITOR_SIDE = "left"`, the client puts the virtual monitor at x=0 and shifts the other logical monitors right by its width; for `"right"`, it places the virtual monitor after the primary. Other monitors keep their scale, transform, primary flag, and current mode.
- The canvas/image uses `object-fit: contain`, so a phone aspect ratio that differs from the virtual monitor shows black bars.
- The HTTP server handles only `/` (the viewer page) and `/stream`; other paths return 404.
- Without a caps filter, the virtual monitor is 1x1. Resolution, side, codec, and JPEG quality are parameters of `add_virtual_monitor(width, height, side="left", codec="h264", jpeg_quality=70)`.
- A `framerate=30/1` caps constraint likely caused a "no more input formats" error, since Mutter probably offers variable framerate. It is left out.
- `org.gnome.Mutter.*` interfaces are private and unstable. Introspect against your installed version:
  ```sh
  busctl --user introspect org.gnome.Mutter.ScreenCast /org/gnome/Mutter/ScreenCast
  ```

## Diagnostics and recovery

The client logs `ScreenCast.Session.Closed`, `DisplayConfig.MonitorsChanged` (with a timestamp), and every Gst bus EOS/ERROR/WARNING/state change. Set `SCRIRROR_DEBUG=1` to also print appsink frame stats every 2s.

A monitor layout change can make Mutter destroy the PipeWire source node without posting EOS or an error. The client checks the source node every 2s with `pw-cli` (which ships with PipeWire), and also treats an external `MonitorsChanged` as a recovery trigger: it recreates the ScreenCast session, reattaches the consumer, and re-applies the placement. EOS/errors restart the consumer once and then fall back to a full session recreate.

## Security

There is no authentication. The server binds to `127.0.0.1` and is reachable only through the USB `adb reverse` tunnel. If it is ever exposed over Wi-Fi, add a token check first.

## Roadmap

- Touch input: events back over TCP, injected via `org.gnome.Mutter.RemoteDesktop` linked to the ScreenCast session (`NotifyPointerMotionAbsolute` or the touch methods).
- Measure latency formally; try a VA-API encoder (`vah264enc`) or WebRTC if needed.
- Optional native Android app (MediaCodec) fed by the same byte stream.

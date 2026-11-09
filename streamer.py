import queue
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import gi
gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

Gst.init(None)


class ClientSubscription:
    """Per-client bounded queue with keyframe gating."""
    def __init__(self, maxsize=60):
        self.maxsize = maxsize
        self._q = queue.Queue(maxsize=maxsize)
        self.waiting_for_keyframe = True
        self._lock = threading.Lock()

    def push(self, frame: bytes, is_key: bool):
        with self._lock:
            if is_key:
                if self._q.full():
                    self._clear()
                self.waiting_for_keyframe = False
                try:
                    self._q.put_nowait((frame, is_key))
                except queue.Full:
                    pass
            else:
                if self.waiting_for_keyframe:
                    return
                if self._q.full():
                    self._clear()
                    self.waiting_for_keyframe = True
                    return
                try:
                    self._q.put_nowait((frame, is_key))
                except queue.Full:
                    self._clear()
                    self.waiting_for_keyframe = True

    def _clear(self):
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def get(self, timeout=5.0):
        return self._q.get(timeout=timeout)


class FrameHub:
    """Distributes encoded frames to connected client queues."""
    def __init__(self, max_queue_size=60):
        self._lock = threading.Lock()
        self._clients = set()
        self._max_queue_size = max_queue_size

    def subscribe(self):
        client = ClientSubscription(maxsize=self._max_queue_size)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client):
        with self._lock:
            self._clients.discard(client)

    def publish(self, frame: bytes, is_key: bool):
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            client.push(frame, is_key)


def _make_handler(hub, codec):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/":
                page_html = codec.page_html()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page_html)))
                self.end_headers()
                self.wfile.write(page_html)
            elif self.path == "/stream":
                self.send_response(200)
                self.send_header("Content-Type", codec.content_type)
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()

                client = hub.subscribe()
                try:
                    while True:
                        try:
                            frame, is_key = client.get(timeout=5.0)
                        except queue.Empty:
                            continue
                        codec.write_frame(self.wfile, frame, is_key)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    hub.unsubscribe(client)
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    return Handler


def start_http_server(hub, codec, host="127.0.0.1", port=8080):
    server = ThreadingHTTPServer((host, port), _make_handler(hub, codec))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Serving on http://{host}:{port}")
    return server


def start_pipeline(node_id, width, height, hub, codec, on_failure=None, on_playing=None):
    pipeline_description = (
        f"pipewiresrc path={node_id} ! "
        f"video/x-raw,width={width},height={height} ! "
        "videoconvert ! video/x-raw,format=I420 ! "
        f"{codec.pipeline_tail()}"
    )
    print(f"Selected codec: {codec.name}")
    print(f"GStreamer pipeline: {pipeline_description}")
    pipeline = Gst.parse_launch(pipeline_description)

    def on_sample(sink):
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        is_key = codec.is_key(buf)
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            hub.publish(bytes(info.data), is_key)
            buf.unmap(info)
        return Gst.FlowReturn.OK

    def on_bus_message(bus, message):
        message_type = message.type
        if message_type == Gst.MessageType.EOS:
            print("GStreamer bus: EOS")
            if on_failure:
                on_failure("eos")
        elif message_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            print("GStreamer bus: ERROR:", err.message)
            print("GStreamer bus: ERROR debug:", debug)
            if on_failure:
                on_failure("error")
        elif message_type == Gst.MessageType.WARNING:
            err, debug = message.parse_warning()
            print("GStreamer bus: WARNING:", err.message)
            print("GStreamer bus: WARNING debug:", debug)
        elif message_type == Gst.MessageType.STATE_CHANGED and message.src == pipeline:
            old_state, new_state, pending = message.parse_state_changed()
            print(
                "GStreamer bus: state",
                old_state.value_nick, "->", new_state.value_nick,
                "(pending", pending.value_nick + ")",
            )
            if new_state == Gst.State.PLAYING:
                sink = pipeline.get_by_name("sink")
                caps = sink.get_static_pad("sink").get_current_caps()
                print("Negotiated caps:", caps.to_string() if caps else "unknown")
                if on_playing:
                    on_playing()
        return True

    watch_id = pipeline.get_bus().add_watch(GLib.PRIORITY_DEFAULT, on_bus_message)
    pipeline.get_by_name("sink").connect("new-sample", on_sample)
    pipeline.set_state(Gst.State.PLAYING)
    return pipeline, watch_id

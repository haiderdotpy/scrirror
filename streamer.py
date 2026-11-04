import queue
import struct
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst

Gst.init(None)

PAGE_HTML = b"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
  <title>Scrirror</title>
  <style>
    * {
      margin: 0;
      padding: 0;
      box-sizing: border-box;
    }
    html, body {
      width: 100vw;
      height: 100dvh;
      overflow: hidden;
      background: #000;
      display: flex;
      justify-content: center;
      align-items: center;
    }
    canvas {
      width: 100vw;
      height: 100dvh;
      object-fit: contain;
      background: #000;
      display: block;
      touch-action: manipulation;
    }
  </style>
</head>
<body>
  <canvas id="display"></canvas>
  <script>
    const canvas = document.getElementById("display");
    const ctx = canvas.getContext("2d");

    canvas.addEventListener("click", () => {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(() => {});
      } else {
        document.exitFullscreen().catch(() => {});
      }
    });

    if (typeof VideoDecoder === "undefined") {
      console.error("WebCodecs VideoDecoder is not supported in this browser.");
    }

    const decoder = new VideoDecoder({
      output(frame) {
        if (canvas.width !== frame.displayWidth || canvas.height !== frame.displayHeight) {
          canvas.width = frame.displayWidth;
          canvas.height = frame.displayHeight;
        }
        ctx.drawImage(frame, 0, 0);
        frame.close();
      },
      error(e) {
        console.error("VideoDecoder error:", e);
      }
    });

    decoder.configure({
      codec: "avc1.42E028",
      optimizeForLatency: true
    });

    async function startStream() {
      try {
        const response = await fetch("/stream");
        if (!response.ok || !response.body) {
          throw new Error("HTTP error " + response.status);
        }

        const reader = response.body.getReader();
        let buffer = new Uint8Array(0);
        let bufferOffset = 0;
        let hasReceivedKeyframe = false;
        let timestamp = 0;
        const timestampStep = 33333;

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;

          const remaining = buffer.length - bufferOffset;
          const newBuf = new Uint8Array(remaining + value.length);
          newBuf.set(buffer.subarray(bufferOffset), 0);
          newBuf.set(value, remaining);
          buffer = newBuf;
          bufferOffset = 0;

          while (buffer.length - bufferOffset >= 5) {
            const view = new DataView(buffer.buffer, buffer.byteOffset + bufferOffset, 4);
            const frameLen = view.getUint32(0, false);
            const isKey = buffer[bufferOffset + 4] === 1;

            if (buffer.length - bufferOffset < 5 + frameLen) {
              break;
            }

            const frameData = buffer.subarray(bufferOffset + 5, bufferOffset + 5 + frameLen);
            bufferOffset += 5 + frameLen;

            if (!hasReceivedKeyframe) {
              if (!isKey) {
                continue;
              }
              hasReceivedKeyframe = true;
            }

            try {
              const chunk = new EncodedVideoChunk({
                type: isKey ? "key" : "delta",
                timestamp: timestamp,
                data: frameData
              });
              timestamp += timestampStep;
              decoder.decode(chunk);
            } catch (err) {
              console.error("Error creating or decoding EncodedVideoChunk:", err);
            }
          }
        }
      } catch (err) {
        console.error("Stream error:", err);
        setTimeout(startStream, 1000);
      }
    }

    startStream();
  </script>
</body>
</html>
"""

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


def _make_handler(hub): 
    class Handler(BaseHTTPRequestHandler): 
        def do_GET(self): 
            if self.path == "/": 
                self.send_response(200) 
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(PAGE_HTML)))
                self.end_headers()
                self.wfile.write(PAGE_HTML) 
            elif self.path == "/stream": 
                self.send_response(200) 
                self.send_header("Content-Type", "application/octet-stream")
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
                        wire_data = struct.pack(">IB", len(frame), 1 if is_key else 0) + frame
                        self.wfile.write(wire_data)
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

def start_http_server(hub, host="127.0.0.1", port=8080): 
    server = ThreadingHTTPServer((host, port), _make_handler(hub)) 
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Serving on http://{host}:{port}")
    return server 

def start_pipeline(node_id, width, height, hub): 
    pipeline = Gst.parse_launch(
        f"pipewiresrc path={node_id} ! "
        f"video/x-raw,width={width},height={height} ! "
        "videoconvert ! video/x-raw,format=I420 ! "
        "x264enc tune=zerolatency speed-preset=ultrafast bframes=0 key-int-max=30 ! "
        "video/x-h264,profile=baseline ! "
        "h264parse config-interval=-1 ! "
        "video/x-h264,stream-format=byte-stream,alignment=au ! "
        "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
    )

    def on_sample(sink): 
        sample = sink.emit("pull-sample")
        buf = sample.get_buffer()
        is_key = not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok: 
            hub.publish(bytes(info.data), is_key)
            buf.unmap(info)
        return Gst.FlowReturn.OK

    pipeline.get_by_name("sink").connect("new-sample", on_sample)
    pipeline.set_state(Gst.State.PLAYING)
    return pipeline

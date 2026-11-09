import struct

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst


H264_PAGE_HTML = b"""<!DOCTYPE html>
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

    const decoderConfig = {
      codec: "avc1.420033",
      optimizeForLatency: true
    };

    let decoderConfigured = false;

    async function startStream() {
      try {
        if (!decoderConfigured) {
          const support = await VideoDecoder.isConfigSupported(decoderConfig);
          console.log("VideoDecoder config support:", support);
          if (!support.supported) {
            console.error("Unsupported decoder config:", decoderConfig);
            return;
          }
          decoder.configure(decoderConfig);
          decoderConfigured = true;
        }

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

MJPEG_PAGE_HTML = b"""<!DOCTYPE html>
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
    }
    img {
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
  <img id="display" src="/stream" alt="">
  <script>
    const img = document.getElementById("display");

    img.addEventListener("click", () => {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(() => {});
      } else {
        document.exitFullscreen().catch(() => {});
      }
    });

    let wakeLock = null;

    async function requestWakeLock() {
      if (!("wakeLock" in navigator) || document.visibilityState !== "visible") {
        return;
      }
      try {
        wakeLock = await navigator.wakeLock.request("screen");
      } catch (err) {
        console.error("Wake lock error:", err);
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") {
        requestWakeLock();
      }
    });

    requestWakeLock();
  </script>
</body>
</html>
"""


class Codec:
    name = None
    content_type = None
    queue_depth = 60

    def pipeline_tail(self):
        raise NotImplementedError

    def page_html(self):
        raise NotImplementedError

    def write_frame(self, wfile, frame, is_key):
        raise NotImplementedError

    def is_key(self, buf):
        raise NotImplementedError


class H264Codec(Codec):
    name = "h264"
    content_type = "application/octet-stream"
    queue_depth = 60

    def pipeline_tail(self):
        return (
            "x264enc tune=zerolatency speed-preset=ultrafast bframes=0 key-int-max=30 ! "
            "video/x-h264,profile=baseline ! "
            "h264parse config-interval=-1 ! "
            "video/x-h264,stream-format=byte-stream,alignment=au ! "
            "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )

    def page_html(self):
        return H264_PAGE_HTML

    def write_frame(self, wfile, frame, is_key):
        wfile.write(struct.pack(">IB", len(frame), 1 if is_key else 0) + frame)

    def is_key(self, buf):
        return not buf.has_flags(Gst.BufferFlags.DELTA_UNIT)


class MjpegCodec(Codec):
    name = "mjpeg"
    content_type = "multipart/x-mixed-replace; boundary=frame"
    queue_depth = 2

    def __init__(self, quality=70):
        self.quality = quality

    def pipeline_tail(self):
        return (
            f"videoconvert ! jpegenc quality={self.quality} ! "
            "appsink name=sink emit-signals=true max-buffers=1 drop=true sync=false"
        )

    def page_html(self):
        return MJPEG_PAGE_HTML

    def write_frame(self, wfile, frame, is_key):
        wfile.write(b"--frame\r\n")
        wfile.write(b"Content-Type: image/jpeg\r\n")
        wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
        wfile.write(frame)
        wfile.write(b"\r\n")

    def is_key(self, buf):
        return True


def create_codec(name, jpeg_quality=70):
    if name == "h264":
        return H264Codec()
    if name == "mjpeg":
        return MjpegCodec(quality=jpeg_quality)
    raise ValueError(f"Unknown codec: {name}")

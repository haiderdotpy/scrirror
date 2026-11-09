import argparse

from adb_setup import get_phone_resolution, normalize_resolution, phone_link
from virtual_monitor import add_virtual_monitor

MAX_LONG_SIDE = 2560
LANDSCAPE = True
MONITOR_SIDE = "left"
FALLBACK_WIDTH, FALLBACK_HEIGHT = 1920, 1080


def _jpeg_quality(value):
    quality = int(value)
    if not 0 <= quality <= 100:
        raise argparse.ArgumentTypeError("jpeg quality must be between 0 and 100")
    return quality


def parse_args():
    parser = argparse.ArgumentParser(description="Stream a virtual monitor to a phone.")
    parser.add_argument(
        "--codec",
        choices=("mjpeg", "h264"),
        default="h264",
        help="video codec for the stream (default: h264)",
    )
    parser.add_argument(
        "--jpeg-quality",
        type=_jpeg_quality,
        default=70,
        help="JPEG quality for --codec mjpeg, 0-100 (default: 70)",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    detected = get_phone_resolution(landscape=LANDSCAPE)
    if detected is None:
        print(
            "Warning: could not detect phone resolution; "
            f"falling back to {FALLBACK_WIDTH}x{FALLBACK_HEIGHT}"
        )
        width, height = FALLBACK_WIDTH, FALLBACK_HEIGHT
    else:
        print(f"Detected phone resolution: {detected[0]}x{detected[1]}")
        width, height = normalize_resolution(
            detected[0], detected[1], max_long_side=MAX_LONG_SIDE
        )

    print(f"Normalized virtual monitor resolution: {width}x{height}")
    with phone_link(8080):
        add_virtual_monitor(
            width,
            height,
            side=MONITOR_SIDE,
            codec=args.codec,
            jpeg_quality=args.jpeg_quality,
        )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import subprocess, time, os
from PIL import Image
from datetime import datetime, timedelta
from io import BytesIO
from collections import deque

# Screenshot path
SCREENSHOT_PATH="/tmp/blank_slots_screenshots"


# Screen config
MONITOR_WIDTH = 1920
MONITOR_HEIGHT = 1080
OFFSET_X = 0
OFFSET_Y = 0

# Time settings
BURST = 0.5
SCREENSHOT_INTERVAL = 2
WINDOW = timedelta(minutes=1)

# Thresholds
BLANK_SLOT = 99.0
POSSIBLE_BLANK_SLOT = 95.0
MIN_BLANK_EVENTS = 2

# Runtime state
interval = SCREENSHOT_INTERVAL
blank_events = deque()

# Capture frame
def take_screenshot():
    now = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{SCREENSHOT_PATH}/screen_{now}.png"
    # cmd = [
    #     "ffmpeg",
    #     "-f", "gdigrab",
    #     "-video_size", f"{MONITOR_WIDTH}x{MONITOR_HEIGHT}",
    #     "-i", "desktop",
    #     "-frames:v", "1",
    #     "-f", "image2pipe",
    #     "-vcodec", "png",
    #     "pipe:1"
    # ]
    cmd = [
    "ffmpeg",
    "-f", "x11grab",
    "-video_size", f"{MONITOR_WIDTH}x{MONITOR_HEIGHT}",
    "-i", os.getenv("DISPLAY", ":0.0"),
            "-frames:v", "1",
        "-f", "image2pipe",
        "-vcodec", "png",
        "pipe:1"
    ]

    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    return result, filename

# Analyze image content
def analyze_image(image):
    img = Image.open(BytesIO(image.stdout)).resize((240, 135), resample=Image.BILINEAR)
    pixels = list(img.getdata())
    total = len(pixels)
    black = sum(1 for p in pixels if sum(p[:3]) / 3 < 30)
    white = sum(1 for p in pixels if sum(p[:3]) / 3 > 240)
    percent_black = (black / total) * 100
    percent_white = (white / total) * 100

    blank_pct = max(percent_black, percent_white)
    if blank_pct >= BLANK_SLOT:
        return "critical", percent_black, percent_white, img
    elif blank_pct >= POSSIBLE_BLANK_SLOT:
        return "warning", percent_black, percent_white, img
    return "none", percent_black, percent_white, img

# Main loop
def monitor_screen():
    global interval
    global blank_events
    while True:
        image, filepath = take_screenshot()
        if not image:
            continue

        status, black_pct, white_pct, img = analyze_image(image)
        now = datetime.now()
        print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Black: {black_pct:.2f}%, White: {white_pct:.2f}%, Status: {status}")

        if status in ("critical", "warning"):
            blank_events.append((now, status, img, filepath))
            blank_events = deque([e for e in blank_events if now - e[0] <= WINDOW])
            count_blanks = sum(1 for e in blank_events if e[1] in ("critical", "warning"))

            if count_blanks >= MIN_BLANK_EVENTS:
                confirmed_status = "CRITICAL" if any(e[1] == "critical" for e in blank_events) else "WARNING"
                print(f"[{confirmed_status}] Blank slot confirmed with {count_blanks} events in last minute")
                last_img = blank_events[-1][2]
                last_img.save(blank_events[-1][3])
                blank_events.clear()
                interval = SCREENSHOT_INTERVAL
            else:
                interval = BURST
        else:
            interval = SCREENSHOT_INTERVAL

        time.sleep(interval)

if __name__ == "__main__":
    monitor_screen()

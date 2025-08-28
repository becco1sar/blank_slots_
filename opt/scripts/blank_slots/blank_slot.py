#!/usr/bin/env python3
# Ubuntu 22 / X11
# - Detect monitors via xrandr (name, x, y, width, height)
# - For each monitor: detect blank slots (>=97% black OR white)
# - Check every second
# - Log duration of blank slot
# - Plus: syslog events on state changes

import subprocess
import time
import logging
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
from Xlib import display, X

# ---------- Tuning ----------
BASE_SAMPLE_SEC = 1.0                 # check every second
BLANK_PCT_MIN = 97.0                  # >=97% considered blank
BLACK_THRESH = 15                     # gray < 15 -> black
WHITE_THRESH = 240                    # gray > 240 -> white
DEBOUNCE_CLEAR_OK_FRAMES = 2          # to end a 'continuous blank' state
DOWNSCALE = 0.5                       # analyze at 50% size

# ---------- Syslog (added) ----------
import syslog
TOOL_NAME = "blankwatch"
LOG_FILE = "/var/log/blankwatch.log"

#init logging
logging.basicConfig(
    filename=LOG_FILE,
    filemode='a',
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - Blankwatch - %(message)s"
)
def create_log_file():
    try:
        open(LOG_FILE, 'x');
        print(f"log file created in {LOG_FILE}")
    except FileExistsError as fileExists:
        print(fileExists)
                    
def log_to_logfile(log):
    try:
        with open(LOG_FILE, 'a') as file:
            file.write(log)
    except FileNotFoundError as fileNotFound:
        print(fileNotFound)
    except Exception as e:
        print(e) 

# ---------- Helpers ----------
def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def iso(dt: float) -> str:
    # ISO8601 timestamp for logs
    import datetime as _dt
    return _dt.datetime.fromtimestamp(dt).isoformat()

def get_monitors() -> List[Dict]:
    out = subprocess.check_output(["xrandr", "--query"], text=True)
    mons = []
    for line in out.splitlines():
        if " connected" in line and ("+" in line) and ("x" in line):
            parts = line.split()
            name = parts[0]
            geom_token = None
            for p in parts:
                if "x" in p and "+" in p:
                    try:
                        wh, xy = p.split("+", 1)
                        w, h = wh.split("x")
                        x_str, y_str = xy.split("+")
                        w = int(w); h = int(h); x = int(x_str); y = int(y_str)
                        geom_token = (w, h, x, y)
                        break
                    except Exception:
                        continue
            if geom_token:
                w, h, x, y = geom_token
                mons.append({"name": name, "x": x, "y": y, "w": w, "h": h})
    return mons

def capture_gray(x: int, y: int, w: int, h: int) -> np.ndarray:
    dsp = display.Display()
    root = dsp.screen().root
    raw = root.get_image(x, y, w, h, X.ZPixmap, 0xffffffff)
    buf = np.frombuffer(raw.data, dtype=np.uint8).reshape((h, w, 4))  # BGRA
    dsp.close()
    gray = buf[:, :, :3].mean(axis=2).astype(np.uint8)
    if DOWNSCALE != 1.0:
        if abs(DOWNSCALE - 0.5) < 1e-6:
            h2 = h // 2
            w2 = w // 2
            gray = gray[:h2*2, :w2*2] \
                     .reshape(h2, 2, w2, 2).mean(axis=(1, 3)).astype(np.uint8)
        else:
            step = max(1, int(1.0 / DOWNSCALE))
            gray = gray[::step, ::step]
    return gray

def blank_metrics(gray: np.ndarray) -> Tuple[float, float, bool]:
    """Return (black_pct, white_pct, is_blank_now)."""
    total = gray.size
    if total == 0:
        return 0.0, 0.0, False
    black_pct = (gray < BLACK_THRESH).sum() * 100.0 / total
    white_pct = (gray > WHITE_THRESH).sum() * 100.0 / total
    blank_now = (black_pct >= BLANK_PCT_MIN) or (white_pct >= BLANK_PCT_MIN)
    return black_pct, white_pct, blank_now

# ---------- Per-monitor state ----------
@dataclass
class Debounce:
    in_blank: bool = False
    clear_left: int = 0
    start_time: float = 0.0
    last_black_pct: float = 0.0
    last_white_pct: float = 0.0

# ---------- Main loop ----------
def main():
    create_log_file()

    print(f"[{ts()}] Blank detector started (Ubuntu 22 / X11). Sample every {BASE_SAMPLE_SEC}s")
    logging.info("service_started sample_sec=%s" % BASE_SAMPLE_SEC)

    monitors = get_monitors()
    if not monitors:
        msg = "No connected monitors found via xrandr."
        print(f"[{ts()}] {msg}")
        logging.warning(msg)
        return

    for i, m in enumerate(monitors):
        line = f"mon{i}: {m['name']} {m['w']}x{m['h']} @ ({m['x']},{m['y']})"
        print(" ", line)
        logging.info("monitor_detected " + line)

    debounces: Dict[int, Debounce] = {i: Debounce() for i in range(len(monitors))}

    while True:
        for i, m in enumerate(monitors):
            geom = (m["x"], m["y"], m["w"], m["h"])
            try:
                gray = capture_gray(*geom)
                black_pct, white_pct, blank_now = blank_metrics(gray)
                # Console debug each frame
                print(f"[DEBUG] mon{i}: black%={black_pct:.2f}, white%={white_pct:.2f}, thr={BLANK_PCT_MIN}")
            except Exception as e:
                err = f"mon{i}: capture error: {e}"
                print(f"[{ts()}] {err}")
                logging.error("error " + err)
                continue

            db = debounces[i]
            now = time.time()
            mon_name = m.get("name", f"mon{i}")

            if blank_now:
                if not db.in_blank:
                    # Rising edge
                    db.in_blank = True
                    db.clear_left = DEBOUNCE_CLEAR_OK_FRAMES
                    db.start_time = now
                    db.last_black_pct = black_pct
                    db.last_white_pct = white_pct

                    print(f"[{ts()}] mon{i}: BLANK detected (start)")
                    # Syslog: state change -> detected=1
                    logging.critical(
                        f'monitor="{mon_name}" blank_slot_timestamp = "{iso(now)}" blank_slot_detected=1 '
                    )
                else:
                    # still blank
                    db.last_black_pct = black_pct
                    db.last_white_pct = white_pct
                    print(f"[DEBUG] mon{i}: still blank (duration {now - db.start_time:.1f}s)")
            else:
                if db.in_blank:
                    db.clear_left -= 1
                    print(f"[DEBUG] mon{i}: clear_left={db.clear_left}")
                    if db.clear_left <= 0:
                        duration = now - db.start_time
                        db.in_blank = False

                        print(f"[{ts()}] mon{i}: BLANK ended; blank_slot_duration={duration:.1f}s")
                        # Syslog: state cleared + duration + set detected=0
                        logging.info(
                            f'monitor="{mon_name}" '
                            f'blank_slot_detected=0 blank_slot_timestamp = "{iso(now)}" blank_slot_duration={duration:.1f}s'
                        )
                # else: remain OK

        time.sleep(BASE_SAMPLE_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        logging.info("Blankwatch stopped manually")

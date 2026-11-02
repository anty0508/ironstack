"""Screenshot capture + reading for the F8 hotkeys.

Capture:
  - `grab_screen_jpeg()`        one full-monitor shot (monitor under the cursor).
  - `capture_scroll_frames()`   scrolls the pane under the cursor with the mouse
                                wheel and captures overlapping frames until the
                                content stops moving, then scrolls back.

Reading:
  - `read_screens(jpegs)`       sends the frames to a vision model, which returns
                                the text / code / problem (and a precise
                                description of anything drawn) as one continuous
                                text, plus the images to attach to the answer
                                (a zoomed crop of the main content for a single
                                screenshot)."""

import base64
import ctypes
import json
import time
from ctypes import wintypes

import mss
import numpy as np
from PySide6 import QtCore, QtGui

from config import OPENAI_API_KEY, SCREENSHOT_MODEL
from logsetup import get_logger

# Wide enough that small code stays legible, small enough to upload quickly.
MAX_WIDTH = 2560
JPEG_QUALITY = 90
MAX_EXTRACT_TOKENS = 30000
MAX_ANSWER_IMAGES = 6           # images attached to the answer call

# Auto-scroll capture
MAX_SCROLL_FRAMES = 30
SCROLL_TIME_LIMIT = 90.0        # seconds for the whole capture
CALIBRATION_NOTCHES = 3         # first scroll, used to learn pixels per notch
TARGET_SHIFT = 0.7              # scroll about 70% of the pane per step (overlap)
MIN_BAND_ROWS = 24              # fewer changed rows than this is noise (caret, clock)
_PIXEL_DIFF = 24                # a pixel counts as changed above this gray difference
_ROW_MIN_PIXELS = 3             # a row counts as changed with at least this many pixels
_ROW_GAP = 30                   # changed rows closer than this belong to one block
_COL_GAP = 40
MOVE_POLLS = 12                 # x 0.1s: how long to wait for the screen to start moving
MAX_ALIGN_ERROR = 8.0           # mean gray-level difference accepted as aligned
SETTLE_SECONDS = 0.3
WHEEL_DELTA = 120
_SIG_BUCKETS = 16

# A crop covering more than this share of the screen is not worth sending.
MAX_CROP_AREA = 0.85

EXTRACT_PROMPT = (
    "These images are screenshots taken during a live job interview. If there are "
    "several, they are consecutive scroll positions of ONE page, in order, and "
    "they overlap: join them into one continuous text and never repeat the "
    "overlapping part.\n"
    "Write down what someone would need in order to answer correctly, without "
    "seeing the images:\n"
    "- Code: copy ALL of it exactly as shown, every line, the same indentation, "
    "nothing summarised or skipped. Add the language and file name if visible.\n"
    "- A test, quiz, coding problem or question: copy the FULL text, including "
    "the statement, constraints, input/output examples, every answer option and "
    "any function signature.\n"
    "- Anything that is a picture rather than text (a puzzle, matrix, chart, "
    "diagram, UI): describe it precisely. List every shape or item and where it "
    "is; for a grid go row by row and cell by cell; describe every answer option "
    "one by one (A, B, C...), with colours, counts, positions and orientation.\n"
    "- An error, log or terminal: copy the exact text.\n"
    "- Otherwise describe in a few lines what is on the screen.\n"
    "Ignore browser tabs, menus, taskbars and other window chrome.\n"
    "Also give `region`: for a SINGLE image, the box around the main thing to "
    "solve or read (the whole puzzle with its answer options, the problem "
    "statement, or the code area), as fractions of the image: x and y of the "
    "top-left corner, then w and h, with a small margin. Use null when there are "
    "several images or the main content already fills most of the screen."
)

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "region": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"}, "y": {"type": "number"},
                        "w": {"type": "number"}, "h": {"type": "number"},
                    },
                    "required": ["x", "y", "w", "h"],
                    "additionalProperties": False,
                },
            ],
        },
    },
    "required": ["content", "region"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------- capture ----

def _cursor_pos():
    pt = wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


def _monitor_under_cursor(sct):
    monitors = sct.monitors[1:] or [sct.monitors[0]]
    target = monitors[0]
    try:
        cx, cy = _cursor_pos()
        for m in monitors:
            if (m["left"] <= cx < m["left"] + m["width"]
                    and m["top"] <= cy < m["top"] + m["height"]):
                return m
    except Exception:
        get_logger().exception("screenshot: cursor position unavailable")
    return target


def _grab_image():
    """Full monitor under the cursor -> (QImage, gray ndarray of the same size)."""
    with mss.mss() as sct:
        shot = sct.grab(_monitor_under_cursor(sct))
        raw = bytes(shot.bgra)
        image = QtGui.QImage(raw, shot.width, shot.height,
                             shot.width * 4, QtGui.QImage.Format_RGB32).copy()
    gray = np.frombuffer(raw, np.uint8).reshape(shot.height, shot.width, 4)[:, :, 1]
    return image, gray


def _to_jpeg(image):
    if image.width() > MAX_WIDTH:
        image = image.scaledToWidth(MAX_WIDTH, QtCore.Qt.SmoothTransformation)
    data = QtCore.QByteArray()
    buf = QtCore.QBuffer(data)
    buf.open(QtCore.QIODevice.WriteOnly)
    image.save(buf, "JPEG", JPEG_QUALITY)
    buf.close()
    return bytes(data)


def grab_screen_jpeg():
    """Capture the full monitor under the mouse cursor as JPEG bytes."""
    image, _ = _grab_image()
    return _to_jpeg(image)


def _wait_keys_released(timeout=2.0):
    """Shift + wheel scrolls sideways, so wait until the hotkey is let go."""
    user32 = ctypes.windll.user32
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(user32.GetAsyncKeyState(vk) & 0x8000
                   for vk in (0x10, 0x11, 0x12, 0x77)):   # Shift Ctrl Alt F8
            return
        time.sleep(0.03)


def _wheel(notches):
    """Mouse wheel at the cursor: negative = scroll down, positive = up."""
    delta = int(notches) * WHEEL_DELTA
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, delta & 0xFFFFFFFF, 0)


def _page_key(down=True):
    """Press Page Down (or Page Up) once. It goes to the focused control."""
    user32 = ctypes.windll.user32
    vk = 0x22 if down else 0x21          # VK_NEXT / VK_PRIOR
    user32.keybd_event(vk, 0, 0x0001, 0)           # key down (extended key)
    user32.keybd_event(vk, 0, 0x0001 | 0x0002, 0)  # key up


def _row_signatures(gray):
    """Each row reduced to a few bucket averages, so rows compare cheaply."""
    h, w = gray.shape
    buckets = min(_SIG_BUCKETS, max(1, w // 4))
    usable = (w // buckets) * buckets
    return gray[:, :usable].reshape(h, buckets, -1).mean(axis=2, dtype=np.float32)


def _runs(mask, gap):
    """Blocks of True values in a 1-D mask as inclusive (start, end) pairs, with
    gaps shorter than `gap` bridged."""
    idx = np.nonzero(mask)[0]
    if idx.size == 0:
        return []
    runs = []
    start = prev = int(idx[0])
    for i in idx[1:]:
        i = int(i)
        if i - prev > gap:
            runs.append((start, prev))
            start = i
        prev = i
    runs.append((start, prev))
    return runs


def _changed_pixels(a, b):
    return np.abs(a.astype(np.int16) - b.astype(np.int16)) > _PIXEL_DIFF


def _noise_rects(a, b):
    """Rectangles (row0, row1, col0, col1) of the screen that changed between two
    frames taken with nothing pressed: clocks, videos, animated ads."""
    changed = _changed_pixels(a, b)
    h, w = changed.shape
    rects = []
    for r0, r1 in _runs(changed.sum(axis=1) >= 1, 10):
        for c0, c1 in _runs(changed[r0:r1 + 1].sum(axis=0) >= 1, 10):
            rects.append((max(0, r0 - 8), min(h - 1, r1 + 8),
                          max(0, c0 - 8), min(w - 1, c1 + 8)))
    return rects


def _find_pane(a, b, cursor=None, ignore=()):
    """The block of the screen that scrolled between frames a and b, as
    (row0, row1, col0, col1), or None. Prefers the block under the mouse cursor,
    else the one with the most changed pixels. `ignore` lists rectangles that
    change on their own and are left out."""
    changed = _changed_pixels(a, b)
    for r0, r1, c0, c1 in ignore:
        changed[r0:r1 + 1, c0:c1 + 1] = False
    rows = changed.sum(axis=1) >= _ROW_MIN_PIXELS
    blocks = []
    for r0, r1 in _runs(rows, _ROW_GAP):
        if int(rows[r0:r1 + 1].sum()) >= MIN_BAND_ROWS:
            blocks.append((r0, r1, int(changed[r0:r1 + 1].sum())))
    if not blocks:
        return None
    pick = None
    if cursor is not None:
        for blk in blocks:
            if blk[0] - 20 <= cursor[1] <= blk[1] + 20 and (pick is None or blk[2] > pick[2]):
                pick = blk
    if pick is None:
        pick = max(blocks, key=lambda blk: blk[2])
    r0, r1, _ = pick

    cols = changed[r0:r1 + 1].sum(axis=0) >= _ROW_MIN_PIXELS
    col_runs = _runs(cols, _COL_GAP)
    if not col_runs:
        return None
    col = None
    if cursor is not None:
        for run in col_runs:
            if run[0] - 20 <= cursor[0] <= run[1] + 20:
                col = run
    if col is None:
        col = max(col_runs, key=lambda run: int(changed[r0:r1 + 1, run[0]:run[1] + 1].sum()))
    return r0, r1, col[0], col[1]


def _pane_view(gray, roi):
    r0, r1, c0, c1 = roi
    return gray[r0:r1 + 1, c0:c1 + 1]


def _pane_moved(a, b, roi):
    """Did the content inside the pane change between two frames?"""
    changed = _changed_pixels(_pane_view(a, roi), _pane_view(b, roi))
    return int((changed.sum(axis=1) >= _ROW_MIN_PIXELS).sum()) >= MIN_BAND_ROWS


def _measure_shift(a, b, roi):
    """How many pixels frame b scrolled relative to frame a, measured inside the
    pane only (sidebars, panels and other areas do not matter). None when the two
    frames cannot be aligned."""
    sa = _row_signatures(_pane_view(a, roi))
    sb = _row_signatures(_pane_view(b, roi))
    height = sa.shape[0]
    if height < 8:
        return None
    best, best_err = None, None
    for s in range(1, height - 4):
        err = float(np.abs(sa[s:] - sb[:height - s]).mean())
        if best_err is None or err < best_err:
            best, best_err = s, err
    if best is None or best_err > MAX_ALIGN_ERROR:
        return None
    return best


def _cursor_in_frame():
    """Mouse cursor position relative to the captured monitor, or None."""
    try:
        with mss.mss() as sct:
            m = _monitor_under_cursor(sct)
        cx, cy = _cursor_pos()
        return cx - m["left"], cy - m["top"]
    except Exception:
        return None


def capture_scroll_frames(on_status=None, grab=None, wheel=None, page=None,
                          cursor=None, sleep=time.sleep, wait_keys=True):
    """Scroll down page by page and capture overlapping frames until the content
    stops moving; then scroll back up. Returns JPEG bytes, one per frame, in order.

    Page Down is tried first (about one pane per press). It goes to the focused
    control, so when it moves nothing the mouse wheel at the cursor is tried
    instead, with its step size measured from the frames.

    The first movement shows WHICH block of the screen scrolls (the one under the
    cursor, else the biggest). From then on only that block is watched, so a
    terminal, timer, ad or blinking caret elsewhere on the screen can neither keep
    the capture going nor throw off the alignment. After each action it waits for
    the block to start moving (smooth scrolling and remote desktop lag can delay
    it) and then to stop; it ends when neither Page Down nor the wheel moves it.
    Nothing is logged while capturing (the log may be visible on the screen being
    captured); one summary line follows at the end.
    `grab`, `wheel`, `page`, `cursor` and `sleep` can be replaced (for tests)."""
    grab = grab or _grab_image
    wheel = wheel or _wheel
    page = page or _page_key
    cursor = cursor or _cursor_in_frame
    log = get_logger()
    say = on_status or (lambda text: None)

    if wait_keys:
        _wait_keys_released()
    cursor_pos = cursor()
    roi = None             # the scrolled block, once the first movement shows it

    # With nothing pressed, see which parts of the screen change by themselves
    # (clock, video, ad); they must not be taken for the scrolled pane.
    first_image, prev = grab()
    sleep(0.4)
    noise = _noise_rects(prev, grab()[1])

    def moved(prev, gray):
        if roi is None:
            return _find_pane(prev, gray, cursor_pos, noise) is not None
        return _pane_moved(prev, gray, roi)

    def region_diff(a, b):
        if roi is not None:
            a, b = _pane_view(a, roi), _pane_view(b, roi)
        return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())

    def wait_for_move(prev):
        """-> (image, gray, did_it_move)"""
        sleep(SETTLE_SECONDS)
        image, gray = grab()
        for _ in range(MOVE_POLLS):
            if moved(prev, gray):
                break
            sleep(0.1)
            image, gray = grab()
        else:
            return image, gray, False
        for _ in range(12):                      # moving: wait until it settles
            sleep(0.15)
            image2, gray2 = grab()
            same = region_diff(gray, gray2) < 0.3
            image, gray = image2, gray2
            if same:
                break
        return image, gray, moved(prev, gray)

    started = time.time()
    frames = [_to_jpeg(first_image)]
    method = "page"
    presses = 0            # Page Down presses that moved the screen
    scrolled = 0           # wheel notches sent
    notches = CALIBRATION_NOTCHES
    reason = "max frames"
    try:
        while len(frames) < MAX_SCROLL_FRAMES:
            if time.time() - started > SCROLL_TIME_LIMIT:
                reason = "time limit"
                break
            result = None
            for how in (["page", "wheel"] if method == "page" else ["wheel"]):
                if how == "page":
                    page(True)
                    presses += 1
                else:
                    wheel(-notches)
                    scrolled += notches
                image, gray, did_move = wait_for_move(prev)
                if did_move and roi is None:
                    roi = _find_pane(prev, gray, cursor_pos, noise)
                    did_move = roi is not None
                if did_move:
                    result = (how, image, gray)
                    break
                if how == "page":
                    presses -= 1                 # that press did nothing
            if result is None:
                reason = "end (the pane no longer moves)"
                break
            how, image, gray = result
            used = notches
            method = how

            shift = _measure_shift(prev, gray, roi)
            frames.append(_to_jpeg(image))
            say(f"[screen] scrolling… {len(frames)} frames")
            log.debug("scroll capture: frame %d via %s, pane %s, shift %s",
                      len(frames), how, roi, shift)
            if shift is None:
                reason = "could not align"      # keep the frame; stop here
                break
            if method == "wheel":
                pane = roi[1] - roi[0] + 1
                notches = max(1, int(round(TARGET_SHIFT * pane / (shift / used))))
            prev = gray
    finally:
        # put the page back where it was (the top, if that is where it started)
        for _ in range(presses):
            page(False)
            sleep(0.15)
        while scrolled > 0:
            step = min(scrolled, 10)
            wheel(step)
            scrolled -= step
            sleep(0.04)
    log.info("scroll capture finished: %d frames in %.1fs via %s, pane %s, stopped: %s",
             len(frames), time.time() - started, method, roi, reason)
    return frames


# ---------------------------------------------------------------- reading ----

class ScreenRead:
    """What the vision model read: the text, and the images to attach to the
    answer call."""

    def __init__(self, text, images):
        self.text = text
        self.images = images


def _crop(jpeg, region):
    """Crop a JPEG to `region` (fractions of the image); None when the region is
    missing, invalid, or covers most of the screen."""
    try:
        x, y, w, h = (float(region[k]) for k in ("x", "y", "w", "h"))
    except Exception:
        return None
    x, y = max(0.0, x), max(0.0, y)
    w, h = min(w, 1.0 - x), min(h, 1.0 - y)
    if w < 0.05 or h < 0.05 or w * h > MAX_CROP_AREA:
        return None
    image = QtGui.QImage.fromData(jpeg)
    if image.isNull():
        return None
    rect = QtCore.QRect(int(x * image.width()), int(y * image.height()),
                        int(w * image.width()), int(h * image.height()))
    return _to_jpeg(image.copy(rect))


def _sample(frames, limit):
    """At most `limit` frames, evenly spread, always keeping first and last."""
    if len(frames) <= limit:
        return list(frames)
    idx = sorted({round(i * (len(frames) - 1) / (limit - 1)) for i in range(limit)})
    return [frames[i] for i in idx]


def _data_url(jpeg):
    return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")


def read_screens(jpegs):
    """Read one or more screenshots with the vision model and return a ScreenRead."""
    from openai import OpenAI
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    content = [{"type": "input_text", "text": EXTRACT_PROMPT}]
    for jpeg in jpegs:
        content.append({"type": "input_image", "detail": "high",
                        "image_url": _data_url(jpeg)})
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.responses.create(
        model=SCREENSHOT_MODEL,
        input=[{"role": "user", "content": content}],
        reasoning={"effort": "minimal"},
        text={"format": {"type": "json_schema", "name": "screen_read",
                         "schema": _EXTRACT_SCHEMA, "strict": True}},
        max_output_tokens=MAX_EXTRACT_TOKENS,
    )
    raw = (resp.output_text or "").strip()
    try:
        data = json.loads(raw)
        text = (data.get("content") or "").strip()
        region = data.get("region")
    except Exception:
        get_logger().exception("screenshot: reply was not JSON; using it as text")
        text, region = raw, None

    if len(jpegs) == 1:
        crop = _crop(jpegs[0], region) if region else None
        images = [crop, jpegs[0]] if crop else [jpegs[0]]
    else:
        images = _sample(jpegs, MAX_ANSWER_IMAGES)
    return ScreenRead(text, images)

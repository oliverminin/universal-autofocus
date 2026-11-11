"""
universal_autofocus_v1.py - ONE autofocus function for any microscope, in one file.
Pengjie Wang Lab, UIUC.

ANY MICROSCOPE: describe your hardware with two small objects and call the function:

    from universal_autofocus_v1 import autofocus, FocusDrive, FrameSource

    class MyDrive(FocusDrive):
        units = "um"
        max_travel = 50.0                                   # safety: how far one autofocus may move each way
        def move_by(self, delta, fast=False):
            stage.move_relative(delta); stage.wait()        # TODO your focus motor

    class MyFrames(FrameSource):
        def frames(self):                                   # [(time.time() of the picture, sharpness), ...]
            return camera.new_frames()                      # TODO your camera

    result = autofocus(MyDrive(), MyFrames())
    print(result.message, result.quality)

  Every setting is in units that mean the same on any microscope (focus-peak widths, camera frames, score
  ratios); the peak width, play of the drive and camera delay are measured on the first run and remembered in
  universal_autofocus_calibration.json. Nothing is tuned per microscope.

THE CLAW RIG (claw on the fine-focus knob, COM4 + Canon through CanonControl.py) is built in. Put this file in
the folder with autofocus_gui.py and change the GUI's import line to
    from universal_autofocus_v1 import autofocus, ZStageController, ARDUINO_PORT, _read_score

    python universal_autofocus_v1.py demo               a simulated microscope, no hardware (a few seconds)
    python universal_autofocus_v1.py                    start the GUI with the universal autofocus
    python universal_autofocus_v1.py auto_scan.py       start the XY scan with it
    python universal_autofocus_v1.py check              check the camera and the claw (moves the knob a little)
    python universal_autofocus_v1.py show               what it has learned about this rig

The GUI starts CanonControl by itself, so for the GUI one Command Prompt window is enough. For "check",
CanonControl must already be running in another window.

What is inside, top to bottom:
    SETTINGS      every tuning number, in units that mean the same on any microscope (focus-peak widths, camera
                  frames, score ratios) - nothing here is about the claw or the Canon
    INTERFACES    the only two things the algorithm needs from hardware: move_by(distance) and frames()
    CALIBRATION   what it learns per setup (peak width, knob play, camera delay), kept in
                  universal_autofocus_calibration.json next to this file
    CORE          the autofocus algorithm itself - no hardware code
    CANON_CAMERA  translator: CanonControl's HTTP server -> frames()
    CANON_CLAW    translator: the claw's serial firmware -> move_by()
    DROP-IN       the same names the old autofocus.py had (autofocus(z), ZStageController, ...), and the launcher
For another microscope nothing here changes: pass your own drive and frames to autofocus().
"""



# ==============================================================================================================
#  SETTINGS.PY
# ==============================================================================================================
from dataclasses import dataclass, field, fields, replace


@dataclass
class Settings:
    # ------------------------------------------------------------------ sweep speeds
    sweep_frames_per_width: float = 4.5    # a measured sweep crosses one peak width in about this many frames
    sweep_frames_per_width_new: float = 4.9  # ... while the peak width of this setup is still a guess
    sweep_max_widths_per_s: float = 3.86   # never faster than this (camera-delay errors grow with speed)
    sweep_min_widths_per_s: float = 1.36   # never slower than this (unless the camera is too slow for it)
    sweep_min_frames_per_width: float = 3.5  # ... except that a sweep always gets at least this many frames per width
    slow_frames_per_width: float = 15.0    # the slow, precise pass over the peak
    slow_max_widths_per_s: float = 0.91
    slow_min_widths_per_s: float = 0.36
    slow_cap_fwhm_per_s: float = 0.3       # (and at most this many of the peak's own widths per second ...
    slow_cap_widths_per_s: float = 2.73    #  ... but never more than this)
    mid_max_widths_per_s: float = 1.59     # speed on the way into the slow pass
    retry_speed_factor: float = 0.5        # the last search stage sweeps this much slower

    # ------------------------------------------------------------------ search (distances in W)
    search_reach: tuple = (2.3, 5.5, 11.4, 20.5, 32.0, 32.0)   # zig-zag: first side, other side, first side, ...
    search_range: float = 32.0             # how far the normal search goes each way
    extended_reach: tuple = (45.5, 63.6, 81.8)   # then both sides further, and to the end of the allowed travel
    end_margin: float = 1.8                # the extended search stops this far before the end of the travel
    lead_in: float = 1.14                  # each sweep starts this far inside what has already been seen
    lead_extra: float = 0.55
    ext_step: float = 1.82                 # a sweep that ends while the score is still rising goes on by this ...
    max_ext: int = 8                       # ... at most this many times
    trend_bin: float = 4.5                 # far out of focus: the side where the blurred score creeps up is searched first

    # ------------------------------------------------------------------ peak detection (log units, W)
    peak_min_rise: float = 0.08            # a peak must stand at least this far above the lowest point just before it
    peak_window: float = 2.73              # ... within this many widths (broad humps and drifts do not count)
    peak_drop: float = 0.40                # confirmed once the score is this fraction of the rise below the top ...
    peak_fall_frames: int = 2              # ... for this many frames in a row
    noise_rise: float = 6.0                # ... and the rise is at least this many times the frame-to-frame noise
    wide_window: float = 10.0              # second detector for broad peaks (low magnification)
    wide_min_rise: float = 0.12
    xwide_window: float = 30.0             # third detector, only while the peak width is unknown
    strong_peak: float = 0.30              # x1.35: an unmistakable focus peak, accepted at once
    medium_peak: float = 0.18              # x1.2: accepted once the area around it has been searched
    weak_peak: float = 0.12                # x1.13: only noted; the best one is used at the very end
    near_zone: float = 3.64                # a peak this close to the start (or to the last focus) ...
    near_min_peak: float = 0.14            # ... needs only this rise to be accepted at once
    hint_zone: float = 2.73
    max_fwhm: float = 4.09                 # a faint peak wider than this is a hump of the background, not the focus
    medium_cover: float = 1.5              # medium peak: both sides searched this many times as far as it is ...
    medium_cover_min: float = 5.45         # ... and at least this far
    weak_ripe: bool = False
    weak_cover: float = 2.0
    weak_cover_min: float = 9.1
    weak_separate: float = 2.73
    downhill_drop: float = 0.10            # the score fell this much since the start of the first sweep -> turn round
    reject_zone: float = 4.09              # a candidate that turned out not to be a peak: this zone is ignored
    peak_unc: float = 0.55                 # how well a sweep locates a peak

    # ------------------------------------------------------------------ what the usual focus of this setup looks like
    default_contrast: float = 2.0          # until anything is known: the focus is expected to stand about this many
                                           # times above its surroundings (an adapter can give a better guess)
    contrast_min_runs: int = 3             # after this many good runs the usual contrast of the focus is trusted
    convincing_frac: float = 0.75          # a peak at least this fraction (log) of the usual focus contrast is
    convincing_cap: float = 1.0            # ... taken at once (but never more than this is demanded, x2.7);
                                           # a weaker peak is used only once the area around it has been searched
    near_cover: float = 2.73               # ... a weak peak near the start: both sides searched this far beyond it
    y_margin: float = 0.05                 # between candidate peaks, the one where the picture is sharpest wins
                                           # (by more than this, log); a peak is not taken while a sharper one is known

    # ------------------------------------------------------------------ camera, settling (seconds)
    camera_lag_guess: float = 0.25         # delay between a position and the frame that shows it, until measured
    mech_settle: float = 0.12              # frames taken this soon after an unmeasured move are ignored
    still_tol: float = 0.02                # the picture is "still" when it can change by no more than this (log)
    tau_max: float = 0.30                  # slowest catching-up time constant the stillness test allows for
    still_se: float = 1.0
    settle_floor: float = 0.35             # never read a settled score sooner than this after a move
    lag_unknown: float = 0.45              # delay assumed for waiting after a move until the delay is measured ...
    lag_slow: float = 0.8                  # ... and after a big move, so a slow camera is not taken for a still picture
    lag_excess: float = 0.5                # until measured, the slow pass allows for a camera this much slower
    stall_seconds: float = 2.5             # no new frame for this long (and for stall_frames frames) = camera frozen
    stall_frames: float = 8.0
    first_frame_timeout: float = 4.0
    stall_wait: float = 120.0              # how long to wait for a frozen camera to come back
    max_stalls: int = 8

    # ------------------------------------------------------------------ the finish (sigma = W / 2.355)
    verify_tol: float = 0.03               # the final reading may be this far (log) below the top of the peak
    max_landings: int = 4
    finish_damp: float = 0.65
    finish_step_sigma: float = 0.55        # one finishing step = this many sigma
    finish_accept_sigma: float = 0.35      # the finish may stop this far in front of the top
    prior_scale: float = 1.0
    prior_sig_p: float = 0.15
    prior_sig_w: float = 0.35
    done_sd_sigma: float = 0.45            # done when the top is known to this many sigma ...
    max_readings: int = 4                  # ... or after this many settled readings in one landing
    close_enough: float = 0.05             # ... or when a reading is this close (fraction of the peak height) to the top
    direct_finish: bool = True             # once the camera delay is known, skip the slow pass

    # ------------------------------------------------------------------ approach from below (knob play / backlash)
    approach_play: float = 1.4             # final approach to a precise position: at least this many times the play ...
    approach_extra: float = 1.14           # ... plus this many widths
    approach_min: float = 2.27             # ... and at least this many widths
    sweep_extra: float = 1.36              # back-off before a measured sweep: play + this many widths ...
    sweep_min: float = 2.27                # ... and at least this many widths
    measure_backlash: bool = True          # measure the play with one sweep down across the first peak found

    # ------------------------------------------------------------------ limits
    max_candidates: int = 6
    max_seconds: float = 300.0             # time limit for one autofocus (waiting for a frozen camera not counted)
    good_score: float = None               # optional: a start score at or above this returns without moving
    learn: bool = True                     # update the calibration file after a good run

    def copy(self, **changes):
        return replace(self, **changes)

    def as_dict(self):
        return {f.name: getattr(self, f.name) for f in fields(self)}


DEFAULT = Settings()


# ==============================================================================================================
#  INTERFACES.PY
# ==============================================================================================================
import math
import time


class DriveError(Exception):
    """Raised by a FocusDrive when the hardware does not respond or reports an error. Stops the autofocus."""


class FrameSourceError(Exception):
    """Raised by a FrameSource when the camera cannot be reached. The autofocus waits for it to come back."""


class FocusDrive:
    """
    Base class of every focus drive.

    REQUIRED
      move_by(delta, fast=False)  move the focus by `delta` (in `units`, positive = the drive's "up"), and return
                                  when the move is finished. `fast=True` means nothing is being measured during
                                  this move, so a faster speed may be used. May return (t_start, t_end): the times
                                  (time.time() clock) the motion really started and ended; if it returns None the
                                  algorithm estimates them from `rate` and the time the call took.
      max_travel                  how far (units) one autofocus may move the focus away from where it starts, in
                                  each direction. Set it to what the hardware can really do and what is safe (a
                                  high-power objective can touch the sample).

    OPTIONAL (help the algorithm, all have safe defaults)
      units          name of the unit, for messages ("steps", "um", ...)
      resolution     smallest move the drive makes reliably (units)
      rate           speed of a normal move (units/s); fast_rate for fast=True
      ack_latency    seconds between the end of the motion and move_by() returning
      settle_time    seconds the mechanics need to come to rest after a fast move
      backlash       the play (units) when the direction reverses, if KNOWN. None: it is measured once and stored
      backlash_guess the play to assume until it is measured (units)
      width_hint     the usual width (FWHM) of the focus peak on this setup (units), if known
      position()     absolute position, if the hardware can report it (None otherwise)
      stop()         stop any motion / release the motor (called if the autofocus is aborted)
      setup_id()     a short text that identifies this drive (used as the key in the calibration file)
    """

    units = "steps"
    max_travel = None
    resolution = 1.0
    rate = None
    fast_rate = None
    ack_latency = 0.0
    settle_time = 0.0
    backlash = None
    backlash_guess = None
    width_hint = None

    def move_by(self, delta, fast=False):
        raise NotImplementedError("a FocusDrive must implement move_by(delta, fast=False)")

    def stop(self):
        pass

    def position(self):
        return None

    def travel_limits(self):
        """(lowest, highest) position the autofocus may use, relative to where it starts. Called once per run."""
        m = float(self.max_travel)
        return -m, m

    def setup_id(self):
        return type(self).__name__

    def describe(self):
        return f"{type(self).__name__} ({self.units})"

    @property
    def memory(self):
        """A small dict that lives as long as the drive (or its connection) does: where the last focus was.
        Adapters that wrap a longer-lived connection object store it there instead."""
        m = getattr(self, "_uaf_memory", None)
        if m is None:
            m = {}
            try:
                self._uaf_memory = m
            except Exception:
                pass
        return m


class FrameSource:
    """
    Base class of every sharpness source.

    REQUIRED
      frames()   every frame that arrived since the previous call, oldest first, as (timestamp, score) pairs.
                 timestamp: time.time() clock, when the frame was taken (or received). score: a positive number
                 that is larger when the picture is sharper. It must return quickly (it is called many times a
                 second); return [] when nothing new has arrived. Raise FrameSourceError if the camera cannot
                 be reached.

    OPTIONAL
      min_poll_interval  the least time (s) between two frames() calls
      settle_floor       the least time (s) after a move before a reading can be trusted (None = default)
      setup_id()         identifies this camera in the calibration file
      describe()         a name for messages
      close()            release the camera (the algorithm never calls it; the owner does)
    """

    min_poll_interval = 0.012
    settle_floor = None

    def frames(self):
        raise NotImplementedError("a FrameSource must implement frames()")

    def setup_id(self):
        return type(self).__name__

    def describe(self):
        return type(self).__name__

    def close(self):
        pass


def _num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def check_drive(drive):
    """Problems with a drive's declarations, as a list of messages (empty = fine)."""
    problems = []
    if not callable(getattr(drive, "move_by", None)):
        problems.append("it has no move_by(delta, fast=False) method")
    mt = _num(getattr(drive, "max_travel", None))
    if mt is None or mt <= 0:
        problems.append("max_travel is not set: say how far (in its units) one autofocus may move the focus "
                        "each way")
    res = _num(getattr(drive, "resolution", 1.0))
    if res is None or res <= 0:
        problems.append("resolution must be a positive number (the smallest move it makes)")
    elif mt is not None and mt > 0 and res > mt / 20:
        problems.append(f"resolution ({res:g}) is too coarse for max_travel ({mt:g}): fewer than 20 steps each way")
    for name in ("rate", "fast_rate", "backlash", "backlash_guess", "width_hint"):
        v = getattr(drive, name, None)
        if v is not None and (_num(v) is None or _num(v) < 0):
            problems.append(f"{name} must be a positive number or None")
    for name in ("rate", "fast_rate", "width_hint"):
        v = _num(getattr(drive, name, None))
        if getattr(drive, name, None) is not None and v is not None and v == 0:
            problems.append(f"{name} must not be 0 (use None if unknown)")
    return problems


def check_frames(src):
    problems = []
    if not callable(getattr(src, "frames", None)):
        problems.append("it has no frames() method")
    return problems


def wall_clock():
    return time.time()


# ==============================================================================================================
#  CALIBRATION.PY
# ==============================================================================================================
import json
import math
import os
import re
import tempfile
import threading

_LOCK = threading.Lock()

# values that do not depend on the objective: a new objective inherits them
_SHARED = ("lag", "lag_n", "backlash", "backlash_n", "fps")


class CalibrationStore:
    def __init__(self, path=None):
        """path: the JSON file (created when something is first learned). None: keep everything in memory."""
        self.path = path
        self._mem = {"version": 2, "setups": {}}
        self.load_error = None
        self._load()

    # ------------------------------------------------------------------ file
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("setups"), dict):
                self._mem = d
            else:
                raise ValueError("not a calibration file")
        except Exception as e:                      # a damaged file must never stop the microscope
            self.load_error = f"{e}"
            try:
                os.replace(self.path, self.path + ".bad")
            except Exception:
                pass
            self._mem = {"version": 2, "setups": {}}

    def _save(self):
        if not self.path:
            return
        folder = os.path.dirname(os.path.abspath(self.path))
        try:
            fd, tmp = tempfile.mkstemp(prefix=".uaf_", suffix=".json", dir=folder)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._mem, f, indent=1, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass

    # ------------------------------------------------------------------ entries
    def keys(self):
        return sorted(self._mem["setups"].keys())

    def get(self, key):
        """A copy of what is known about `key` ({} if nothing). A new objective inherits the camera delay and
        the play of the drive from another objective of the same drive and camera, and gets a first guess of its
        peak width from the others' (scaled by the magnification in the names, e.g. "20x" -> "50x")."""
        with _LOCK:
            d = self._mem["setups"].get(key)
            if d is not None:
                return dict(d)
            base, _, obj = key.partition("/")
            sib = {}
            for k, v in self._mem["setups"].items():
                b, _, o = k.partition("/")
                if b == base and not o.startswith("_"):
                    sib[o] = v
            out = {}
            if sib:
                best = max(sib.values(), key=lambda v: v.get("lag_n", 0) + v.get("backlash_n", 0))
                for name in _SHARED:
                    if name in best:
                        out[name] = best[name]
                out["inherited"] = True
                g = _width_guess(obj, sib)
                if g:
                    out["fwhm_guess"] = g
            return out

    def put(self, key, d):
        with _LOCK:
            if self.path:
                self._reload_quiet()
            clean = {k: v for k, v in d.items() if k not in ("inherited", "fwhm_guess")}
            self._mem["setups"][key] = clean
            self._save()

    def _reload_quiet(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("setups"), dict):
                self._mem = d
        except Exception:
            pass

    def forget(self, key=None):
        """Forget one setup (key), or everything (key=None)."""
        with _LOCK:
            if key is None:
                self._mem["setups"] = {}
            else:
                for k in [k for k in self._mem["setups"] if k == key or k.startswith(key)]:
                    del self._mem["setups"][k]
            self._save()

    def mark(self, key, **flags):
        d = self.get(key)
        d.update(flags)
        self.put(key, d)

    def import_v5(self, v5_path, key):
        """Take over what autofocus v5 learned (its autofocus_calibration.json) for `key`, if nothing is known
        about `key` yet. The play (backlash) is not taken over: v5 never measured it, it is measured anew.
        Returns True if something was taken over."""
        with _LOCK:
            if key in self._mem["setups"] or not v5_path or not os.path.exists(v5_path):
                return False
        try:
            with open(v5_path, "r", encoding="utf-8") as f:
                v5 = json.load(f)
        except Exception:
            return False
        if not isinstance(v5, dict):
            return False
        d = {}
        try:
            if "lag" in v5 and 0.0 < float(v5["lag"]) < 2.0:
                d["lag"] = float(v5["lag"])
                d["lag_n"] = int(min(v5.get("lag_n", 1), 4))
            if "fwhm" in v5 and 0.0 < float(v5["fwhm"]) < 1e6:
                d["fwhm"] = float(v5["fwhm"])
                d["fwhm_n"] = 2
            if "fps" in v5:
                d["fps"] = float(v5["fps"])
        except (TypeError, ValueError):
            return False
        if not d:
            return False
        d["imported_from_v5"] = True
        self.put(key, d)
        return True

    def describe(self, key=None):
        lines = []
        for k in self.keys():
            if key and not k.startswith(key):
                continue
            d = self._mem["setups"][k]
            lines.append(k)
            for name, label, fmt in (("fwhm", "focus-peak width", "{:.3g}"), ("lag", "camera delay (s)", "{:.3f}"),
                                     ("backlash", "play of the drive", "{:.3g}"), ("contrast", "usual focus contrast",
                                                                                   "x{:.2f}"),
                                     ("fps", "frames per second", "{:.1f}")):
                if name in d:
                    v = d[name]
                    if name == "contrast":
                        import math
                        v = math.exp(v)
                    n = d.get(name + "_n")
                    lines.append(f"    {label:22s} {fmt.format(v)}" + (f"   (from {n} runs)" if n else ""))
            if "move_extra_ms" in d or "down_scale" in d:
                lines.append(f"    movement corrections   {float(d.get('move_extra_ms', 0.0)):+.2f} ms per move, downward "
                             f"moves x{float(d.get('down_scale', 1.0)):.4f}   (measured by check-zstage)")
            if d.get("recheck_backlash"):
                lines.append("    (the play will be measured again on the next run)")
        return "\n".join(lines) if lines else "(nothing learned yet)"


def magnification(name):
    """5 from "5x", 50 from "Olympus 50X LMPlan", None if the name has no magnification in it."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*[xX×](?![a-zA-Z0-9])", str(name or ""))
    try:
        return float(m.group(1)) if m and float(m.group(1)) > 0 else None
    except ValueError:
        return None


def _width_guess(obj, siblings):
    """Peak width of objective `obj` guessed from the nearest other objective with a known width: the depth of
    field shrinks roughly as magnification^-1.2 in a usual series of objectives. Only a starting point - the
    width is measured on the first run."""
    M = magnification(obj)
    if not M:
        return None
    best = None
    for o, v in siblings.items():
        Mi = magnification(o)
        try:
            w = float(v.get("fwhm") or 0.0)
        except (TypeError, ValueError):
            continue
        if Mi and w > 0:
            dist = abs(math.log(Mi / M))
            if best is None or dist < best[0]:
                best = (dist, w * (Mi / M) ** 1.2)
    return round(best[1], 5) if best else None


def ema_store(d, key, value, tol, n_max=8):
    """Remember `value` under `key` in dict d: the first value as it is, later ones move the stored value only part
    of the way, and a value far from the stored one (a wrong peak, a bad run) hardly counts."""
    n = int(d.get(key + "_n", 0))
    old = d.get(key)
    if old is None:
        new = float(value)
    else:
        w = max(0.3, 1.0 / (n + 1))
        if n >= 2 and abs(value - old) > tol:
            w = 0.12
        new = old + (value - old) * w
    d[key] = round(new, 5)
    d[key + "_n"] = min(n + 1, n_max)
    return d[key]


# ==============================================================================================================
#  CORE.PY
# ==============================================================================================================
import bisect
import collections
import math
import time
import traceback
from dataclasses import dataclass, field


try:                                   # numpy makes the finish faster; it also works without
    import numpy as _np
except Exception:                      # pragma: no cover
    _np = None

__all__ = ["autofocus", "FocusResult", "Env", "setup_key"]

VERSION = "1.0"


class Env:
    """The clock and sleep the algorithm uses (a simulator swaps in virtual time)."""

    def __init__(self, clock=None, sleep=None):
        self.clock = clock or time.time
        self.sleep = sleep or time.sleep


@dataclass
class FocusResult:
    success: bool
    message: str
    position: float = 0.0                 # final position, relative to where it started (drive units)
    score: float = float("nan")           # sharpness at the end
    start_score: float = float("nan")
    measurements: int = 0                 # frames used
    seconds: float = 0.0
    quality: str = "none"                 # "good", "fair", "poor" ("none" if it did not focus)
    peak_ratio: float = float("nan")      # how many times its surroundings the focus peak stood
    width: float = float("nan")           # width of the focus peak (drive units)
    units: str = ""
    notes: list = field(default_factory=list)

    def __bool__(self):
        return bool(self.success)


class _Abort(Exception):
    def __init__(self, reason, user=False):
        super().__init__(reason)
        self.reason = reason
        self.user = user


class _Stall(Exception):
    """The camera stopped delivering frames."""


# ═══════════════════════════════════════════════════════════
#  SMALL MATH HELPERS
# ═══════════════════════════════════════════════════════════
def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def _median(v):
    s = sorted(v)
    n = len(s)
    if n == 0:
        return float("nan")
    return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _sigma_mad(v):
    m = _median(v)
    return 1.4826 * _median([abs(x - m) for x in v])


def _solve3(m, b):
    a = [row[:] + [b[i]] for i, row in enumerate(m)]
    for i in range(3):
        p = max(range(i, 3), key=lambda r: abs(a[r][i]))
        if abs(a[p][i]) < 1e-12:
            return None
        a[i], a[p] = a[p], a[i]
        for r in range(i + 1, 3):
            f = a[r][i] / a[i][i]
            for c in range(i, 4):
                a[r][c] -= f * a[i][c]
    x = [0.0] * 3
    for i in (2, 1, 0):
        x[i] = (a[i][3] - sum(a[i][c] * x[c] for c in range(i + 1, 3))) / a[i][i]
    return x


def _parabola(us, ys):
    """Least-squares y = a*(u-u0)^2 + b*(u-u0) + c.  Returns (a, b, c, u0) or None."""
    n = len(us)
    if n < 3:
        return None
    u0 = sum(us) / n
    s = [sum((u - u0) ** k for u in us) for k in range(5)]
    t = [sum(y * (u - u0) ** k for u, y in zip(us, ys)) for k in range(3)]
    sol = _solve3([[s[4], s[3], s[2]], [s[3], s[2], s[1]], [s[2], s[1], s[0]]], [t[2], t[1], t[0]])
    if sol is None:
        return None
    return sol[0], sol[1], sol[2], u0


def _log(s):
    try:
        return math.log(max(float(s), 1e-9))
    except (TypeError, ValueError):
        return math.log(1e-9)


# the shapes the finish allows the top of the focus peak to have: exp(-|x/a|^beta), from pointed (beta 1, what a
# high-NA objective gives once the blur grows linearly with defocus) to rounded (beta 2, a Gaussian)
_BETAS = (1.0, 1.5, 2.0)


def sigma_from_log_width(fwhm_log, L):
    """
    The peak detector measures a peak's width at half its height on the LOG of the score. The finish models the
    score itself as background + Gaussian bump (sigma). For a bump L (log) high, the log-half-height lies at the
    fraction f = 1 / (e^(L/2) + 1) of the bump, so sigma = width / (2 * sqrt(2 ln(1/f))). (Small bumps: f = 1/2
    and the usual width / 2.355; a peak 10x the background: width / 3.38.)
    """
    L = max(0.0, float(L))
    f = 1.0 / (math.exp(0.5 * L) + 1.0)
    return float(fwhm_log) / (2.0 * math.sqrt(2.0 * math.log(1.0 / f)))


def _bracket_top(pts, sds):
    """If the best settled reading has a lower reading on each side (in position), the top of the parabola (in log
    score) through it and those two neighbours: (position, best reading, tolerance for confirming it). Else None."""
    by_x = {}
    for (x, y), s in zip(pts, sds):
        by_x.setdefault(round(x, 9), []).append((y, s))
    xs = sorted(by_x)
    if len(xs) < 3:
        return None
    ys = [sum(v[0] for v in by_x[x]) / len(by_x[x]) for x in xs]
    ss = [max(v[1] for v in by_x[x]) for x in xs]
    i = max(range(len(xs)), key=lambda k: ys[k])
    if i == 0 or i == len(xs) - 1:
        return None
    (x1, y1), (x2, y2), (x3, y3) = (xs[i - 1], ys[i - 1]), (xs[i], ys[i]), (xs[i + 1], ys[i + 1])
    noise = max(ss[i - 1], ss[i], ss[i + 1], 0.003)
    if not (y2 - y1 > noise and y2 - y3 > noise):
        return None
    d1, d3 = x2 - x1, x3 - x2
    den = d1 * (y2 - y3) + d3 * (y2 - y1)
    if den <= 0:
        return None
    # vertex of the parabola through (x2-d1, y1), (x2, y2), (x2+d3, y3), kept within half a gap of x2
    off = 0.5 * (d3 * d3 * (y2 - y1) - d1 * d1 * (y2 - y3)) / den
    xv = min(max(x2 + off, x2 - 0.5 * d1), x2 + 0.5 * d3)
    return xv, y2, max(0.03, 2.5 * noise) + 0.02


def setup_key(drive, frames, objective=None):
    """The key under which what is learned about this drive + camera (+ objective) is stored."""
    try:
        d = str(drive.setup_id())
    except Exception:
        d = type(drive).__name__
    try:
        f = str(frames.setup_id())
    except Exception:
        f = type(frames).__name__
    return f"{d}|{f}/{objective or 'default'}"


@dataclass
class _Peak:
    pos: float            # position of the top (lag-compensated), relative to the start, "up" coordinates
    d: int                # +1 / -1: direction of the pass that saw it
    v: float              # speed of that pass (units/s)
    L: float              # how far (log) it stood above the lowest point just before it
    y: float              # log score at the top
    fwhm: float           # width at half height (units)
    npts: int = 0
    unc: float = 0.0      # position uncertainty (units)
    fit: bool = True
    kappa: float = 0.0
    lag_used: float = 0.25

    @property
    def ratio(self):
        return math.exp(self.L)


# ═══════════════════════════════════════════════════════════
#  FRAME FEED
# ═══════════════════════════════════════════════════════════
class _Feed:
    def __init__(self, src, env, say):
        self.src = src
        self.env = env
        self.say = say
        self.t_start = env.clock()
        self.queue = []
        self.newest = None            # timestamp of the newest NEW frame
        self.last_t = None
        self.last_s = None
        self.fail = 0
        self.errors = 0
        self.last_error = None
        self.total = 0
        self._t_poll = -1e18
        try:
            self.min_gap = max(0.002, float(getattr(src, "min_poll_interval", 0.012) or 0.012))
        except (TypeError, ValueError):
            self.min_gap = 0.012
        self._said_err = False

    def poll(self):
        now = self.env.clock()
        if now - self._t_poll < self.min_gap:
            return
        self._t_poll = now
        try:
            rows = self.src.frames()
            self.fail = 0
        except Exception as e:                      # camera not reachable: no frames (stall detection waits)
            self.fail += 1
            self.errors += 1
            self.last_error = str(e) or type(e).__name__
            if not self._said_err and self.fail >= 3:
                self._said_err = True
                self.say(f"cannot read the camera ({self.last_error})")
            rows = ()
        for row in rows or ():
            try:
                t = float(row[0])
                s = float(row[-1])
            except (TypeError, ValueError, IndexError):
                continue
            if not (math.isfinite(t) and math.isfinite(s)):
                continue
            if self.last_t is not None and t <= self.last_t:
                continue                            # old or repeated frame
            self.last_t = t
            if s == self.last_s:                    # exact repeat = the same picture delivered again
                continue
            self.last_s = s
            self.newest = t
            self.queue.append((t, s))
            self.total += 1

    def pop(self):
        return self.queue.pop(0) if self.queue else None

    def clear(self):
        self.queue.clear()

    def stalled(self, stall_s, first_s):
        ref = self.newest if self.newest is not None else self.t_start
        limit = stall_s if self.newest is not None else first_s
        return self.env.clock() - ref > limit


# ═══════════════════════════════════════════════════════════
#  STAGE: commanded position + a time-stamped log of where the focus was
# ═══════════════════════════════════════════════════════════
class _Stage:
    def __init__(self, drive, env, check, res, lo, hi, settle, chunk):
        self.drive = drive
        self.env = env
        self.check = check
        self.res = res
        self.lo, self.hi = lo, hi
        self.settle = settle
        self.chunk = chunk                   # longest single move (keeps the Stop button responsive)
        self.c = 0.0                         # commanded position, relative to the start
        self.segs = []                       # (t0, t1, c0, c1)
        self.t0s = []
        self.t_quiet = -1e18                 # frames taken before this are ignored (right after fast moves)
        self.travel = 0.0
        self.up_run = 0.0                    # moved upward since the last downward move
        self.up_slack = 0.0                  # how much short of the margin still counts as "play taken up"
        self.moves = 0
        rate = getattr(drive, "rate", None)
        frate = getattr(drive, "fast_rate", None)
        self.rate = float(rate) if rate else None
        self.fast_rate = float(frate) if frate else self.rate
        try:
            self.ack = max(0.0, float(getattr(drive, "ack_latency", 0.0) or 0.0))
        except (TypeError, ValueError):
            self.ack = 0.0

    def q(self, x):
        """Round to the drive's resolution."""
        return round(round(x / self.res) * self.res, 9)

    def pos_at(self, t):
        i = bisect.bisect_right(self.t0s, t) - 1
        if i < 0:
            return float(self.segs[0][2]) if self.segs else float(self.c)
        t0, t1, c0, c1 = self.segs[i]
        if t >= t1 or t1 <= t0:
            return float(c1)
        return c0 + (c1 - c0) * (t - t0) / (t1 - t0)

    def room(self, d):
        """How far the drive may still go in direction d."""
        return (self.hi - self.c) if d > 0 else (self.c - self.lo)

    def move(self, n, fast=False, measured=False):
        n = self.q(n)
        tgt = self.c + n
        if tgt > self.hi + 1e-9:
            n = self.q(math.floor((self.hi - self.c) / self.res + 1e-9) * self.res)
        elif tgt < self.lo - 1e-9:
            n = self.q(-math.floor((self.c - self.lo) / self.res + 1e-9) * self.res)
        if abs(n) < 0.5 * self.res:
            return False
        clock = self.env.clock
        t_send = clock()
        try:
            r = self.drive.move_by(n, fast=fast)
        except DriveError as e:
            raise _Abort(f"the focus drive reported a problem: {e}")
        except _Abort:
            raise
        except Exception as e:
            raise _Abort(f"the focus drive failed: {e!r}")
        t_end = clock()
        t0 = t1 = None
        if isinstance(r, (tuple, list)) and len(r) == 2:
            try:
                t0, t1 = float(r[0]), float(r[1])
                if not (t_send - 1.0 <= t0 <= t1 <= t_end + 1.0):
                    t0 = t1 = None
            except (TypeError, ValueError):
                t0 = t1 = None
        if t0 is None:
            rate = self.fast_rate if fast else self.rate
            t1 = max(t_send, t_end - self.ack)
            t0 = max(t_send, t1 - abs(n) / rate) if rate else t_send
        self.segs.append((t0, t1, self.c, self.c + n))
        self.t0s.append(t0)
        self.c = round(self.c + n, 9)
        self.travel += abs(n)
        self.moves += 1
        self.up_run = self.up_run + n if n > 0 else 0.0
        if not measured:
            self.t_quiet = t_end + self.settle
        return True

    def _run(self, target, fast=True, slow_tail=0.0):
        """Move to `target` in chunks (Stop-responsive). The last `slow_tail` uses the measuring speed."""
        guard = 0
        while abs(self.c - target) >= 0.5 * self.res:
            self.check()
            rem = target - self.c
            f = fast
            n = rem
            if slow_tail and abs(rem) <= slow_tail + 1e-9:
                f = False
            elif slow_tail and abs(rem) > slow_tail:
                n = rem - (slow_tail if rem > 0 else -slow_tail)
            n = max(-self.chunk, min(self.chunk, n))
            if not self.move(n, fast=f):
                break                                   # at the end of the allowed travel
            guard += 1
            if guard > 100000:
                raise _Abort("internal error: a move did not finish")

    def goto(self, target, approach=0, margin=0.0, fast=True, slow_tail=0.0):
        """
        Unmeasured move to `target`. With approach=+1 (-1) the last move is upward (downward) by at least
        `margin`, so the play of the drive is taken up the same way every time.
        """
        target = self.q(_clamp(target, self.lo, self.hi))
        if approach > 0 and target >= self.c and self.up_run >= margin - self.up_slack:
            pass                                        # already moving upward with the play taken up
        elif approach > 0 and self.c > target - margin:
            self._run(self.q(_clamp(target - margin, self.lo, self.hi)), fast)
        elif approach < 0 and self.c < target + margin:
            self._run(self.q(_clamp(target + margin, self.lo, self.hi)), fast)
        self._run(target, fast, slow_tail=slow_tail if approach else 0.0)


class _Pace:
    """Issues small moves so that the average speed is v units/s (position follows a straight line in time)."""

    def __init__(self, stage, env, d, v, nmin, nmax):
        self.st, self.env, self.d = stage, env, d
        self.nmin, self.nmax = nmin, nmax
        self.set_speed(v)

    def set_speed(self, v):
        self.v = v
        self.t0 = self.env.clock()
        self.c0 = self.st.c

    def step(self):
        t = self.env.clock()
        c_des = self.c0 + self.d * self.v * (t + 0.02 - self.t0)
        n = self.d * (c_des - self.st.c)
        room = self.st.room(self.d)
        if n > room:
            n = room
        if n >= self.nmin - 1e-9 or (room < self.nmin and n >= room - 1e-9 and room >= 0.5 * self.st.res):
            n = min(n, self.nmax)
            if self.st.move(self.d * n, fast=False, measured=True):
                return True
        self.env.sleep(0.004)
        return False


# ═══════════════════════════════════════════════════════════
#  PEAK DETECTOR  (one measured sweep; leg coordinates u = d*c, increasing along the travel)
# ═══════════════════════════════════════════════════════════
class _Detector:
    """
    Feeds on (position, score) frames in order and reports
      ("peak", _Peak)    the score climbed by at least `pthr` (log) and has now fallen for several frames
      ("downhill", None) (first sweep only) the score has fallen clearly since the sweep began
    A 3-frame median removes single bad frames. The rise L is measured against the lowest point in the last
    `wl` units, so broad humps and slow drifts never count.
    """

    def __init__(self, d, sigma, v, S, *, pthr, wl, wx, drop_frac, nfall, fwhm_hint, fw_lo, fw_hi,
                 spacing=None, allow_downhill=False, pending_wl=None):
        self.d, self.v, self.S = d, v, S
        self.sigma = sigma
        self.pthr0 = pthr
        self.wl = wl
        self.wx = wx
        self.drop_frac = drop_frac
        self.nfall = nfall
        self.fwhm_hint = fwhm_hint
        self.fw_lo, self.fw_hi = fw_lo, fw_hi
        self.spacing = spacing                 # position change per frame (to reject impossibly narrow peaks)
        self.allow_downhill = allow_downhill
        self.pending_wl = pending_wl if pending_wl is not None else wl
        self.RU, self.RY = [], []
        self.U, self.Y = [], []
        self.diffs = []
        self.sig_loc = sigma
        self.cand = None
        self.cy = self.cL = 0.0
        self.fall = 0
        self.yref = None
        self.dh = 0

    def reset_candidate(self):
        self.cand = None
        self.fall = 0

    def pending(self):
        if self.cand is not None or self.dh > 0:
            return True
        if len(self.Y) < 4:
            return False
        k = bisect.bisect_left(self.U, self.U[-1] - self.pending_wl)
        return self.Y[-1] - min(self.Y[k:]) >= max(0.06, self.S.noise_rise * self.sig_eff()) \
            and self.Y[-1] >= self.Y[-2] - 0.005

    def sig_eff(self):
        sg = self.sig_loc if len(self.diffs) >= 8 else self.sigma
        return _clamp(sg, 0.003, 0.06)

    def pthr(self):
        return max(self.pthr0, self.S.noise_rise * self.sig_eff())

    def add(self, c, s):
        return self._push(self.d * c, _log(s))

    def _push(self, u, y):
        self.RU.append(u)
        self.RY.append(y)
        n = len(self.RY)
        if n >= 3:
            self.diffs.append(abs(self.RY[-1] - 2 * self.RY[-2] + self.RY[-3]))
            if len(self.diffs) >= 8:
                w = sorted(self.diffs[-24:])
                self.sig_loc = w[int(0.4 * (len(w) - 1))] / 1.284
        if n == 1:
            self.U.append(self.RU[0])
            self.Y.append(self.RY[0])
            return None
        if n < 3:
            return None
        m = sorted(self.RY[-3:])[1]
        self.U.append(self.RU[-2])
        self.Y.append(m)
        return self._step()

    def _step(self):
        j = len(self.Y) - 1
        u, y = self.U[j], self.Y[j]
        i0 = bisect.bisect_left(self.U, u - self.wl)
        L = y - min(self.Y[i0:j + 1])

        if self.allow_downhill and self.cand is None:
            if self.yref is None and j >= 2:
                self.yref = _median(self.Y[:4])
            if self.yref is not None:
                if self.yref - y >= max(self.S.downhill_drop, 8 * self.sig_eff()):
                    self.dh += 1
                else:
                    self.dh = 0
                if self.dh >= 3:
                    return ("downhill", None)

        if L >= self.pthr() and (self.cand is None or y > self.cy):
            self.cand, self.cy, self.cL = j, y, L
            self.fall = 0
        elif self.cand is not None:
            drop = self.cy - y
            if drop >= max(self.drop_frac * self.cL, 5 * self.sig_eff(), 0.04):
                self.fall += 1
            else:
                self.fall = 0
            if self.fall >= self.nfall:
                pk = self.peak()
                if pk is not None:
                    return ("peak", pk)
                self.reset_candidate()
                return None
            if u - self.U[self.cand] > self.wx:
                self.reset_candidate()
        return None

    def peak(self):
        j = self.cand
        top, L = self.cy, self.cL
        depth = max(0.35 * L, 3 * self.sig_eff(), 0.03)
        lo = j
        while lo > 0 and self.Y[lo - 1] >= top - depth:
            lo -= 1
        hi = j
        while hi < len(self.Y) - 1 and self.Y[hi + 1] >= top - depth:
            hi += 1
        if hi - lo < 1:
            return None                                # only one frame stands at the top: not a peak
        a, b = max(0, lo - 1), min(len(self.RU) - 1, hi + 1)
        us, ys = self.RU[a:b + 1], self.RY[a:b + 1]
        pos, ytop, fit, kap = self._vertex(us, ys, self.U[j], top)
        fwhm = self._fwhm(j, pos, top, L)
        if self.spacing and fwhm < 1.33 * self.spacing:
            return None                                # narrower than the frame spacing allows: noise
        w = fwhm / 2.355
        k0 = (1.0 - math.exp(-L)) / (2.0 * w * w)
        kap = _clamp(kap, 0.25 * k0, 4.0 * k0) if kap > 0 else k0
        return _Peak(pos=self.d * pos, d=self.d, v=self.v, L=L, y=ytop, fwhm=fwhm, npts=len(us), fit=fit,
                     kappa=kap)

    def _vertex(self, us, ys, u_c, y_c):
        if len(us) >= 4:
            keep = list(range(len(us)))
            for _ in range(2):
                f = _parabola([us[i] for i in keep], [ys[i] for i in keep])
                if f is None or f[0] >= -1e-12:
                    break
                a, b, c, u0 = f
                res = [ys[i] - (a * (us[i] - u0) ** 2 + b * (us[i] - u0) + c) for i in keep]
                s = max(_sigma_mad(res), 0.005)
                k2 = [i for i, r in zip(keep, res) if abs(r) <= 3.5 * s]
                if len(k2) == len(keep) or len(k2) < 4:
                    break
                keep = k2
            f = _parabola([us[i] for i in keep], [ys[i] for i in keep])
            if f is not None and f[0] < -1e-12:
                a, b, c, u0 = f
                x = u0 - b / (2 * a)
                step = (us[-1] - us[0]) / max(1, len(us) - 1)
                if us[0] - 1.5 * step <= x <= us[-1] + 1.5 * step:
                    return x, c - b * b / (4 * a), True, -a
        w = [max(0.0, y - (y_c - 0.35 * max(0.0, y_c - min(ys)))) for y in ys]
        if sum(w) > 0:
            x = sum(u * wi for u, wi in zip(us, w)) / sum(w)
        else:
            x = u_c
        return x, y_c, False, 0.0

    def _fwhm(self, j, u_p, top, L):
        half = top - 0.5 * L
        uL = None
        for k in range(j, 0, -1):
            if self.Y[k - 1] <= half <= self.Y[k] or (self.Y[k - 1] < half and self.Y[k] >= half):
                y0, y1 = self.Y[k - 1], self.Y[k]
                f = (half - y0) / (y1 - y0) if y1 != y0 else 0.0
                uL = self.U[k - 1] + f * (self.U[k] - self.U[k - 1])
                break
        uR = None
        for k in range(j, len(self.Y) - 1):
            if self.Y[k + 1] <= half <= self.Y[k]:
                y0, y1 = self.Y[k], self.Y[k + 1]
                f = (y0 - half) / (y0 - y1) if y0 != y1 else 0.0
                uR = self.U[k] + f * (self.U[k + 1] - self.U[k])
                break
        if uL is not None and uR is not None:
            w = uR - uL
        elif uL is not None:
            w = 2 * (u_p - uL)
        else:
            w = self.fwhm_hint
        return _clamp(w, self.fw_lo, self.fw_hi)


class _MultiDet:
    """Detectors for narrow and broad peaks fed with the same frames (a peak's width depends on the objective)."""

    def __init__(self, dets, strong):
        self.dets = dets
        self.narrow = dets[0]
        self.strong = strong

    def push_rest(self, ys):
        for y in ys:
            for d in self.dets:
                d._push(0.0, y)

    def add(self, c, s):
        ev = None
        for d in self.dets:
            e = d.add(c, s)
            if e is not None and ev is None:
                ev = e
        return ev

    def pending(self, ext=0):
        if self.narrow.pending():
            return True
        return ext < 2 and any(d.cand is not None and d.cL >= self.strong for d in self.dets[1:])

    def reset_candidate(self):
        for d in self.dets:
            d.reset_candidate()


class _Search:
    """Progress of the zig-zag search (kept so a frozen camera can resume it)."""

    def __init__(self, first_dir, r0=None):
        self.ext = {+1: 0.0, -1: 0.0}     # how far above the start has been OBSERVED
        self.D = 0.0                      # how far below the start has been observed (contiguously)
        self.dn_resume = None
        self.first_dir = first_dir
        self.queue = []
        self.stage_i = 0
        self.weak = []
        self.rejected = []
        self.sweeps_done = 0
        self.r0 = r0

    def cover(self, side):
        return self.ext[+1] if side > 0 else self.D


# ═══════════════════════════════════════════════════════════
#  THE AUTOFOCUS
# ═══════════════════════════════════════════════════════════
class _Run:
    def __init__(self, drive, frames, S, store, key, log, stop_event, env, expect=None, direction=None,
                 use_last_focus=True):
        self.drive = drive
        self.src = frames
        self.S = S
        self.store = store
        self.key = key
        self.log_fn = log or (lambda m: None)
        self.stop_event = stop_event
        self.env = env
        self.expect = expect
        self.direction = direction
        self.use_last_focus = use_last_focus
        self.t0 = env.clock()
        self.notes = []
        self.cal = store.get(key) if store is not None else {}
        self.cal0 = dict(self.cal)

        self.units = str(getattr(drive, "units", "units") or "units")
        self.res = float(getattr(drive, "resolution", 1.0) or 1.0)
        lo, hi = drive.travel_limits()
        lo, hi = float(min(lo, 0.0)), float(max(hi, 0.0))
        self.lo, self.hi = lo, hi
        span = max(hi, -lo)

        # the width of the focus peak (every distance is a multiple of it)
        # (the drive's hint is trusted for its usual objective; a named objective that is new starts from a guess -
        # scaled from the other objectives' widths if there are any - and measures its width on the first run)
        self.W_known = False
        W = None
        named = str(key).partition("/")[2] not in ("", "default")
        hint = getattr(drive, "width_hint", None)
        if "fwhm" in self.cal:
            W = float(self.cal["fwhm"])
            self.W_known = True
        elif hint and not named:
            W = float(hint)
            self.W_known = True
        else:
            for g in (self.cal.get("fwhm_guess"), hint):
                try:
                    if g and float(g) > 0:
                        W = _clamp(float(g), 3.0 * self.res, span / 4.0)
                        break
                except (TypeError, ValueError):
                    pass
        if not self.W_known:
            if W is None or not (W > 0):
                W = max(span / 55.0, 3.0 * self.res)
            self.notes.append("peak width not known yet: it is measured on this run")
        self.W = max(W, 2.0 * self.res)
        self.fwhm = self.W

        # camera delay
        self.lag = _clamp(float(self.cal.get("lag", S.camera_lag_guess)), 0.0, 3.0)
        self.lag_known = "lag" in self.cal and not self.cal.get("lag_suspect")

        # play of the drive (backlash)
        self.B_declared = getattr(drive, "backlash", None) is not None
        self.B_raw = None
        self.B_raw_v = None
        if self.B_declared:
            self.B = max(0.0, float(drive.backlash))
            self.B_known = True
        elif "backlash" in self.cal:
            self.B = max(0.0, float(self.cal["backlash"]))
            self.B_known = True
        else:
            g = getattr(drive, "backlash_guess", None)
            self.B = max(0.0, float(g)) if g is not None else 0.5 * self.W
            self.B_known = False
        self.measure_B = bool(S.measure_backlash and not self.B_declared and
                              (not self.B_known or self.cal.get("recheck_backlash")))

        # usual contrast of the focus on this setup (learned; until then the adapter's hint, if any)
        self.contrast = self.cal.get("contrast")
        self.contrast_n = int(self.cal.get("contrast_n", 0))
        self.contrast_hint = None
        for obj in (frames, drive):
            h = getattr(obj, "contrast_hint", None)
            try:
                if h is not None and float(h) > 1.0:
                    self.contrast_hint = math.log(float(h))
                    break
            except (TypeError, ValueError):
                pass

        self.sigma = 0.01
        self.fps = float(self.cal.get("fps", 15.0))
        self.v_sweep = self.W * 2.0
        self.start_score = float("nan")
        self.frames_used = 0
        self.sp = None
        self.rest_y = []
        self.stalls = 0
        self.t_waited = 0.0
        self.candidates_tried = 0
        self.hint_rel = None
        self.prof = []
        self._said_rising = set()
        self.final_sd = None
        self._rec = None                      # frames recorded around a finishing step (camera-delay measurement)
        self.lag_obs = []                     # camera delays measured directly from finishing steps

        try:
            settle = max(S.mech_settle, float(getattr(drive, "settle_time", 0.0) or 0.0))
        except (TypeError, ValueError):
            settle = S.mech_settle
        self.mech_settle = settle
        fr = getattr(drive, "fast_rate", None) or getattr(drive, "rate", None)
        chunk = float(fr) * 0.9 if fr else max(span / 8.0, 4 * self.res)
        chunk = max(self.res, math.floor(chunk / self.res) * self.res)
        self.feed = _Feed(frames, env, self.say)
        self.stage = _Stage(drive, env, self.check, self.res, lo, hi, settle, chunk)
        self.stage.up_slack = 0.68 * self.W
        mem = self._memory()
        self.abs0 = mem.get("abs", 0.0)
        try:
            p = drive.position()
            self.abs_pos0 = float(p) if p is not None else None
        except Exception:
            self.abs_pos0 = None
        fsf = getattr(frames, "settle_floor", None)
        self.settle_floor = float(fsf) if fsf else S.settle_floor

    # ------------------------------------------------------------------ plumbing
    def _memory(self):
        try:
            m = self.drive.memory
            return m if isinstance(m, dict) else {}
        except Exception:
            return {}

    def fx(self, x):
        """A position for messages."""
        if self.res >= 0.5:
            return f"{x:+.0f}"
        dec = max(1, min(4, int(math.ceil(-math.log10(self.res))) ))
        return f"{x:+.{dec}f}"

    def fw(self, x):
        if self.res >= 0.5:
            return f"{x:.0f}"
        dec = max(1, min(4, int(math.ceil(-math.log10(self.res)))))
        return f"{x:.{dec}f}"

    def elapsed(self):
        return self.env.clock() - self.t0

    def say(self, msg):
        self.log_fn(f"[AF] t={self.elapsed():5.1f}s  {msg}")

    def check(self):
        if self.stop_event is not None and self.stop_event.is_set():
            raise _Abort("stopped by user", user=True)
        if self.elapsed() - self.t_waited > self.S.max_seconds:
            raise _Abort(f"reached the time limit of {self.S.max_seconds:.0f} s")

    def _result(self, ok, msg, score=float("nan"), quality="none", top=None):
        r = FocusResult(ok, msg, round(self.stage.c, 9), score, self.start_score, self.frames_used,
                        self.elapsed(), quality, top.ratio if top is not None else float("nan"),
                        top.fwhm if top is not None else float("nan"), self.units, list(self.notes))
        self.log_fn(f"[AF] {msg}  ({self.frames_used} frames, {self.elapsed():.1f} s)")
        return r

    # ------------------------------------------------------------------ frames
    def _frames(self):
        """Valid frames (taken after the last fast move had settled) as (t, position, score)."""
        self.feed.poll()
        out = []
        while True:
            fr = self.feed.pop()
            if fr is None:
                break
            t, s = fr
            if self._rec is not None and len(self._rec) < 2000:
                self._rec.append((t, _log(s)))
            if t - self.lag < self.stage.t_quiet:
                continue
            self.frames_used += 1
            pos = self.stage.pos_at(t - self.lag)
            if len(self.prof) < 6000:
                self.prof.append((pos, _log(s)))
            out.append((t, pos, s))
        if not out and self.feed.stalled(self.stall_s(), self.S.first_frame_timeout):
            raise _Stall()
        return out

    def stall_s(self):
        return max(self.S.stall_seconds, self.S.stall_frames / max(self.fps, 0.1))

    def _wait_camera(self):
        """The camera feed froze: hold still and wait for it to come back."""
        self.stalls += 1
        S = self.S
        t_wait = self.env.clock()
        never = self.feed.total == 0
        what = self.src_name()
        tip = getattr(self.src, "stall_tip", None) or "Is the camera running, with live view on?"
        if never:
            self.say(f"no frames from the camera ({what}) yet - waiting for it. ({tip})")
        else:
            self.say(f"camera feed has stopped (no new frames for {self.stall_s():.1f} s) - waiting for it to come "
                     f"back. ({tip})")
        last_say = t_wait
        good = 0
        while True:
            self.check()
            self.feed.poll()
            got = len(self.feed.queue)
            self.feed.clear()
            good = good + got if got else 0
            if good >= 5:
                break
            waited = self.env.clock() - t_wait
            if waited > S.stall_wait:
                raise _Abort(f"the camera feed did not come back ({what})")
            if never and self.feed.errors > 0 and self.feed.fail > 0 and waited > 15.0:
                raise _Abort(f"the camera ({what}) cannot be reached: {self.feed.last_error}")
            if self.env.clock() - last_say > 10:
                self.say(f"still waiting for the camera ({waited:.0f} s)...")
                last_say = self.env.clock()
            self.env.sleep(0.2)
        self.feed.newest = self.env.clock()
        self.stage.t_quiet = self.env.clock() + 0.5
        self.t_waited += self.env.clock() - t_wait
        self.say(f"camera is back after {self.env.clock() - t_wait:.0f} s - repeating the step it was in")
        if not never:
            self.notes.append("the camera feed froze and came back")

    def src_name(self):
        try:
            return str(self.src.describe())
        except Exception:
            return type(self.src).__name__

    def _guarded(self, fn, *a, **kw):
        while True:
            try:
                return fn(*a, **kw)
            except _Stall:
                if self.stalls >= self.S.max_stalls:
                    raise _Abort("the camera feed keeps freezing")
                self._wait_camera()

    # ------------------------------------------------------------------ at rest
    def _calibrate_rest(self):
        """Read ~10 frames at rest: noise, frame rate and the starting level."""
        S = self.S
        t0 = self.env.clock()
        # Frames that show the moment the autofocus was called or later ("fresh") set the starting level: right
        # after a turret change or a stage move the frames from just before still show the old view. The older
        # frames are only used too if they agree with the fresh ones.
        t_fresh = t0 + (self.lag if self.lag_known else min(self.lag, S.camera_lag_guess))
        old, fresh, times = [], [], []

        def agree():
            if len(old) < 4 or len(fresh) < 3:
                return False
            return abs(_median(old) - _median(fresh)) <= max(0.03, 5.0 * self._rest_noise(old + fresh))

        while True:
            self.check()
            self.feed.poll()
            while True:
                fr = self.feed.pop()
                if fr is None:
                    break
                if fr[0] >= t_fresh:
                    fresh.append(_log(fr[1]))
                    times.append(fr[0])
                elif fr[0] >= t0 - 0.6:
                    old.append(_log(fr[1]))
                    times.append(fr[0])
            el = self.env.clock() - t0
            n = len(fresh)
            if n >= 6 or (n >= 3 and agree()) or (n >= 4 and el > 1.2) or (n >= 3 and el > 3.5) or el > 12.0:
                break
            if el > S.first_frame_timeout and n < 3 and self.feed.stalled(self.stall_s(), S.first_frame_timeout):
                raise _Stall()
            self.env.sleep(0.03)
        if len(fresh) < 3:
            raise _Stall()
        vals = old + fresh if agree() else fresh
        self.frames_used += len(vals)
        self.rest_y = vals[-8:]
        self.start_score = math.exp(_median(vals))
        d = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
        sig = _sigma_mad(d) / math.sqrt(2.0) if len(d) >= 4 else 0.01
        self.sigma = _clamp(sig, 0.003, 0.08)
        if len(times) >= 4:
            dt = _median([times[i + 1] - times[i] for i in range(len(times) - 1)])
            if dt > 0:
                self.fps = _clamp(1.0 / dt, 0.5, 500.0)
        self.v_sweep = self._sweep_speed()
        self.say(f"start score={self.start_score:.4g}  noise={self.sigma * 100:.1f}%  frames={self.fps:.1f}/s  "
                 f"camera delay={self.lag:.2f}s{'' if self.lag_known else ' (assumed)'}  "
                 f"peak width={self.fw(self.W)} {self.units}{'' if self.W_known else ' (guess)'}  "
                 f"play={self.fw(self.B)}{'' if self.B_known else ' (assumed)'}")
        return self.start_score

    @staticmethod
    def _rest_noise(vals):
        d = [vals[i + 1] - vals[i] for i in range(len(vals) - 1)]
        return _clamp(_sigma_mad(d) / math.sqrt(2.0) if len(d) >= 4 else 0.01, 0.003, 0.08)

    def _sweep_speed(self):
        S, W = self.S, self.W
        fpw = S.sweep_frames_per_width if self.W_known else S.sweep_frames_per_width_new
        v = W * self.fps / fpw
        v_lo = min(S.sweep_min_widths_per_s * W, W * self.fps / S.sweep_min_frames_per_width)
        v = _clamp(v, v_lo, S.sweep_max_widths_per_s * W)
        r = getattr(self.drive, "rate", None)
        if r:
            v = min(v, 0.8 * float(r))
        return max(v, 1e-9)

    def margin(self):
        """Final approach to a precise position: an upward move of at least this much."""
        S, W = self.S, self.W
        return max(S.approach_play * self.B + S.approach_extra * W, S.approach_min * W, 2 * self.res)

    def sweep_margin(self):
        """Back-off before a measured sweep: more than the play, so the play is taken up first."""
        S, W = self.S, self.W
        return max(self.B + S.sweep_extra * W, S.sweep_min * W, 2 * self.res)

    def _pace_sizes(self, v):
        rate = getattr(self.drive, "rate", None)
        nmin = max(self.res, self.stage.q(v * 0.025))
        nmax = max(nmin, self.stage.q(float(rate) * 0.036) if rate else self.stage.q(v * 0.09))
        return nmin, nmax

    # ------------------------------------------------------------------ one measured leg
    def _leg(self, d, v, det, obs_limit, on_peak=None, speed_fn=None, on_progress=None, extend_noted=0.0):
        """
        Move in direction d at v units/s, feed every frame to `det`, until a peak is confirmed (and on_peak says
        stop), the detector reports downhill, or the OBSERVED position reaches obs_limit. Returns
        (event, observed_extent).
        """
        S = self.S
        st = self.stage
        nmin, nmax = self._pace_sizes(v)
        pace = _Pace(st, self.env, d, v, nmin, nmax)
        u_obs = None
        ext = 0
        at_limit_since = None
        while True:
            self.check()
            for t, c, s in self._frames():
                ev = det.add(c, s)
                u_obs = d * c
                if ev is None:
                    continue
                if ev[0] == "peak":
                    ev[1].lag_used = self.lag
                    ev[1].unc = S.peak_unc * self.W
                    if on_peak is None or on_peak(ev[1]):
                        return ev, u_obs
                    det.reset_candidate()
                    if extend_noted and ev[1].L >= S.medium_peak:
                        # a peak noted but not taken: see a little beyond it before the sweep ends
                        obs_limit = max(obs_limit, d * ev[1].pos + extend_noted)
                else:
                    return ev, u_obs
            if on_progress is not None and u_obs is not None:
                pk = on_progress(u_obs)
                if pk is not None:
                    return ("peak", pk), u_obs
            if u_obs is not None and u_obs >= obs_limit:
                pend = det.pending(ext) if isinstance(det, _MultiDet) else det.pending()
                if ext < S.max_ext and pend:
                    obs_limit += S.ext_step * self.W
                    ext += 1
                else:
                    return ("done", None), u_obs
            if speed_fn is not None:
                nv = speed_fn(st.c)
                if nv != pace.v:
                    pace.v = nv
                    pace.nmin, pace.nmax = self._pace_sizes(nv)
                    pace.set_speed(nv)
            moved = pace.step()
            if not moved and st.room(d) < max(0.5 * self.res, 0.0):
                at_limit_since = at_limit_since or self.env.clock()
                if self.env.clock() - at_limit_since > self.lag + 0.6:
                    return ("done", None), u_obs

    # ------------------------------------------------------------------ detectors
    def _sweep_dets(self, v, allow_downhill=False):
        S, W = self.S, self.W
        sigma = self.sigma
        spacing = v / max(self.fps, 0.1) if v > S.sweep_min_widths_per_s * W * 0.84 else None
        fw_lo = max(self.res, 0.23 * W)
        fw_hi = 18.0 * W if self.W_known else 60.0 * W
        dets = [_Detector(+1, sigma, v, S, pthr=S.peak_min_rise, wl=S.peak_window * W, wx=1.2 * S.peak_window * W,
                          drop_frac=S.peak_drop, nfall=S.peak_fall_frames, fwhm_hint=W, fw_lo=fw_lo, fw_hi=fw_hi,
                          spacing=spacing, allow_downhill=allow_downhill),
                _Detector(+1, sigma, v, S, pthr=S.wide_min_rise, wl=S.wide_window * W, wx=1.5 * S.wide_window * W,
                          drop_frac=0.35, nfall=3, fwhm_hint=max(4.5 * W, 3 * W), fw_lo=fw_lo, fw_hi=fw_hi,
                          spacing=spacing, pending_wl=S.peak_window * W)]
        if not self.W_known:
            dets.append(_Detector(+1, sigma, v, S, pthr=S.wide_min_rise, wl=S.xwide_window * W,
                                  wx=1.5 * S.xwide_window * W, drop_frac=0.35, nfall=4, fwhm_hint=10 * W,
                                  fw_lo=fw_lo, fw_hi=fw_hi, spacing=spacing, pending_wl=S.peak_window * W))
        return _MultiDet(dets, self._strong_thr())

    # ------------------------------------------------------------------ search
    def _hint(self):
        """Where the last autofocus ended, relative to here (None if unknown)."""
        if self.expect is not None:
            return float(self.expect)
        if not self.use_last_focus:
            return None
        mem = self._memory()
        last = mem.get("last_focus")
        if last is None:
            return None
        if self.abs_pos0 is not None and mem.get("last_focus_pos") is not None:
            return float(mem["last_focus_pos"]) - self.abs_pos0
        cur = mem.get("abs")
        if cur is None:
            return None
        return float(last) - float(cur)

    def _travel(self, side):
        return self.hi if side > 0 else -self.lo

    def _reaches(self, stage_i, side):
        S, W = self.S, self.W
        tr = self._travel(side)
        end = tr - min(S.end_margin * W, 0.05 * tr)
        if stage_i == 0:
            rs = [r * W for r in S.search_reach if r * W <= S.search_range * W + 1e-9]
            return [min(r, end) for r in rs]
        if stage_i == 1:
            rs = [r * W for r in S.extended_reach if r * W <= end]
            top = max(rs) if rs else min(S.search_range * W, end)
            if end > top + S.trend_bin * W * 0.6:
                rs.append(end)
            return rs
        rs = [r * W for r in S.search_reach if r * W <= S.search_range * W + 1e-9][1:]
        return [min(r, end) for r in rs]

    def _plan(self, sp):
        s0 = sp.first_dir
        plan = []
        if sp.stage_i == 1:
            s1 = self._trend_dir(s0)
            ra, rb = self._reaches(1, s1), self._reaches(1, -s1)
            for k in range(max(len(ra), len(rb))):
                if k < len(ra):
                    plan.append((s1, ra[k]))
                if k < len(rb):
                    plan.append((-s1, rb[k]))
            return plan
        side = s0
        n = len(self.S.search_reach) if sp.stage_i == 0 else len(self.S.search_reach) - 1
        for k in range(n):
            rs = self._reaches(sp.stage_i, side)
            if k >= len(rs):
                break
            R = rs[k]
            if sp.stage_i == 0 and k == 0 and sp.r0:
                R = min(max(R, sp.r0), self._travel(side) - min(self.S.end_margin * self.W,
                                                                0.05 * self._travel(side)))
            plan.append((side, R))
            side = -side
        return plan

    def _trend_dir(self, default):
        """Which end of the range seen so far has the higher (blurred) score? +1 above, -1 below."""
        if len(self.prof) < 40:
            return default
        b = self.S.trend_bin * self.W
        bins = {}
        for c, y in self.prof:
            bins.setdefault(int(math.floor(c / b)), []).append(y)
        ks = sorted(k for k, v in bins.items() if len(v) >= 3)
        if len(ks) < 6:
            return default
        med = {k: _median(bins[k]) for k in ks}
        lo = _median([med[k] for k in ks[:3]])
        hi = _median([med[k] for k in ks[-3:]])
        if abs(hi - lo) < 3 * self.sigma:
            return default
        return +1 if hi > lo else -1

    def _in_rejected(self, pos):
        return any(lo <= pos <= hi for lo, hi in self.sp.rejected)

    def _typ_contrast(self):
        """The usual height (log) of the focus peak on this setup: learned, or the adapter's hint, or None."""
        S = self.S
        n, learned, hint = self.contrast_n, self.contrast, self.contrast_hint
        if hint is None and S.default_contrast and S.default_contrast > 1.0:
            hint = math.log(S.default_contrast)
        if learned is not None and n >= S.contrast_min_runs:
            return float(learned)
        if hint is not None:
            if learned is not None and n > 0:
                return (n * float(learned) + hint) / (n + 1)
            return hint
        return None

    def _strong_thr(self):
        return self.S.strong_peak

    def _convincing(self):
        """A peak at least this high (log) is taken at once."""
        S = self.S
        Lt = self._typ_contrast()
        if Lt is None:
            return S.strong_peak
        return max(S.strong_peak, min(S.convincing_cap, S.convincing_frac * Lt))

    def _best_seen_y(self, exclude=None):
        ys = [p.y for p in self.sp.weak if p is not exclude and not self._in_rejected(p.pos)]
        return max(ys) if ys else -1e9

    def _on_peak(self, pk):
        """Decide during a sweep whether a confirmed peak ends the search."""
        S = self.S
        tp = pk.pos
        if self._in_rejected(tp):
            return False
        conv = self._convincing()
        sharper_elsewhere = self._best_seen_y() > pk.y + S.y_margin or self._sharper_frames(pk)
        if pk.L >= conv and not sharper_elsewhere:
            if not self._near_expected(tp) and self._rising_beyond(pk):
                self.sp.weak.append(pk)
                return False
            self.say(f"peak at {self.fx(tp)} {self.units} ({pk.ratio:.2f}x its surroundings, width "
                     f"{self.fw(pk.fwhm)}) -> stopping and going back to it")
            return True
        typ = self._typ_contrast()
        if pk.L >= S.near_min_peak and self._near_expected(tp) and not self._too_wide(pk) and typ is None \
                and not sharper_elsewhere:
            self.say(f"peak at {self.fx(tp)} {self.units} ({pk.ratio:.2f}x, width {self.fw(pk.fwhm)}) close to where "
                     "it started -> stopping and going back to it")
            return True
        if pk.L >= S.weak_peak:
            if typ is not None and pk.L >= S.strong_peak:
                self.say(f"peak at {self.fx(tp)} {self.units} ({pk.ratio:.2f}x) - weaker than the usual focus here "
                         f"(~{math.exp(typ):.1f}x): noted, looking around it before using it")
            else:
                self.say(f"weak peak at {self.fx(tp)} {self.units} ({pk.ratio:.2f}x) noted - looking for a stronger one")
            self.sp.weak.append(pk)
        return False

    def _near_expected(self, pos, tight=False):
        S, W = self.S, self.W
        z = (0.5 if tight else 1.0)
        if abs(pos) <= z * S.near_zone * W:
            return True
        return self.hint_rel is not None and abs(pos - self.hint_rel) <= z * S.hint_zone * W

    def _ripe_candidate(self):
        """
        A peak found earlier that was not taken at once: is it safe to use now?
          - it must be the sharpest picture seen, with nothing sharper anywhere else, and the blurred background
            must not still be rising at the ends of the range searched so far;
          - a clear peak (strong) is used at once if it sits at the top of the blurred hump; otherwise once the
            other side has been searched as far as it is (plus a margin);
          - a medium peak once both sides were searched 1.5 times as far as it is (near the start: just past it).
        """
        S, W = self.S, self.W
        sp = self.sp
        cands = [p for p in sp.weak if not self._in_rejected(p.pos) and not self._too_wide(p)]
        if not cands:
            return None
        best = self._pick(cands)
        d = abs(best.pos)
        if self._sharper_frames(best) or self._rising_beyond(best):
            return None
        near = self._typ_contrast() is not None and self._near_expected(best.pos)
        if best.L >= S.strong_peak:
            if self._hump_top(best):
                self.say(f"the peak at {self.fx(best.pos)} is the sharpest place and sits on top of the blurred "
                         "background -> using it")
                return best
            need = max(d + S.near_cover * W, S.near_zone * W) if near else max(d + 4.0 * W, S.medium_cover_min * W)
        elif best.L >= S.medium_peak:
            if near:
                need = max(d + S.near_cover * W, S.near_zone * W)
            else:
                need = max(S.medium_cover_min * W, S.medium_cover * d)
        elif S.weak_ripe and best.L >= S.weak_peak:
            need = max(S.weak_cover_min * W, S.weak_cover * d)
            for p in cands:
                if p is not best and abs(p.pos - best.pos) > S.weak_separate * W and p.L >= 0.7 * best.L:
                    return None
        else:
            return None
        # a side that ends at the travel limit counts as fully covered
        cov_up = sp.ext[+1] if sp.ext[+1] < self._travel(+1) - 2 * W else float("inf")
        cov_dn = sp.D if sp.D < self._travel(-1) - 2 * W else float("inf")
        cover = min(cov_up, cov_dn)
        return best if cover >= need else None

    def _sharper_frames(self, cand):
        """True if pictures clearly sharper than the top of `cand` were seen somewhere else."""
        W = self.W
        lim = cand.y + self.S.y_margin + 2.0 * self.sigma
        n = 0
        for c, y in self.prof:
            if y > lim and abs(c - cand.pos) > 2.5 * W:
                n += 1
                if n >= 3:
                    return True
        return False

    def _hump_top(self, cand):
        """True if `cand` sits at the top of the blurred background: the blurred level next to it (2-6.5 widths
        away, on both sides) is as high as anywhere else seen."""
        W = self.W
        bins = self._floor_bins(also=cand)
        if len(bins) < 4:
            return False
        left = [b[1] for b in bins if cand.pos - 6.5 * W <= b[0] <= cand.pos - 2.0 * W]
        right = [b[1] for b in bins if cand.pos + 2.0 * W <= b[0] <= cand.pos + 6.5 * W]
        if not left or not right:
            return False
        near = max(left + right)
        far = [b[1] for b in bins if abs(b[0] - cand.pos) > 6.5 * W]
        tol = max(0.01, 1.5 * self.sigma)
        if far and max(far) > near + tol:
            return False
        # the hump must really fall away on both sides (else it is a plateau and says nothing)
        lo_side = min(b[1] for b in bins if b[0] < cand.pos) if any(b[0] < cand.pos for b in bins) else near
        hi_side = min(b[1] for b in bins if b[0] > cand.pos) if any(b[0] > cand.pos for b in bins) else near
        return near - lo_side > tol and near - hi_side > tol

    def _too_wide(self, pk):
        if not self.W_known:
            return False
        return pk.fwhm > self.S.max_fwhm * self.W

    def _sweep(self, side, R, first=False):
        """One measured sweep, always UPWARD with the play taken up first. side=+1 explores above the start up
        to R; side=-1 explores below it down to -R (starts there and measures upward to what was seen)."""
        S, W = self.S, self.W
        sp = self.sp
        st = self.stage
        lead = max(S.lead_in * W, self.v_sweep * (self.lag + self.mech_settle) + S.lead_extra * W)
        seed_rest = False
        if side > 0:
            if sp.sweeps_done == 0 and abs(st.c) < 0.5 * self.res:
                start = None
                seed_rest = True
            else:
                start = sp.ext[+1] - lead
            limit = float(R)
            what = f"above the start, up to {self.fx(R)}"
        else:
            low = sp.dn_resume if sp.dn_resume is not None else -float(R) - W
            start = low - lead
            overlap = (self.B if sp.D <= 0 else 0.0) + 0.36 * W
            limit = -sp.D + overlap
            what = f"below the start, down to {self.fx(-R)}"
        if start is not None and abs(st.c - start) > self.res:
            st.goto(start, approach=+1, margin=self.sweep_margin())
        det = self._sweep_dets(self.v_sweep, allow_downhill=(first and side > 0))
        if seed_rest:
            det.push_rest(self.rest_y[-6:])
        self.say(f"sweeping up from {self.fx(st.c)} to {self.fx(limit)} (looking {what}) at "
                 f"{self.fw(self.v_sweep)} {self.units}/s")
        n_weak0 = len(sp.weak)
        checked = set()

        def progress(u, n_weak0=n_weak0, checked=checked):
            # a peak noted on this sweep: once the sweep is 5 widths past it, see whether it can be used already
            for i in range(n_weak0, len(sp.weak)):
                p = sp.weak[i]
                if i not in checked and u >= p.pos + 5.0 * W:
                    checked.add(i)
                    best = self._ripe_candidate()
                    if best is not None:
                        sp.weak = [w for w in sp.weak if w is not best]
                        return best
            return None

        ev, u_obs = self._leg(+1, self.v_sweep, det, limit, on_peak=self._on_peak, on_progress=progress,
                              extend_noted=6.5 * W)
        if side > 0:
            if u_obs is not None:
                sp.ext[+1] = max(sp.ext[+1], min(u_obs, R + S.ext_step * W * S.max_ext))
            if st.room(+1) < 0.5 * self.res:
                sp.ext[+1] = max(sp.ext[+1], self._travel(+1))
        else:
            if ev[0] == "done":
                sp.D = max(sp.D, float(R))
                sp.dn_resume = None
                if st.c - self.lo <= R + W and R >= self._travel(-1) - 3 * W:
                    sp.D = max(sp.D, self._travel(-1))
            elif u_obs is not None:
                sp.dn_resume = min(u_obs, limit)
        sp.sweeps_done += 1
        return ev

    def _search(self):
        """Zig-zag search; returns a _Peak (the one to refine) or None."""
        S, W = self.S, self.W
        sp = self.sp
        while True:
            self.check()
            if not sp.queue:
                if sp.weak and sp.stage_i >= 1:
                    best = self._best_weak()
                    if best is not None:
                        self.say(f"no strong peak anywhere in the range - using the best one at {self.fx(best.pos)} "
                                 f"({best.ratio:.2f}x)")
                        self.notes.append("used the best of the weak peaks in the whole range")
                        sp.weak = []
                        return best
                sp.stage_i += 1
                if sp.stage_i == 2 and self.W_known and self._flat_everywhere():
                    self.say("the picture looked the same everywhere (no structure at all) - not searching again")
                    return None
                if sp.stage_i >= 3:
                    return None
                if sp.stage_i == 1:
                    self.say(f"nothing within +/-{self.fw(min(S.search_range * W, max(self.hi, -self.lo)))} "
                             f"{self.units} -> extending the search to the ends of the allowed travel")
                if sp.stage_i == 2:
                    self.say("still nothing -> sweeping the range again, slower, in case the peak is very narrow")
                    sp.ext = {+1: 0.0, -1: 0.0}
                    sp.D = 0.0
                    sp.dn_resume = None
                    self.v_sweep = max(S.sweep_min_widths_per_s * W * 0.6, self.v_sweep * S.retry_speed_factor)
                sp.queue = self._plan(sp)
                continue
            side, R = sp.queue[0]
            if sp.cover(side) >= R - self.res:
                sp.queue.pop(0)
                continue
            first = (sp.sweeps_done == 0)
            ev = self._sweep(side, R, first=first)
            if ev[0] == "peak":
                return ev[1]
            if ev[0] == "downhill":
                self.say("the score is falling -> the focus is probably below: searching there first")
                if len(sp.queue) > 1 and sp.queue[1][0] == -side:
                    sp.queue.insert(0, sp.queue.pop(1))
                else:
                    sp.queue.insert(0, (-side, max(R, min(5.45 * W, self._travel(-side)))))
                if sp.D < 2.73 * W:
                    sp.queue.insert(0, (-side, min(2.73 * W, self._travel(-side))))
                continue
            sp.queue.pop(0)
            if sp.weak:
                best = self._ripe_candidate()
                if best is not None:
                    self.say(f"nothing stronger within +/-{self.fw(min(sp.ext[+1], sp.D))} {self.units} - using the "
                             f"peak at {self.fx(best.pos)} ({best.ratio:.2f}x)")
                    sp.weak = []
                    return best

    def _flat_everywhere(self):
        """True if no frame anywhere stood clearly above the rest (nothing in view that could be focused)."""
        ys = sorted(y for _, y in self.prof)
        if len(ys) < 50:
            return False
        med = ys[len(ys) // 2]
        top = ys[-3]
        return top - med < max(0.05, 6.0 * self.sigma)

    def _best_weak(self):
        cands = [p for p in self.sp.weak if not self._in_rejected(p.pos)]
        if not cands:
            return None
        return self._pick(cands)

    def _pick(self, cands):
        """The candidate where the picture got sharpest (the focus is the sharpest picture there is). Peaks that
        stand out more win a near tie."""
        top = max(p.y for p in cands)
        close = [p for p in cands if p.y >= top - self.S.y_margin]
        return max(close, key=lambda p: (round(p.L, 2), -abs(p.pos)))

    def _floor_bins(self, also=None):
        """The blurred background of the scene: in bins of 2 peak widths, the 25th percentile of the log-score
        (narrow peaks hardly move it), leaving out the tops of the peaks seen (and of `also`).
        Returns [(centre, level, count)] sorted by position."""
        W = self.W
        b = 2.0 * W
        excl = [p.pos for p in self.sp.weak] + [0.5 * (lo + hi) for lo, hi in self.sp.rejected]
        if also is not None:
            excl.append(also.pos)
        bins = {}
        for c, y in self.prof:
            if any(abs(c - e) < 1.2 * W for e in excl):
                continue
            bins.setdefault(int(math.floor(c / b)), []).append(y)
        out = []
        for k, v in bins.items():
            if len(v) >= 4:
                v.sort()
                out.append(((k + 0.5) * b, v[int(0.25 * (len(v) - 1))], len(v)))
        out.sort()
        return out

    def _rising_beyond(self, cand):
        """
        True if the blurred background says the real focus is probably further out than `cand` (the real focus
        sits on top of the blur hump; a stray peak sits on its slope):
          - beyond `cand` (away from the start) the background is higher than just before it, or
          - nothing has been seen beyond `cand` yet while the background was still climbing on the way to it, or
          - at one end of the range searched so far the background is still rising outward and is already about
            as high as around `cand`.
        """
        W = self.W
        bins = self._floor_bins(also=cand)
        if len(bins) < 4:
            return False
        floor_c = cand.y - cand.L
        tol = max(0.02, 1.5 * self.sigma)
        out = 1 if cand.pos >= 0 else -1
        beyond = [b for b in bins if out * (b[0] - cand.pos) >= 1.2 * W]
        before = sorted((b for b in bins if 1.2 * W <= out * (cand.pos - b[0]) <= 8.0 * W), key=lambda b: out * b[0])
        why = None
        if beyond:
            near_beyond = [b[1] for b in beyond if out * (b[0] - cand.pos) <= 8.0 * W] or [beyond[0][1]]
            ref = max([b[1] for b in before] + [floor_c])
            if max(near_beyond) > ref + tol:
                why = beyond[0][0]
        elif len(before) >= 3 and before[-1][1] - before[0][1] > tol:
            why = cand.pos + out * 2.0 * W
        if why is None:
            for side in (+1, -1):
                if side > 0:
                    inner, mid, outer = bins[-3], bins[-2], bins[-1]
                else:
                    inner, mid, outer = bins[2], bins[1], bins[0]
                rise = outer[1] - inner[1]
                if rise > tol and outer[1] >= mid[1] - 0.25 * tol and outer[1] > floor_c - tol:
                    if (side > 0 and outer[0] > cand.pos) or (side < 0 and outer[0] < cand.pos):
                        why = outer[0]
                        break
        if why is None:
            return False
        key = round(cand.pos, 3)
        if key not in self._said_rising:
            self._said_rising.add(key)
            self.say(f"the blurred picture still gets sharper towards {self.fx(why)} - not settling on the peak at "
                     f"{self.fx(cand.pos)} yet")
        return True

    # ------------------------------------------------------------------ the play of the drive
    def _measure_play(self, pk):
        """
        Sweep DOWN across the peak just found going up. Going down, the drive first turns through its play
        before the focus moves, so the peak shows up lower by the play. Returns the raw difference (it still
        contains 2 * speed * camera-delay error, which is removed once the delay is known), or None.
        """
        S, W = self.S, self.W
        st = self.stage
        fwhm = _clamp(pk.fwhm, max(2 * self.res, 0.27 * W), 13.6 * W)
        v = self.v_sweep
        lead = max(S.lead_in * W, v * (self.lag + self.mech_settle) + S.lead_extra * W)
        top = pk.pos + 2.5 * fwhm + lead
        if top > self.hi - self.res:
            self.say("not enough room above the peak to measure the play - skipped")
            return None
        b_max = max(4.0 * self.B, 3.0 * W)
        bottom = pk.pos - 2.0 * fwhm - b_max
        self.say(f"measuring the play of the drive: sweeping down across the peak at {self.fx(pk.pos)}")
        st.goto(top, approach=+1, margin=self.sweep_margin())
        det = _Detector(-1, self.sigma, v, S, pthr=max(0.07, 0.45 * pk.L), wl=max(S.peak_window * W, 2.2 * fwhm),
                        wx=max(3.64 * W, 3 * fwhm), drop_frac=0.3, nfall=3, fwhm_hint=fwhm,
                        fw_lo=max(self.res, 0.23 * W), fw_hi=18.0 * W, spacing=None)
        best = []

        def accept(p2):
            if p2.L >= 0.5 * pk.L and 0.4 <= p2.fwhm / fwhm <= 2.5:
                best.append(p2)
                return True
            return False

        ev, u_obs = self._leg(-1, v, det, obs_limit=-bottom, on_peak=accept)
        if ev[0] != "peak" or not best:
            self.say("the peak did not show up on the way down - the play stays assumed")
            return None
        p_dn = best[0].pos                       # commanded position (down coordinates) of the top
        raw = pk.pos - p_dn
        self.say(f"peak seen {self.fw(raw)} {self.units} lower on the way down (play + camera-delay effect)")
        return raw, v

    # ------------------------------------------------------------------ the slow, precise pass
    def _fine(self, pk):
        """Cross the peak slowly from below. Returns a refined _Peak (up coordinates) or None."""
        S, W = self.S, self.W
        st = self.stage
        fwhm = _clamp(pk.fwhm, max(2 * self.res, 0.27 * W), 13.6 * W)
        p_up = pk.pos
        unc = pk.unc
        v_f = _clamp(self.fps * fwhm / S.slow_frames_per_width, S.slow_min_widths_per_s * W,
                     max(S.slow_max_widths_per_s * W, min(S.slow_cap_widths_per_s * W, S.slow_cap_fwhm_per_s * fwhm)))
        r = getattr(self.drive, "rate", None)
        if r:
            v_f = min(v_f, 0.8 * float(r))
        lagx = 0.0 if self.lag_known else S.lag_excess * pk.v
        for attempt in (1, 2):
            below = unc + 0.6 * fwhm + 0.27 * W
            c_start = p_up - below - lagx - 1.14 * W
            tol = unc + lagx + max(1.36 * W, 1.2 * fwhm)
            self.say(f"slow pass over the peak (expected near {self.fx(p_up)}, speed {self.fw(v_f)} {self.units}/s)")
            st.goto(c_start, approach=+1, margin=self.margin())
            det = _Detector(+1, self.sigma, v_f, S, pthr=max(0.07, 0.45 * pk.L), wl=max(S.peak_window * W, 2.2 * fwhm),
                            wx=max(3.64 * W, 3 * fwhm), drop_frac=0.3, nfall=3, fwhm_hint=fwhm,
                            fw_lo=max(self.res, 0.23 * W), fw_hi=18.0 * W if self.W_known else 60 * W, spacing=None)
            slow_from = p_up - below
            v_mid = min(self.v_sweep, S.mid_max_widths_per_s * W)
            others = []

            def speed_fn(c, slow_from=slow_from, v_mid=v_mid, v_f=v_f):
                return v_f if c >= slow_from else v_mid

            def accept(pk2, p_up=p_up, tol=tol, others=others, fwhm=fwhm):
                if abs(pk2.pos - p_up) <= tol and 0.4 <= pk2.fwhm / fwhm <= 2.5 and pk2.L >= 0.65 * pk.L:
                    return True
                others.append(pk2)
                return False

            limit = p_up + unc + 1.3 * fwhm + 1.14 * W
            ev, u_obs = self._leg(+1, v_mid, det, obs_limit=limit + attempt * 1.82 * W, speed_fn=speed_fn,
                                  on_peak=accept)
            for o in others:
                if o.L >= S.weak_peak:
                    self.sp.weak.append(o)
            if ev[0] == "peak":
                fine = ev[1]
                fine.unc = max(2.0 * self.res, 0.09 * W)
                return fine
            self.say("the slow pass did not find the peak where expected" + (" - widening" if attempt == 1 else ""))
            unc = unc * 2.5 + 0.68 * W
            fwhm = fwhm * 1.3
        return None

    # ------------------------------------------------------------------ settled readings
    def settle_gate(self):
        eff = self.lag if self.lag_known else self.S.lag_unknown
        return max(self.settle_floor, 0.9 * eff + 0.08)

    def _still_test(self, ts, ys):
        S = self.S
        k = len(ys)
        t_mid = 0.5 * (ts[0] + ts[-1])
        x = [t - t_mid for t in ts]
        slopes = []
        for i in range(k):
            for j in range(i + 1, k):
                dx = x[j] - x[i]
                if dx > 1e-6:
                    slopes.append((ys[j] - ys[i]) / dx)
        b = _median(slopes) if slopes else 0.0
        y0 = _median([y - b * xi for y, xi in zip(ys, x)])
        res = [y - (y0 + b * xi) for y, xi in zip(ys, x)]
        s = max(1.4826 * _median([abs(r) for r in res]), 0.5 * self.sigma, 0.004)
        sxx = sum(xi * xi for xi in x)
        se_b = 1.25 * s / math.sqrt(max(sxx, 1e-9))
        s_mean = 1.2 * s / math.sqrt(k)
        bound = (abs(b) + S.still_se * se_b) * S.tau_max
        still = bound <= max(S.still_tol, s_mean)
        sd = math.sqrt(s_mean ** 2 + (0.5 * abs(b) * S.tau_max) ** 2)
        return still, y0, sd

    def _settled_score(self, t_stop, timeout=1.6, extra=1.6):
        """Log-score once the picture has stopped changing after a move that ended at t_stop. Returns
        (y, sd, still)."""
        t_first = t_stop + self.settle_gate()
        t_soft = t_first + max(timeout, 8.0 / max(self.fps, 0.1))
        t_hard = t_soft + max(extra, 8.0 / max(self.fps, 0.1))
        m = max(5, int(math.ceil(0.33 * self.fps)))
        ts, ys = [], []
        best = None
        while True:
            self.check()
            for t, c, s in self._frames():
                if t >= t_first:
                    ts.append(t)
                    ys.append(_log(s))
            n = len(ys)
            if n >= m:
                k = m
                while k <= min(n, 3 * m):
                    still, y, sd = self._still_test(ts[-k:], ys[-k:])
                    if best is None or sd < best[1] or still:
                        best = (y, sd, still)
                    if still:
                        return y, sd, True
                    k += m
                if self.env.clock() > t_hard:
                    break
            elif self.env.clock() > t_hard + 2.0:
                raise _Stall()
            self.env.sleep(0.02)
        if best is None:
            raise _Stall()
        return best[0], max(best[1], 0.03), False

    def landing_gate(self):
        eff = self.lag if self.lag_known else self.S.lag_slow
        return max(self.settle_floor, eff + 0.1)

    def _land(self, target, fwhm):
        W = self.W
        slow_tail = _clamp(0.8 * fwhm, 0.45 * W, 1.8 * W)
        slow_tail = max(self.res, self.stage.q(slow_tail))
        self.stage.goto(target, approach=+1, margin=self.margin(), slow_tail=slow_tail)
        return self._settled_score(self.env.clock() + max(0.0, self.landing_gate() - self.settle_gate()))

    def _step_to(self, target, y_prev=None):
        """Small upward move to `target`, then a settled reading. The frames around the move also measure the
        camera delay directly: the picture changes half-way when the move was half done, one delay earlier."""
        st = self.stage
        n0 = len(st.segs)
        self._rec = []
        try:
            st.goto(target, approach=0, fast=False)
            r = self._settled_score(self.env.clock())
        finally:
            rec, self._rec = self._rec, None
        if y_prev is not None and r[2] and len(st.segs) > n0:
            self._lag_from_step(rec, st.segs[n0][0], st.segs[-1][1], y_prev, r[0])
        return r

    def _lag_from_step(self, rec, m0, m1, y0, y1):
        dy = y1 - y0
        if abs(dy) < max(6.0 * self.sigma, 0.03) or m1 - m0 > 0.6:
            return
        mid = 0.5 * (y0 + y1)
        sg = 1.0 if dy > 0 else -1.0
        fr = [(t, y) for t, y in rec if t >= m0 - 0.05]
        for i in range(1, len(fr) - 1):
            if sg * (fr[i][1] - mid) > 0 and sg * (fr[i + 1][1] - mid) > 0 and sg * (fr[i - 1][1] - mid) <= 0:
                est = 0.5 * (fr[i - 1][0] + fr[i][0]) - 0.5 * (m0 + m1)
                if -0.05 <= est <= 3.0:
                    self.lag_obs.append(max(0.0, est))
                return

    def _read_again(self, r):
        if r[2]:
            return r
        r2 = self._guarded(self._settled_score, self.env.clock() - self.settle_gate())
        return r2 if (r2[2] or r2[1] < r[1]) else r

    # ------------------------------------------------------------------ the finish
    @staticmethod
    def _bump_post(pts, sds, F, w0, P0, sig_P=0.15, sig_w=0.35, x_prior=None, gstep=0.5):
        """
        What the settled readings say about the top of the focus bump  score = F + P * shape((x - xt) / w), where
        the shape runs from pointed to Gaussian (_BETAS) with the half-width of a Gaussian of sigma w.
        Grid search over (xt, P, w, shape) -> posterior of xt. Returns dict(mean, sd, q25, ytop, w, rms) or None.
        """
        xs = [x for x, _ in pts]
        ys = [y for _, y in pts]
        ss = [max(s, 0.012) for s in sds]
        n = len(xs)
        lo = min(xs) - 2.0 * w0
        hi = max(xs) + 4.0 * w0
        nx = int((hi - lo) / gstep) + 1
        if nx > 4000:
            gstep = (hi - lo) / 4000.0
            nx = 4001
        if _np is not None:
            grid = lo + gstep * _np.arange(nx)
            X = _np.asarray(xs)[:, None]
            Y = _np.asarray(ys)[:, None]
            SS = _np.asarray(ss)[:, None]
            adx = _np.abs(X - grid[None, :])
            pri = ((grid - x_prior[0]) / x_prior[1]) ** 2 if x_prior is not None else 0.0
            costs, lps, ws, c0s = [], [], [], []
            for beta in _BETAS:
                for kw in range(-3, 4):
                    wm = math.exp(0.22 * kw)
                    w = w0 * wm
                    a = w * 1.1774100225 / (0.6931471806 ** (1.0 / beta))   # same half-width as a Gaussian of sigma w
                    g = _np.exp(-(adx / a) ** beta)
                    c_w = (math.log(wm) / sig_w) ** 2
                    for pm in (0.75, 0.87, 1.0, 1.15, 1.32):
                        P = P0 * pm
                        c0 = c_w + (math.log(pm) / sig_P) ** 2
                        r = (Y - _np.log(F + P * g)) / SS
                        costs.append(c0 + pri + (r * r).sum(axis=0))
                        lps.append(math.log(F + P))
                        ws.append(w)
                        c0s.append(c0)
            C = _np.vstack(costs)
            cmin = float(C.min())
            Wt = _np.where(C - cmin > 40.0, 0.0, _np.exp(-0.5 * (C - cmin)))
            sw = float(Wt.sum())
            if sw <= 0.0:
                return None
            wx = Wt.sum(axis=0)
            mean = float((wx * grid).sum() / sw)
            var = float((wx * (grid - mean) ** 2).sum() / sw)
            cum = _np.cumsum(wx)
            i25 = int(_np.searchsorted(cum, 0.25 * sw))
            q25 = float(grid[min(i25, nx - 1)])
            rows = Wt.sum(axis=1)
            ytop = float((rows * _np.asarray(lps)).sum() / sw)
            wmean = float((rows * _np.asarray(ws)).sum() / sw)
            ib = int(_np.argmin(C)) // nx
            rms = math.sqrt(max(cmin - c0s[ib], 0.0) / n)
            return dict(mean=mean, sd=math.sqrt(max(var, 0.0)), q25=q25, ytop=ytop, w=wmean, rms=rms)
        # pure Python (slower; used only without numpy)
        grid = [lo + gstep * i for i in range(nx)]
        combos = []
        for beta in _BETAS:
            for kw in range(-3, 4):
                wm = math.exp(0.22 * kw)
                w = w0 * wm
                a = w * 1.1774100225 / (0.6931471806 ** (1.0 / beta))
                c_w = (math.log(wm) / sig_w) ** 2
                for pm in (0.75, 0.87, 1.0, 1.15, 1.32):
                    P = P0 * pm
                    c0 = c_w + (math.log(pm) / sig_P) ** 2
                    lp = math.log(F + P)
                    costs = []
                    for xt in grid:
                        cst = c0
                        if x_prior is not None:
                            cst += ((xt - x_prior[0]) / x_prior[1]) ** 2
                        for x, y, s in zip(xs, ys, ss):
                            g = math.exp(-(abs(x - xt) / a) ** beta)
                            r = (y - math.log(F + P * g)) / s
                            cst += r * r
                        costs.append(cst)
                    combos.append((costs, lp, w, c0))
        cmin = min(min(c[0]) for c in combos)
        wx = [0.0] * nx
        sw = swl = sww = 0.0
        best = None
        for costs, lp, w, c0 in combos:
            for i, cst in enumerate(costs):
                if cst - cmin > 40.0:
                    continue
                wt = math.exp(-0.5 * (cst - cmin))
                wx[i] += wt
                sw += wt
                swl += wt * lp
                sww += wt * w
                if best is None or cst < best[0]:
                    best = (cst, c0)
        if sw <= 0.0:
            return None
        mean = sum(wx[i] * grid[i] for i in range(nx)) / sw
        var = sum(wx[i] * (grid[i] - mean) ** 2 for i in range(nx)) / sw
        acc = 0.0
        q25 = grid[-1]
        for i in range(nx):
            acc += wx[i]
            if acc >= 0.25 * sw:
                q25 = grid[i]
                break
        return dict(mean=mean, sd=math.sqrt(var), q25=q25, ytop=swl / sw, w=sww / sw,
                    rms=math.sqrt(max(best[0] - best[1], 0.0) / n))

    def _fstep(self, w0):
        """One finishing step for a peak of width w0 (sigma)."""
        return max(self.res, self.stage.q(self.S.finish_step_sigma * w0))

    def _facc(self, w0):
        """How far in front of the top the finish may stop."""
        return max(self.S.finish_accept_sigma * w0, 0.6 * self.res)

    def _finish(self, pk, e_pos):
        """
        Closed loop on settled readings (the camera delay cancels out). Land on the rising side of the peak from
        below, then climb in small upward steps; after every reading the bump model gives the most likely top,
        and the next step covers only part of the way there, so the top is approached from below.
        Returns (top position or None, last settled score (log), verified).
        """
        S = self.S
        st = self.stage
        w0 = max(sigma_from_log_width(pk.fwhm, pk.L), self.res)
        F = math.exp(pk.y - pk.L)
        P0 = max(math.exp(pk.y) - F, 0.05 * F)
        sig_P, sig_w = S.prior_sig_p, S.prior_sig_w
        x_p = pk.pos
        x_prior = (x_p, max(0.32 * w0, S.prior_scale * e_pos) if self.lag_known else 1.5 * e_pos)
        xt = None
        y_cur = pk.y
        ytop = pk.y
        verified = False
        post = None
        action = "more"
        tgt = x_p - (self._fstep(w0) + e_pos)
        for n_land in range(S.max_landings):
            step = self._fstep(w0)
            acc = self._facc(w0)
            gstep = max(self.res / 4.0, w0 / 18.6)
            y_cur, sd, still = self._read_again(self._guarded(self._land, st.q(tgt), 2.355 * w0))
            pts = [(float(st.c), y_cur)]
            sds = [sd]
            stills = [still]
            self.say(f"reading at {self.fx(st.c)}: score {math.exp(y_cur):.4g}")
            action = "more"
            n_flat = 0                                   # readings below the peak (they do not count as readings)
            for k in range(9 + 4):
                signal = max(y for _, y in pts) - math.log(F) >= max(0.04, 4.0 * min(sds))
                if not signal:
                    n_flat = len(pts)
                n_read = len(pts) - n_flat
                post = self._bump_post(pts, sds, F, w0, P0, sig_P, sig_w, x_prior, gstep) if signal else None
                if post is None:
                    if st.c > x_p + e_pos + 3 * w0:
                        action = "lost"
                        break
                    nxt = st.c + step * 2
                else:
                    ahead = post["mean"] - st.c
                    ytop = post["ytop"]
                    if ahead < -acc:
                        action = "back"
                        break
                    amp = math.exp(ytop) - F
                    deficit = (math.exp(ytop) - math.exp(y_cur)) / amp if amp > 1e-12 else 1.0
                    if ahead <= acc and (sum(stills) >= 2 or n_read >= S.max_readings) \
                            and (post["sd"] <= max(S.done_sd_sigma * w0, self.res) or n_read >= S.max_readings
                                 or deficit <= S.close_enough):
                        action = "done"
                        break
                    reach = post["q25"] - st.c
                    nxt = st.c + _clamp(S.finish_damp * reach, max(self.res, 0.21 * w0), 2.0 * step)
                if st.room(+1) < 0.5 * self.res:
                    action = "lost" if post is None else "done"
                    break
                y_cur, sd, still = self._read_again(self._guarded(self._step_to, st.q(nxt), y_cur))
                pts.append((float(st.c), y_cur))
                sds.append(sd)
                stills.append(still)
                self.say(f"reading at {self.fx(st.c)}: score {math.exp(y_cur):.4g}")
            if action == "done" and post is not None:
                xt = post["mean"]
                self.final_sd = post["sd"] / max(w0, 1e-12)
                verified = y_cur >= ytop - max(S.verify_tol, 2.5 * sd) - 0.02
                nudge = st.q(xt - st.c)
                if verified and self.res - 1e-9 <= nudge <= acc:
                    self.stage.goto(st.c + nudge, approach=0, fast=False)
                elif not verified and self.res - 1e-9 <= nudge <= 2.0 * step:
                    # the last reading was short of the top: step onto the predicted top and check it there
                    y2, sd2, still2 = self._read_again(self._guarded(self._step_to, st.q(st.c + nudge), y_cur))
                    self.say(f"reading at {self.fx(st.c)}: score {math.exp(y2):.4g}")
                    pts.append((float(st.c), y2))
                    sds.append(sd2)
                    if y2 >= y_cur - 2.0 * max(sd, sd2):
                        y_cur, sd = y2, sd2
                        verified = y_cur >= ytop - max(S.verify_tol, 2.5 * sd) - 0.02
                if not verified:
                    # The readings went over the top (the model of the peak was off): the best reading with a lower
                    # one on each side brackets it. The top of a parabola through those three (log score) is
                    # where the top is; go there - from below - and check it.
                    br = _bracket_top(pts, sds)
                    if br is not None:
                        x_v, y_best, tol_v = br
                        x_v = st.q(x_v)
                        if abs(x_v - st.c) < 0.5 * self.res:
                            y_v = y_cur
                        elif x_v > st.c:
                            y_v, sd_v, _ = self._read_again(self._guarded(self._step_to, x_v, y_cur))
                        else:
                            y_v, sd_v, _ = self._read_again(self._guarded(self._land, x_v, 2.355 * w0))
                        self.say(f"reading at {self.fx(st.c)}: score {math.exp(y_v):.4g} (the top, between the "
                                 "readings on either side)")
                        y_cur = y_v
                        xt = float(st.c)
                        verified = y_v >= y_best - tol_v
                    elif nudge <= -self.res and n_land < S.max_landings - 1:
                        # it ended a little past the top: come back to it from below once more (a small step
                        # back down would not be reliable on a drive with play)
                        action = "back"
                if action == "done":
                    break
            if action == "back":
                xt = post["mean"]
                self.say(f"went {self.fw(st.c - xt)} {self.units} past the top - coming back")
                w0 = _clamp(post["w"], max(self.res, 0.21 * w0), 6.4 * w0)
                P0 = max(math.exp(post["ytop"]) - F, 0.05 * F)
                sig_P, sig_w = 0.08, 0.2
                tgt = xt - (self._fstep(w0) + 0.21 * w0)
                x_p = xt
                e_pos = 0.32 * w0 + post["sd"]
                x_prior = (xt, max(0.27 * w0, post["sd"] + 0.16 * w0))
                xt = None
                continue
            xt = None
            break
        if xt is None and post is not None and action == "more":
            xt = post["mean"]
            self.final_sd = post["sd"] / max(w0, 1e-12)
        return xt, y_cur, verified

    # ------------------------------------------------------------------ learning
    def _learn_after(self, coarse, fine, xt, verified):
        """Learn the camera delay, the play of the drive, the peak width and the usual contrast."""
        S = self.S
        cal = self.cal
        if not verified or xt is None:
            if self.measure_B or self.B_known:
                cal["recheck_backlash"] = True
            return
        first = cal.get("lag_n", 0) == 0
        guard = 0.4 if first else 0.25
        # 1) from the passes over the peak: a moving pass sees the top too far on by speed * (delay - delay used).
        #    Only passes fast enough for this to be precise (a slow pass turns a small position error into a large
        #    delay error) count.
        ests = []
        for pk in (coarse, fine):
            if pk is None or not pk.fit or pk.L < 0.2 or pk.npts < 4 or self._too_wide(pk):
                continue
            sd_est = max(0.06 * pk.fwhm, self.res) / max(pk.v, 1e-9)
            if sd_est > 0.08:
                continue
            d_est = pk.lag_used + (pk.pos - xt) / pk.v
            if 0.0 <= d_est <= 3.0:
                ests.append((pk, d_est))
        pick = None
        for pk, d_est in ests:
            if abs(d_est - pk.lag_used) <= guard:
                pick = d_est
                break
        if pick is None and first and len(ests) == 2 and abs(ests[0][1] - ests[1][1]) <= 0.25:
            va, vb = ests[0][0].v ** 2, ests[1][0].v ** 2
            pick = (va * ests[0][1] + vb * ests[1][1]) / (va + vb)
        # 2) measured directly from the finishing steps (when the picture changed): preferred when available
        direct = None
        if len(self.lag_obs) >= 2:
            direct = _median(self.lag_obs)
            if pick is not None and abs(pick - direct) <= 0.08:
                pick = 0.5 * (pick + direct)
            else:
                pick = direct
        lag_true = None
        if pick is not None:
            lag_true = pick
            old_lag = cal.get("lag")
            if direct is not None and old_lag is not None and abs(direct - old_lag) > 0.15:
                cal["lag_n"] = min(int(cal.get("lag_n", 0)), 1)      # a clearly different delay: re-learn quickly
            stored = ema_store(cal, "lag", pick, 0.2)
            self.say(f"camera delay measured: {pick:.3f} s (stored {stored:.3f} s)")
            if first:
                self.notes.append("first run on this setup: the camera delay was measured")
        elif self.lag_known:
            lag_true = self.lag
        # the play: remove the camera-delay part (2 * speed * (true delay - delay used)). If the delay could not be
        # measured (a very slow camera), the play is still good enough when the down-sweep was slow enough for a
        # delay error of 0.3 s to matter little.
        lag_for_b = lag_true
        if lag_for_b is None and self.B_raw is not None and coarse is not None and \
                2.0 * self.B_raw_v * 0.3 <= 0.35 * self.W:
            lag_for_b = coarse.lag_used
        if self.B_raw is not None and lag_for_b is not None and coarse is not None:
            b = self.B_raw - 2.0 * self.B_raw_v * (lag_for_b - coarse.lag_used)
            limit = 0.25 * max(self.hi, -self.lo)
            if -0.15 * self.W <= b <= limit:
                b = max(0.0, b)
                old_n = cal.get("backlash_n", 0)
                if cal.get("recheck_backlash"):
                    cal["backlash_n"] = 0
                stored = ema_store(cal, "backlash", b, max(0.5 * self.W, 0.3 * (cal.get("backlash") or b)))
                self.say(f"play of the drive measured: {self.fw(b)} {self.units} (stored {self.fw(stored)})")
                if not old_n:
                    self.notes.append(f"the play of the drive was measured: {self.fw(b)} {self.units}")
        cal["recheck_backlash"] = False
        top = fine if fine is not None else coarse
        if top is not None and not self._too_wide(top):
            old = cal.get("fwhm")
            if old is None and not self.W_known:
                self.notes.append(f"the width of the focus peak was measured: {self.fw(top.fwhm)} {self.units}")
            ema_store(cal, "fwhm", top.fwhm, 0.5 * (old if old else top.fwhm))
        if top is not None and top.L >= S.weak_peak:
            # the usual contrast rises quickly and falls slowly: a stray peak taken for the focus (weaker than the
            # real one) must not teach the next runs to accept weak peaks
            old_c = cal.get("contrast")
            n_c = int(cal.get("contrast_n", 0))
            if old_c is None:
                cal["contrast"] = round(top.L, 5)
            else:
                w = 0.5 if top.L > old_c else max(0.12, 0.5 / (n_c + 1))
                cal["contrast"] = round(old_c + (top.L - old_c) * w, 5)
            cal["contrast_n"] = min(n_c + 1, 8)
        cal["fps"] = round(self.fps, 2)

    def _can_go_direct(self, pk):
        S = self.S
        if not S.direct_finish:
            return False
        if self.cal.get("lag_n", 0) < 2 or not self.B_known or self.measure_B:
            return False
        return bool(pk.fit and pk.npts >= 4 and pk.L >= S.strong_peak)

    # ------------------------------------------------------------------ top level
    def run(self):
        S = self.S
        self.say(f"Starting universal autofocus {VERSION} ({self._describe_setup()})")
        try:
            self.feed.poll()
            self._guarded(self._calibrate_rest)
            if S.good_score is not None and self.start_score >= S.good_score:
                return self._result(True, "Already sharp enough - not moving.", self.start_score, "good")

            hint = self._hint()
            self.hint_rel = hint
            if hint is None and self.direction:
                first_dir = 1 if self.direction > 0 else -1
                self.say(f"the focus is expected {'above' if first_dir > 0 else 'below'} -> searching that way first")
            else:
                first_dir = 1 if (hint is None or hint >= 0) else -1
            r0 = abs(hint) + 2.73 * self.W if (hint is not None and abs(hint) > 1.82 * self.W) else None
            if r0:
                self.say(f"last focus was about {self.fx(hint)} {self.units} from here -> searching that way first")
            self.sp = _Search(first_dir, r0)
            self.sp.queue = self._plan(self.sp)

            coarse = fine = None
            while True:
                pk = self._guarded(self._search)
                if pk is None:
                    return self._no_focus()
                self.candidates_tried += 1
                coarse = pk
                p_coarse = pk.pos
                fine = None
                xt = None
                ok = False
                y_end = pk.y
                if self.measure_B and self.B_raw is None:
                    r = self._guarded(self._measure_play, pk)
                    if r is not None:
                        self.B_raw, self.B_raw_v = r
                        b_now = max(0.0, self.B_raw - (2.0 * self.B_raw_v * 0.15 if not self.lag_known else 0.0))
                        if b_now > self.B:
                            self.B = b_now            # use the larger margins at once
                if self._can_go_direct(pk):
                    self.say("camera delay and play are known -> going straight to the finish")
                    use = _Peak(pos=p_coarse, d=+1, v=pk.v, L=pk.L, y=pk.y, fwhm=pk.fwhm, npts=pk.npts,
                                kappa=pk.kappa, lag_used=pk.lag_used)
                    e_pos = 0.27 * self.W + pk.v * (0.06 if self.lag_known else 0.15)
                    xt, y_end, ok = self._guarded(self._finish, use, e_pos)
                    if xt is not None and ok:
                        break
                    self.say("the finish could not settle on a top from there -> doing the slow pass first")
                fine = self._guarded(self._fine, pk)
                if fine is None:
                    if self._reject_candidate(p_coarse, "that candidate was not a real peak"):
                        return self._no_focus()
                    continue
                self.say(f"sharpest point on the slow pass {self.fx(fine.pos)} (width {self.fw(fine.fwhm)} "
                         f"{self.units}, {fine.ratio:.2f}x)")
                self._adopt_width(fine)
                e_pos = 0.18 * self.W + fine.v * (0.06 if self.lag_known else 0.15)
                xt, y_end, ok = self._guarded(self._finish, fine, e_pos)
                if xt is None:
                    if self._reject_candidate(p_coarse, "no real peak there (the readings stayed flat)"):
                        return self._no_focus()
                    continue
                break

            if S.learn:
                self._learn_after(coarse, fine, xt, ok)
                if self.store is not None and self.cal != self.cal0:
                    try:
                        self.store.put(self.key, self.cal)
                    except Exception:
                        pass
            self._remember_focus()
            score = math.exp(y_end)
            top = fine if fine is not None else coarse
            good_sd = self.final_sd is not None and self.final_sd <= 0.9
            if ok and top.ratio >= 1.3 and good_sd:
                quality = "good"
            elif ok:
                quality = "fair"
                if top.ratio < 1.3:
                    self.notes.append("faint focus peak")
            else:
                quality = "poor"
                self.notes.append("the top could not be confirmed (position approximate)")
            if ok and top.ratio < 1.3:
                msg = (f"Focus found: score {score:.4g}  (faint peak, only {top.ratio:.2f}x the background: if the "
                       "picture is not sharp, run it again)")
            elif ok:
                msg = f"Focus found: score {score:.4g}  ({top.ratio:.1f}x the background)"
            else:
                msg = f"Focus found (position approximate): score {score:.4g}"
            return self._result(True, msg, score, quality, top)
        except _Abort as e:
            return self._result(False, f"Stopped: {e.reason}.")
        except Exception as e:
            self.log_fn("[AF] ERROR: " + traceback.format_exc().replace("\n", "\n[AF]   "))
            return self._result(False, f"Error: {e}")
        finally:
            self._remember_position()

    def _describe_setup(self):
        try:
            d = self.drive.describe()
        except Exception:
            d = type(self.drive).__name__
        return f"{d}, camera: {self.src_name()}"

    def _adopt_width(self, pk):
        """The peak width of this setup was only a guess: from now on use the width just measured."""
        if self.W_known or pk is None:
            return
        w = _clamp(pk.fwhm, 2.0 * self.res, 20.0 * self.W)
        if abs(w - self.W) > 0.15 * self.W:
            self.W = w
            self.stage.up_slack = 0.68 * w

    def _reject_candidate(self, p, why):
        """Returns True if the search should give up."""
        W = self.W
        self.sp.rejected.append((p - self.S.reject_zone * W, p + self.S.reject_zone * W))
        self.say(f"{why} -> carrying on with the search")
        if self.candidates_tried >= self.S.max_candidates:
            return True
        self.sp.weak = [w for w in self.sp.weak if not self._in_rejected(w.pos)]
        if not self.sp.queue:
            self.sp.queue = self._plan(self.sp)
        return False

    def _remember_position(self):
        mem = self._memory()
        try:
            mem["abs"] = float(self.abs0) + float(self.stage.c)
        except Exception:
            pass

    def _remember_focus(self):
        mem = self._memory()
        try:
            mem["last_focus"] = float(self.abs0) + float(self.stage.c)
            if self.abs_pos0 is not None:
                mem["last_focus_pos"] = self.abs_pos0 + float(self.stage.c)
        except Exception:
            pass

    def _no_focus(self):
        self.stage.goto(0, approach=+1, margin=self.margin())
        return self._result(False, "No focus signal: no peak was found within the whole range. Check: lens cap, that "
                                   "there is texture in view (edges, dust, a flake - not a blank area), that the drive "
                                   "really moves the focus, and that live view is running. Returned to the start.")


# ═══════════════════════════════════════════════════════════
#  PUBLIC ENTRY POINT
# ═══════════════════════════════════════════════════════════
def autofocus(drive, frames, settings=None, store=None, calibration_file=None, objective=None, key=None,
              log=print, stop_event=None, env=None, expect=None, direction=None, use_last_focus=True):
    """
    Find the sharpest focus and leave the drive there.

    drive             a FocusDrive (interfaces.py) - what moves the focus
    frames            a FrameSource (interfaces.py) - sharpness of every camera frame
    settings          Settings() (settings.py); the defaults are right for any hardware
    store             a CalibrationStore, or calibration_file=path: where what is learned is kept
    objective         name of the objective in use, if it can change (each one has its own peak width)
    key               the calibration key to use (default: built from the drive, the camera and the objective)
    log               print() or a GUI's log function
    stop_event        threading.Event: when set, the autofocus stops within a fraction of a second
    expect            optional: where the focus is expected, relative to the current position (searched first)
    direction         optional: +1 / -1, the side the focus is expected on, if `expect` is not known
    use_last_focus    use where the previous autofocus ended as a hint (False after changing the objective)
    Returns a FocusResult (truthy on success): .success .message .position .score .quality .notes ...
    """
    S = settings or Settings()
    if store is None:
        store = CalibrationStore(calibration_file)
    if key is None:
        key = setup_key(drive, frames, objective)
    log = log or (lambda m: None)
    problems = check_drive(drive) + check_frames(frames)
    if problems:
        msg = "the adapter is not set up correctly: " + "; ".join(problems)
        log(f"[AF] {msg}")
        return FocusResult(False, msg, units=str(getattr(drive, "units", "")))
    try:
        run = _Run(drive, frames, S, store, key, log, stop_event, env or Env(), expect=expect, direction=direction,
                   use_last_focus=use_last_focus)
    except DriveError as e:
        msg = f"Stopped: the focus drive reported a problem: {e}."
        log(f"[AF] {msg}")
        return FocusResult(False, msg, units=str(getattr(drive, "units", "")))
    except Exception as e:
        log("[AF] ERROR: " + traceback.format_exc().replace("\n", "\n[AF]   "))
        return FocusResult(False, f"Error: {e}", units=str(getattr(drive, "units", "")))
    return run.run()


# ==============================================================================================================
#  ADAPTERS/CANON_CAMERA.PY
# ==============================================================================================================
import hashlib
import time
import urllib.request


BASE_URL = "http://127.0.0.1:5556"
SCORE_URL = BASE_URL + "/score"
SCORES_URL = BASE_URL + "/scores"
GRAY_URL = BASE_URL + "/gray_frame"

METRIC_WIDTH = 320          # the picture is shrunk to this width before the sharpness is computed


def _get(url, timeout=1.5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def read_score(url=SCORE_URL):
    """CanonControl's own sharpness score (or None). Kept for programs that call _read_score()."""
    try:
        val = _get(url, timeout=2.0).decode(errors="ignore").strip()
        return None if val in ("none", "", "unknown") else float(val)
    except Exception:
        return None


def jpeg_sharpness(data, width=METRIC_WIDTH):
    """Variance of the Laplacian of a JPEG picture shrunk to `width` pixels (the CanonControl score, computed on
    every frame). Returns None if the picture cannot be decoded."""
    import numpy as np
    import cv2
    arr = np.frombuffer(data, dtype=np.uint8)
    flag = getattr(cv2, "IMREAD_REDUCED_GRAYSCALE_2", cv2.IMREAD_GRAYSCALE)
    g = cv2.imdecode(arr, flag)
    if g is None:
        g = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if g is None:
            return None
    h, w = g.shape[:2]
    if w > width:
        g = cv2.resize(g, (width, max(1, int(round(h * width / w)))), interpolation=cv2.INTER_AREA)
    return float(cv2.Laplacian(g, cv2.CV_64F).var()) + 1e-6


class CanonScores(FrameSource):
    """Sharpness of every live-view frame from CanonControl.py."""

    stall_tip = ("Is CanonControl running with live view on? If the camera falls asleep, turn off 'Auto power off' "
                 "in its menu.")

    def __init__(self, base_url=BASE_URL, mode="auto", log=None, clock=time.time):
        self.base = base_url.rstrip("/")
        self.mode = None if mode == "auto" else mode
        self.log = log or (lambda m: None)
        self.clock = clock
        self.since = clock() - 1.0
        self._last_hash = None
        self._last_score = None
        self._fails = 0
        self._said = False
        self.min_poll_interval = 0.012
        if self.mode is None:
            try:
                self._detect()
            except FrameSourceError:
                self.mode = None               # not reachable yet: try again when frames are asked for

    # ------------------------------------------------------------------ which endpoint
    def _detect(self):
        try:
            txt = _get(f"{self.base}/scores?since={self.clock() - 0.5:.4f}", timeout=1.0).decode(errors="ignore")
            if txt.strip() != "unknown":
                self._set_mode("scores")
                return
        except Exception:
            pass                                  # the unpatched CanonControl drops the connection on /scores
        try:
            data = _get(f"{self.base}/gray_frame", timeout=1.5)
            if data[:2] == b"\xff\xd8":
                try:
                    import cv2, numpy  # noqa: F401
                    self._set_mode("gray")
                    return
                except Exception:
                    pass
        except Exception:
            pass
        try:
            _get(f"{self.base}/score", timeout=1.0)
            self._set_mode("score")
            return
        except Exception:
            pass
        raise FrameSourceError(f"CanonControl does not answer at {self.base} - is it running?")

    def _set_mode(self, mode):
        self.mode = mode
        self.min_poll_interval = {"scores": 0.012, "gray": 0.03, "score": 0.05}[mode]
        if mode == "scores":
            self.log("[AF] camera: CanonControl /scores (every frame, with its time)")
        elif mode == "gray":
            self.log("[AF] camera: this CanonControl has no /scores (older version) - computing the sharpness of "
                     "every live-view picture from /gray_frame instead (fine; the patched CanonControl is a little "
                     "more precise)")
        else:
            self.log("[AF] camera: only /score is available - it changes about twice a second, so the autofocus "
                     "will be slow. Use the patched CanonControl.py (or one that serves /gray_frame).")

    def setup_id(self):
        return "canon-" + (self.mode or "scores")

    def describe(self):
        return f"Canon via CanonControl ({self.mode or 'not connected'})"

    # ------------------------------------------------------------------ frames
    def frames(self):
        if self.mode is None:
            self._detect()
        try:
            if self.mode == "scores":
                rows = self._scores()
            elif self.mode == "gray":
                rows = self._gray()
            else:
                rows = self._score()
            self._fails = 0
            return rows
        except FrameSourceError:
            raise
        except Exception as e:
            self._fails += 1
            if self._fails >= 25:                 # the server changed (restarted as another version?)
                self._fails = 0
                old = self.mode
                try:
                    self._detect()
                    if self.mode != old:
                        return []
                except FrameSourceError:
                    pass
            raise FrameSourceError(f"CanonControl: {e}")

    def _scores(self):
        txt = _get(f"{self.base}/scores?since={self.since:.4f}", timeout=1.5).decode(errors="ignore")
        if txt.strip() == "unknown":
            raise FrameSourceError("this CanonControl has no /scores")
        rows = []
        for line in txt.splitlines():
            p = line.split()
            if len(p) == 3:
                try:
                    t, s = float(p[0]), float(p[2])
                except ValueError:
                    continue
                if t > self.since:
                    self.since = t
                    rows.append((t, s))
        return rows

    def _gray(self):
        data = _get(f"{self.base}/gray_frame", timeout=1.5)
        t = self.clock()
        if not data or data[:2] != b"\xff\xd8":
            return []
        h = hashlib.blake2b(data, digest_size=12).digest()
        if h == self._last_hash:
            return []
        self._last_hash = h
        s = jpeg_sharpness(data)
        if s is None:
            return []
        return [(t, s)]

    def _score(self):
        val = _get(f"{self.base}/score", timeout=1.5).decode(errors="ignore").strip()
        if val in ("none", "", "unknown"):
            return []
        s = float(val)
        if s == self._last_score:
            return []
        self._last_score = s
        return [(self.clock(), s)]


# ==============================================================================================================
#  ADAPTERS/CANON_CLAW.PY
# ==============================================================================================================
import threading
import time


try:
    import serial
except ImportError:                 # lets everything be tested without pyserial
    serial = None

# ---------------------------------------------------------------- settings of this rig
ARDUINO_PORT = "COM4"               # Windows: Device Manager -> Ports (COM & LPT)
CLAW_MAX_TRAVEL = 1200              # steps one autofocus may turn the knob each way (set it to what the knob can do)
CLAW_WIDTH_HINT = 22.0              # usual width of the focus peak in steps (measured on the first run anyway)
CLAW_PLAY_GUESS = 60                # knob play assumed until it has been measured (steps)
CLAW_CONTRAST_HINT = 2.3            # how many times its surroundings the real focus usually stands (about 3x on the
                                    # lab's samples; only a starting point, the usual value is learned)

AF_STEP_TIME = 0.01                 # kept for signature compatibility with move_up/move_down
AF_STEP_SPEED = 1                   # speed byte while measuring (1 = 166 steps/s)
AF_FAST_SPEED = 1                   # speed byte for moves where nothing is measured (2 = 333 steps/s is faster;
                                    # go back to 1 if the claw ever skips steps)
AF_STEP_COUNT = 4                   # default steps when move_up/move_down are called without steps
AF_STEP_DELAY_US = 3000             # step pulse delay (us) at speed 1; the firmware floor is 300 us
AF_MAX_CHUNK = 150                  # longest single :MOTOR:MOVE (keeps the Stop button responsive)
AF_SETTLE = 3.0                     # old name, kept so nothing that imported it breaks


def _delay_us(speed):
    return max(300, AF_STEP_DELAY_US // max(1, int(speed)))


def steps_per_second(speed):
    return 1e6 / (2.0 * _delay_us(speed))


def _read_score():
    """CanonControl's sharpness score, or None (the GUI uses this for its 'Read Score' button)."""
    return read_score()


class ZStageController:
    """
    Drives the M5Stack Atom stepper firmware that turns the claw. Same interface as before:
    move_up(speed, duration, steps) / move_down(...) / stop() / close().
    The step delay is only re-sent when the speed changes (sweeps send many tiny moves).
    """

    def __init__(self, port: str = ARDUINO_PORT, baudrate: int = 115200, timeout: float = 2.0):
        if serial is None:
            raise RuntimeError("pyserial is not installed (in Command Prompt: pip install pyserial)")
        self.ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout, write_timeout=0.5)
        self._base_timeout = timeout
        time.sleep(2)                  # the board resets when the serial port is opened
        self.lock = threading.Lock()
        self._delay_set = None
        self._af_abs_pos = 0
        self.bad_replies = 0
        self.port = port
        self._sync()
        print(f"[ZStage] Connected on {port}")

    def _sync(self):
        """Throw away the board's start-up message, so every reply belongs to the command just sent."""
        try:
            self.ser.reset_input_buffer()
            self.ser.write(b"*IDN?\n")
            self.ser.timeout = 0.4
            t_end = time.time() + 2.0
            while time.time() < t_end:
                if not self.ser.readline().strip():
                    break
            self.ser.timeout = self._base_timeout
        except Exception:
            pass

    def _send(self, cmd: str, expect_seconds: float = 0.0) -> str:
        with self.lock:
            try:
                self.ser.reset_input_buffer()          # a late reply to an earlier command must not be taken for this one's
            except Exception:
                pass
            self.ser.timeout = max(self._base_timeout, expect_seconds + 1.0)
            self.ser.write((cmd + "\n").encode("utf-8"))
            reply = self.ser.readline().decode("utf-8", errors="ignore").strip()
            self.ser.timeout = self._base_timeout
        if reply != "OK":
            self.bad_replies += 1                      # only a move that answers OK proves the motor is alive
            print(f"[ZStage] WARNING: '{cmd}' got reply '{reply or '(no reply)'}'")
        elif cmd.startswith(":MOTOR:MOVE"):
            self.bad_replies = 0
        return reply

    def _move(self, steps: int, speed: int):
        delay_us = _delay_us(speed)
        if delay_us != self._delay_set:
            ok = self._send(f":MOTOR:DELAY {delay_us}") == "OK"
            self._delay_set = delay_us if ok else None
        remaining = int(steps)
        while remaining != 0:
            chunk = max(-AF_MAX_CHUNK, min(AF_MAX_CHUNK, remaining))
            if self._send(f":MOTOR:MOVE {chunk}", expect_seconds=abs(chunk) * 2 * delay_us / 1e6) != "OK":
                self._delay_set = None
            remaining -= chunk

    def move_up(self, speed: int, duration: float, steps: int = None):
        self._move(steps if steps is not None else AF_STEP_COUNT, speed)

    def move_down(self, speed: int, duration: float, steps: int = None):
        self._move(-(steps if steps is not None else AF_STEP_COUNT), speed)

    def stop(self):
        self._send(":MOTOR:EN 0")

    def close(self):
        try:
            self._send(":MOTOR:EN 0")
        finally:
            self.ser.close()
        print("[ZStage] Connection closed.")


class ClawDrive(FocusDrive):
    """The claw as a FocusDrive. Units: motor steps (after the belt, as the firmware counts them)."""

    units = "steps"
    resolution = 1.0
    ack_latency = 0.004
    settle_time = 0.0

    def __init__(self, z, max_travel=CLAW_MAX_TRAVEL, knob_play=None, width_hint=CLAW_WIDTH_HINT,
                 play_guess=CLAW_PLAY_GUESS, step_speed=None, fast_speed=None, contrast_hint=CLAW_CONTRAST_HINT,
                 clock=time.time):
        self.z = z
        self.contrast_hint = contrast_hint
        self.max_travel = float(max_travel)
        self.backlash = None if knob_play is None else float(knob_play)
        self.backlash_guess = float(play_guess)
        self.width_hint = width_hint
        self.step_speed = int(step_speed or AF_STEP_SPEED)
        self.fast_speed = int(fast_speed or AF_FAST_SPEED)
        self.rate = steps_per_second(self.step_speed)
        self.fast_rate = steps_per_second(self.fast_speed)
        self.clock = clock
        try:
            z._delay_set = None        # send the step delay once at the start of every run (safe after a reset)
            z.bad_replies = 0
        except Exception:
            pass

    def move_by(self, delta, fast=False):
        n = int(round(delta))
        if n == 0:
            return None
        speed = self.fast_speed if fast else self.step_speed
        t_send = self.clock()
        try:
            if n > 0:
                self.z.move_up(speed, AF_STEP_TIME, steps=n)
            else:
                self.z.move_down(speed, AF_STEP_TIME, steps=-n)
        except DriveError:
            raise
        except Exception as e:
            raise DriveError(f"the motor controller connection failed ({e}): check the claw's USB cable") from e
        t_end = self.clock()
        if getattr(self.z, "bad_replies", 0) >= 3:
            raise DriveError("the motor controller is not answering (3 commands in a row): check the claw's USB "
                             "cable and that the COM port is right (close stepper_gui.py if it is open)")
        T = abs(n) * 2 * _delay_us(speed) / 1e6
        t1 = max(t_send, t_end - self.ack_latency)
        return max(t_send, t1 - T), t1

    def stop(self):
        try:
            self.z.stop()
        except Exception:
            pass

    def setup_id(self):
        return "claw"

    def describe(self):
        return f"claw on {getattr(self.z, 'port', 'the focus knob')} (steps)"

    @property
    def memory(self):
        d = getattr(self.z, "__dict__", None)
        if d is None:
            return super().memory
        return d.setdefault("_uaf_memory", {})


# ==============================================================================================================
#  DROP-IN: the names autofocus_gui.py and auto_scan.py import from autofocus.py, and the launcher
# ==============================================================================================================
import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))
MAX_TRAVEL = CLAW_MAX_TRAVEL          # steps one autofocus may turn the knob each way
KNOB_PLAY = None                      # knob play in steps if you KNOW it; None = measured automatically
OBJECTIVE = None                      # name of the objective, if you change objectives (each learns its own width)
CALIB_FILE = _os.path.join(_HERE, "universal_autofocus_calibration.json")
V5_CALIB_FILE = _os.path.join(_HERE, "autofocus_calibration.json")
SETTINGS = Settings()
AutofocusResult = FocusResult
_uaf_autofocus = autofocus


def autofocus(z, frames=None, log=print, stop_event=None, objective=None, **kw):     # noqa: F811
    """
    The universal function, and the drop-in for the claw rig, in one:

      autofocus(drive, frames, ...)   ANY microscope: drive has move_by() + max_travel, frames has frames()
                                      (see INTERFACES). Extra keyword arguments go to the universal function:
                                      settings, store, calibration_file, objective, stop_event, log, direction ...
      autofocus(z, log, stop_event)   the claw rig, exactly as the GUI calls it (z = the ZStageController)
    """
    if frames is not None and not callable(frames) or isinstance(z, FocusDrive):
        if "calibration_file" not in kw and "store" not in kw:
            kw["calibration_file"] = CALIB_FILE
        return _uaf_autofocus(z, frames, log=log, stop_event=stop_event, objective=objective, **kw)
    if callable(frames):                    # old positional call autofocus(z, log)
        log = frames
    drive = ClawDrive(z, max_travel=MAX_TRAVEL, knob_play=KNOB_PLAY)
    cam = CanonScores(log=log)
    store = CalibrationStore(CALIB_FILE)
    key = f"{drive.setup_id()}|{cam.setup_id()}/{objective or OBJECTIVE or 'default'}"
    try:
        if store.import_v5(V5_CALIB_FILE, key):
            log("[AF] took over the camera delay and peak width learned by autofocus v5")
    except Exception:
        pass
    result = _uaf_autofocus(drive, cam, settings=SETTINGS, store=store, key=key, log=log, stop_event=stop_event)
    if result.success:
        log(f"[AF] quality: {result.quality}" + (f"  ({'; '.join(result.notes)})" if result.notes else ""))
    return result


def _check(port=ARDUINO_PORT):
    """Is CanonControl answering, and does the claw move the focus and come back?"""
    import statistics
    print("camera (CanonControl at 127.0.0.1:5556):")
    cam = CanonScores(log=print)

    def level(sec):
        t0, ys = time.time(), []
        while time.time() - t0 < sec:
            try:
                ys += [math.log(max(r[1], 1e-9)) for r in cam.frames()]
            except Exception:
                pass
            time.sleep(0.01)
        return ys

    level(0.3)                                         # (skip the history CanonControl sends first)
    ys = level(2.5)
    if len(ys) < 3:
        print("  PROBLEM: no frames - start CanonControl.py in another window, live view on")
        return
    print(f"  {len(ys) / 2.5:.1f} frames/s, score {math.exp(statistics.median(ys)):.4g}  OK")
    z = ZStageController(port)
    try:
        d = ClawDrive(z)
        y0 = statistics.median(level(1.0))
        for _ in range(20):
            d.move_by(3)
        y1 = statistics.median(level(1.0))
        d.move_by(-60)
        y2 = statistics.median(level(1.0))
        print(f"claw: score at the start {math.exp(y0):.4g}, 60 steps up {math.exp(y1):.4g}, back {math.exp(y2):.4g}")
        print("  PROBLEM: the picture did not change - is the claw switched on and gripping the knob?"
              if abs(y1 - y0) < 0.03 else "  OK: the picture follows the claw")
    finally:
        z.close()


def _demo(runs=4, seed=3):
    import random
    rng = random.Random(seed)

    class Clock:                                   # simulated time, so the demo takes seconds, not minutes
        t = 1000.0

        def now(self):
            return self.t

        def sleep(self, s):
            self.t += max(0.0, s)

    clk = Clock()
    state = dict(pos=0.0, focus=37.0, moves=[])    # true focus 37 um above the start, peak width ~4 um

    class SimDrive(FocusDrive):
        units = "um"
        max_travel = 100.0
        resolution = 0.05

        def move_by(self, delta, fast=False):
            t0 = clk.now()
            clk.sleep(abs(delta) / 200.0 + 0.005)  # 200 um/s
            state["moves"].append((t0, clk.now(), state["pos"], state["pos"] + delta))
            state["pos"] += delta
            return t0, clk.now()

        def setup_id(self):
            return "demo-stage"

    def pos_at(t):
        p = 0.0
        for t0, t1, a, b in state["moves"]:
            if t >= t1:
                p = b
            elif t > t0:
                return a + (b - a) * (t - t0) / (t1 - t0)
        return p if state["moves"] else state["pos"]

    class SimFrames(FrameSource):
        last = [1000.0]

        def frames(self):
            out = []
            while self.last[0] + 1 / 30.0 <= clk.now():          # 30 frames/s, 50 ms camera delay
                self.last[0] += 1 / 30.0
                x = pos_at(self.last[0] - 0.05) - state["focus"]
                s = 100.0 * (1.0 + 2.5 * math.exp(-0.5 * (x / 1.7) ** 2)) * math.exp(rng.gauss(0, 0.005))
                out.append((self.last[0], s))
            return out

        def setup_id(self):
            return "demo-camera"

    store = CalibrationStore(None)                 # in memory: the demo learns, then forgets
    env = Env(clk.now, clk.sleep)
    for k in range(runs):
        if k:
            state["focus"] = state["pos"] + rng.choice((-1, 1)) * rng.uniform(5, 30)
        r = _uaf_autofocus(SimDrive(), SimFrames(), store=store, env=env, log=lambda m: None)
        err = state["pos"] - state["focus"]
        print(f"run {k + 1}: {'IN FOCUS' if abs(err) < 1.0 else 'NOT in focus'}  ended {err:+.2f} um from the "
              f"true focus, quality {r.quality}, {r.seconds:.1f} s (simulated)")
    print("learned:", store.describe())



def _launch(target, args):
    import importlib.util
    import runpy
    spec = importlib.util.spec_from_file_location("autofocus", _os.path.abspath(__file__))
    mod = importlib.util.module_from_spec(spec)
    _sys.modules["autofocus"] = mod                    # every "import autofocus" now gets this file
    spec.loader.exec_module(mod)
    path = target if _os.path.isabs(target) else _os.path.join(_HERE, target)
    if not _os.path.exists(path):
        _sys.exit(f"cannot find {path} - put this file in the same folder as {target}")
    print(f"[universal autofocus] starting {_os.path.basename(path)} with the universal autofocus (one file)")
    _sys.argv = [path] + list(args)
    _os.chdir(_os.path.dirname(path))
    if _HERE not in _sys.path:
        _sys.path.insert(0, _HERE)
    runpy.run_path(path, run_name="__main__")


if __name__ == "__main__":
    a = _sys.argv[1:]
    if a and a[0] == "check":
        _check(a[1] if len(a) > 1 else ARDUINO_PORT)
    elif a and a[0] == "demo":
        _demo()
    elif a and a[0] == "show":
        print(CalibrationStore(CALIB_FILE).describe())
    else:
        _launch(a[0] if a else "autofocus_gui.py", a[1:])

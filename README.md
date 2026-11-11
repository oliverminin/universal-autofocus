# Universal autofocus

`universal_autofocus_v1.py` is one autofocus function for any microscope, in one file.

It only needs two things from the hardware: a way to move the focus, and a sharpness score for each camera
picture. The peak width, play of the drive and camera delay are measured on the first run and remembered in
`universal_autofocus_calibration.json`, so nothing is tuned per microscope.

## Any microscope

```python
from universal_autofocus_v1 import autofocus, FocusDrive, FrameSource

class MyDrive(FocusDrive):
    units = "um"
    max_travel = 50.0                       # how far one autofocus may move each way
    def move_by(self, delta, fast=False):
        ...                                 # move the focus by delta, return when done

class MyFrames(FrameSource):
    def frames(self):
        return [...]                        # [(time.time() of each new picture, sharpness), ...]

result = autofocus(MyDrive(), MyFrames())
print(result.message, result.quality)
```

## Claw rig (claw on COM4 + Canon via CanonControl.py)

Put the file next to `autofocus_gui.py` and change the GUI's import line to

```python
from universal_autofocus_v1 import autofocus, ZStageController, ARDUINO_PORT, _read_score
```

## Commands

```
python universal_autofocus_v1.py demo     simulated microscope, no hardware
python universal_autofocus_v1.py check    camera + claw check (CanonControl must be running)
python universal_autofocus_v1.py show     what it has learned about this rig
```

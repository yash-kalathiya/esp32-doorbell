"""Image-based low-light estimate; it is not an ambient lux measurement."""
from io import BytesIO

from PIL import Image, ImageStat


def frame_brightness(jpeg):
    with Image.open(BytesIO(jpeg)) as image:
        image.thumbnail((64, 64))
        return float(ImageStat.Stat(image.convert("L")).mean[0])


class AutomaticLight:
    # Separate thresholds, consecutive samples and minimum hold reduce toggling
    # when the camera's automatic exposure or its own lamp changes the image.
    DARK = 55
    BRIGHT = 115
    HOLD_SECONDS = 30

    def __init__(self):
        self.enabled = False
        self.brightness = None
        self.changed_at = float("-inf")
        self.samples = 0

    def update(self, brightness, sampled_at):
        self.brightness = brightness
        candidate = brightness >= self.BRIGHT if self.enabled else brightness <= self.DARK
        self.samples = self.samples + 1 if candidate else 0
        if self.samples >= 3 and sampled_at - self.changed_at >= self.HOLD_SECONDS:
            self.enabled = not self.enabled
            self.changed_at = sampled_at
            self.samples = 0

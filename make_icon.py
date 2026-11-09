"""Draw claude_widget.ico - a context ring, matching the widget's palette.

Run once after changing the design:

    python make_icon.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
BG = "#1c1c1c"
TRACK = "#3a3a3a"
FILL = "#4ec9a0"
RING_FRACTION = 0.68  # how much of the ring is filled, purely cosmetic

OUTPUT = Path(__file__).with_name("claude_widget.ico")


def main() -> None:
    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    draw.rounded_rectangle((0, 0, SIZE - 1, SIZE - 1), radius=56, fill=BG)

    margin, width = 52, 30
    box = (margin, margin, SIZE - 1 - margin, SIZE - 1 - margin)
    draw.arc(box, 0, 360, fill=TRACK, width=width)
    draw.arc(box, -90, -90 + int(360 * RING_FRACTION), fill=FILL, width=width)

    image.save(OUTPUT, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                              (128, 128), (256, 256)])
    print(f"wrote {OUTPUT}")


if __name__ == "__main__":
    main()

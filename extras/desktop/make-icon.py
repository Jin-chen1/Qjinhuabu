from pathlib import Path
from PIL import Image

root = Path(__file__).resolve().parent
target = root / "QJin.iconset"
target.mkdir(exist_ok=True)
image = Image.open(root / "app-icon.png").convert("RGBA")
for size in (16, 32, 128, 256, 512):
    image.resize((size, size), Image.Resampling.LANCZOS).save(target / f"icon_{size}x{size}.png")
    image.resize((size * 2, size * 2), Image.Resampling.LANCZOS).save(target / f"icon_{size}x{size}@2x.png")

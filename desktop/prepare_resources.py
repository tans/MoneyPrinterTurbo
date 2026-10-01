"""Stage explicitly licensed resources for distributable desktop builds."""

import hashlib
from pathlib import Path
import urllib.request

REVISION = "523d033d6cb47f4a80c58a35753646f5c3608a78"
BASE = f"https://raw.githubusercontent.com/notofonts/noto-cjk/{REVISION}/Sans"
FONT_SHA256 = "2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b"


def main():
    root = Path(__file__).resolve().parents[1] / "build/desktop-resources"
    fonts = root / "fonts"
    fonts.mkdir(parents=True, exist_ok=True)
    font = fonts / "NotoSansCJKsc-Regular.otf"
    if (
        not font.exists()
        or hashlib.sha256(font.read_bytes()).hexdigest() != FONT_SHA256
    ):
        with urllib.request.urlopen(
            BASE + "/OTF/SimplifiedChinese/" + font.name, timeout=120
        ) as response:
            content = response.read()
        if hashlib.sha256(content).hexdigest() != FONT_SHA256:
            raise RuntimeError("Noto font checksum mismatch")
        font.write_bytes(content)
    with urllib.request.urlopen(BASE + "/../LICENSE", timeout=30) as response:
        (root / "Noto-OFL.txt").write_bytes(response.read())
    print(f"Staged verified Noto CJK font and OFL license in {root}")


if __name__ == "__main__":
    main()

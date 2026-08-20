# BeauHoroscope production entrypoint
# Full implementation is supplied in the attached v9 artifact.
# IMPORTANT: this repository entrypoint is intentionally a small launcher.

from pathlib import Path
import runpy

BASE = Path(__file__).resolve().parent
CANDIDATES = [
    BASE / "horoscope_free_pipeline_v9_content_engine.py",
    BASE / "horoscope_free_pipeline_v8_autopost_jca.py",
    BASE / "horoscope_free_pipeline_v7_adminpublish.py",
]

for candidate in CANDIDATES:
    if candidate.exists():
        runpy.run_path(str(candidate), run_name="__main__")
        break
else:
    raise FileNotFoundError("No BeauHoroscope pipeline source file found")

"""Cache one tight, complete name/price/badge crop for the next Lidl run.

Example: .venv/bin/python scripts/read_lidl_crop.py page.png --page 42
           --box 60 3890 1240 4330
No truth files are consulted. Review crop geometry before running.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from babaemail import lidl_crops, vlm


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('image', type=Path)
    ap.add_argument('--page', type=int, required=True)
    ap.add_argument('--box', type=int, nargs=4, required=True)
    ap.add_argument('--cache-dir', type=Path, default=lidl_crops.CACHE_DIR)
    args = ap.parse_args()
    with vlm.ollama_session():
        result = lidl_crops.read_crop(args.image.read_bytes(), args.page, tuple(args.box))
    path = lidl_crops.save_crop(result, args.cache_dir)
    print(path)
    print('independent name/price proof:', bool(lidl_crops._proof(result)))


if __name__ == '__main__':
    main()

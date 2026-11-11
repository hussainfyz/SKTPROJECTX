import os
import sys
import argparse
import subprocess
from pathlib import Path

def discover_pending_ocr(base_dir: Path):
    tasks = []
    for item in sorted(base_dir.iterdir()):
        if item.is_dir():
            # Check if this directory already has a searchable PDF
            searchable_dir = item / "searchable"
            searchable_pdfs = list(searchable_dir.glob("*.pdf")) if searchable_dir.is_dir() else []
            if searchable_pdfs:
                print(f"[SKIP] Searchable PDF already exists in: {item.name}")
                continue

            # Check if raw PDF exists inside this folder or matching root PDF
            inside_pdfs = [f for f in item.glob("*.pdf") if not f.name.endswith("_searchable.pdf") and not f.name.endswith("_ocr.pdf")]
            if inside_pdfs:
                tasks.append((inside_pdfs[0], item))
                continue

        elif item.is_file() and item.suffix.lower() == ".pdf":
            if item.name.endswith("_searchable.pdf") or item.name.endswith("_ocr.pdf"):
                continue
            matching_folder = base_dir / item.stem
            searchable_dir = matching_folder / "searchable"
            if searchable_dir.is_dir() and list(searchable_dir.glob("*.pdf")):
                print(f"[SKIP] Output folder and searchable PDF already exist for: {item.name}")
                continue
            tasks.append((item, matching_folder))

    return tasks

def main():
    parser = argparse.ArgumentParser(description="Master Runner for Step 1 (OCR)")
    parser.add_argument("--basedir", required=True, help="Base directory containing booth folders/PDFs")
    parser.add_argument("-l", "--lang", default="kan+eng", help="OCR language(s), default: kan+eng")
    parser.add_argument("--force", action="store_true", help="Force OCR even if searchable")
    args = parser.parse_args()

    base_dir = Path(args.basedir.strip('"')).resolve()
    if not base_dir.is_dir():
        parser.error(f"Base directory does not exist: {base_dir}")

    pipeline_dir = Path(__file__).parent.resolve()
    step_script = pipeline_dir / "step1_ocr.py"

    tasks = discover_pending_ocr(base_dir)
    print("=" * 70)
    print(f" MASTER STEP 1 (OCR): Discovered {len(tasks)} pending file(s)")
    print("=" * 70)

    for idx, (pdf_path, out_folder) in enumerate(tasks, start=1):
        print(f"\n[{idx}/{len(tasks)}] Running Step 1 on: {pdf_path.name}")
        searchable_dir = out_folder / "searchable"
        searchable_dir.mkdir(parents=True, exist_ok=True)
        out_pdf = searchable_dir / f"{out_folder.name}_searchable.pdf"

        cmd = [sys.executable, str(step_script), str(pdf_path), "-o", str(out_pdf), "-l", args.lang]
        if args.force:
            cmd.append("--force")

        res = subprocess.run(cmd)
        if res.returncode != 0:
            print(f"[ERROR] Step 1 failed on: {pdf_path.name}")
        else:
            print(f"[SUCCESS] Searchable PDF created: {out_pdf.name}")

if __name__ == "__main__":
    main()

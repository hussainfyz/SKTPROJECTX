import os
import sys
import shutil
import argparse
from pathlib import Path
from pdfminer.high_level import extract_text
from pdfminer.pdfpage import PDFPage
import ocrmypdf

def is_pdf_searchable(pdf_path: str, min_char_threshold: int = 100) -> bool:
    try:
        with open(pdf_path, "rb") as f:
            total_pages = sum(1 for _ in PDFPage.get_pages(f))
        sample_pages = list(range(min(3, total_pages)))
        extracted = extract_text(pdf_path, page_numbers=sample_pages)
        cleaned_text = extracted.replace("\n", "").replace(" ", "").strip()
        return len(cleaned_text) >= min_char_threshold
    except Exception as e:
        print(f"[!] Warning checking PDF searchability: {e}")
        return False

def run_ocr(input_pdf: str, output_pdf: str = None, lang: str = "kan+eng", force_ocr: bool = False):
    clean_input = input_pdf.strip().strip('"').strip("'")
    input_path = Path(clean_input).resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    stem = input_path.stem.replace("_searchable", "")
    base_folder = input_path.parent / stem

    if not output_pdf:
        searchable_dir = base_folder / "searchable"
        searchable_dir.mkdir(parents=True, exist_ok=True)
        output_path = searchable_dir / f"{stem}_searchable.pdf"
    else:
        clean_output = output_pdf.strip().strip('"').strip("'")
        output_path = Path(clean_output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

    # CHECK 1: Already processed output file exists and is valid
    if output_path.exists() and output_path.stat().st_size > 1024 and not force_ocr:
        print(f"[SKIP] Step 1: Searchable PDF already exists at '{output_path}'.")
        return str(output_path)

    # CHECK 2: Input is already searchable
    if not force_ocr and is_pdf_searchable(str(input_path)):
        print(f"[+] '{input_path.name}' is already searchable.")
        if not output_path.exists():
            shutil.copy2(str(input_path), str(output_path))
            print(f"[+] Copied to '{output_path}'.")
        return str(output_path)

    print(f"[*] Running OCR ({lang}) on '{input_path.name}' -> '{output_path}'...")
    ocrmypdf.ocr(
        str(input_path),
        str(output_path),
        language=lang,
        redo_ocr=True,
        tesseract_pagesegmode=11,
        output_type="pdf",
        progress_bar=True,
    )
    print(f"[+] Searchable PDF saved: {output_path}")
    return str(output_path)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Step 1: Convert Scanned PDF to Searchable PDF")
    parser.add_argument("input", help="Path to scanned PDF")
    parser.add_argument("-o", "--output", default=None, help="Output searchable PDF path")
    parser.add_argument("-l", "--lang", default="kan+eng", help="OCR language(s), default: kan+eng")
    parser.add_argument("--force", action="store_true", help="Force OCR even if already searchable")
    args = parser.parse_args()

    run_ocr(args.input, args.output, lang=args.lang, force_ocr=args.force)

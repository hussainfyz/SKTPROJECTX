import argparse
import sys
import time
from pathlib import Path
from extract_metadata import process_pdf


def find_searchable_pdfs(basedir: Path):
    """
    Finds PDFs matching `searchable/*_searchable.pdf` or `searchable/*.pdf` 
    under each immediate subfolder in basedir.
    Returns tuples of (pdf_path, target_metadata_json_path).
    """
    targets = []
    
    # Iterate through each folder inside basedir (e.g. 149_part, someidfolder)
    #for part_dir in sorted(basedir.iterdir()):
   
    pdf_candidates = list(basedir.glob("*.pdf"))
    pdf_candidates.sort(reverse=True)
    print(pdf_candidates[-1])
    #exit()
    if not pdf_candidates:
        print(f"[!] Warning: No PDF found in {basedir}")
        exit()

    # Take the first matched PDF for this ID folder
    #pdf_file = pdf_candidates[0]
    for onefile in pdf_candidates:
        filename=str(onefile).split('/')[-1]
        print(filename)
        output_json = basedir/"output" / f"{filename}_metadata.json"
    
        targets.append((onefile, output_json))

    
    return targets


def main():
    parser = argparse.ArgumentParser(description="Scan base directory and extract PDF metadata using Gemini API.")
    parser.add_argument("--basedir", required=True, help="Base folder containing ID/part subfolders.")
    parser.add_argument("--force", action="store_true", help="Re-process even if metadata.json already exists.")
    parser.add_argument("--delay", type=float, default=4.5, help="Sleep delay in seconds between API calls (default: 4.5s to stay under 15 RPM).")
    args = parser.parse_args()

    base_path = Path(args.basedir).resolve()
    if not base_path.exists():
        print(f"Error: Base directory '{base_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    targets = find_searchable_pdfs(base_path)
    total_found = len(targets)
    print(f"[*] Found {total_found} potential folders to process in: {base_path}")

    processed_count = 0
    skipped_count = 0
    failed_count = 0

    for idx, (pdf_path, out_json) in enumerate(targets, start=1):
        print(f"\n[{idx}/{total_found}] Checking: {pdf_path.parent.parent.name}")

        # Skip if metadata.json already exists unless --force is given
        if out_json.exists() and not args.force:
            print(f"    [-] Skipping: '{out_json.name}' already exists.")
            skipped_count += 1
            continue

        print(f"    [>] Processing: {pdf_path.name}")
        try:
            process_pdf(str(pdf_path), str(out_json))
            processed_count += 1
        except Exception as e:
            print(f"    [X] Error processing {pdf_path.name}: {e}", file=sys.stderr)
            failed_count += 1

        # Pause to comply with Free Tier rate limit (15 requests/min => ~1 request every 4 seconds)
        if idx < total_found:
            time.sleep(args.delay)

    print("\n" + "=" * 50)
    print(f"Batch Run Summary:")
    print(f"  Total found: {total_found}")
    print(f"  Processed:   {processed_count}")
    print(f"  Skipped:     {skipped_count}")
    print(f"  Failed:      {failed_count}")
    print("=" * 50)


if __name__ == "__main__":
    main()
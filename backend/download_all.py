
import argparse
import logging
import os
import re
import tempfile
from pathlib import Path

from eci_client import ECIClient, ECIClientError

# ---------------- CONFIGURATION ----------------

STATE_CODE = "S10"
YEAR = "2026"

AC_NUMBER = 54
DISTRICT_CODE = "S1006"

ROLL_TYPE = "SIR-FinalRoll"
PUBLISHED_ROLL_ID = "S10-2026-SIR-FIR-1"
PDF_GEN_TYPE = "EROLLGEN"
REVISION_NO = "1"

LANGUAGE_CODE = "ENG"
MIS_KEY = "EROLLA32DVI09AJH"

OUTPUT_DIR = Path("downloads") / f"AC_{AC_NUMBER}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)


def _part_name_for(number, part_names):
    raw = str(part_names.get(str(number)) or "").strip()
    words = re.findall(r"[A-Za-z0-9]+", raw)
    return "".join(word[0].upper() for word in words) or f"Part_{number}"


def _build_download_filename(original_name, part_number, language_code, part_names):
    stem = Path(original_name).stem
    tokens = stem.split("-")
    revision_index = next(
        (
            i for i, token in enumerate(tokens)
            if re.fullmatch(r"Revision\d+", token, flags=re.IGNORECASE)
        ),
        None,
    )
    if revision_index is None or revision_index + 2 >= len(tokens):
        raise ECIClientError(
            f"Cannot parse revision/language/part fields from ECI filename: {stem}"
        )

    revision_number = re.sub(r"(?i)^Revision", "", tokens[revision_index])
    source_language = tokens[revision_index + 1] or str(language_code)
    source_part_number = tokens[revision_index + 2]
    if source_part_number != str(part_number):
        raise ECIClientError(
            f"ECI filename part number {source_part_number!r} does not match "
            f"requested part {part_number!r}: {stem}"
        )

    trailing_tokens = tokens[revision_index + 3:]
    roll_suffix = "_".join(token for token in trailing_tokens if token).strip("_")
    components = [f"part{part_number}", source_language, revision_number]
    if roll_suffix:
        components.append(roll_suffix)
    components.append(_part_name_for(part_number, part_names))

    filename = "_".join(components) + ".pdf"
    filename = re.sub(r'[<>:"/\\|?*]', "", filename).strip()
    return re.sub(r"_+", "_", filename)


def _keep_first_and_last_pages(pdf_path):
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise ECIClientError(
            "The --metadata option requires pypdf. Install it with: pip install pypdf"
        ) from exc

    pdf_path = Path(pdf_path)
    temporary_path = None
    try:
        reader = PdfReader(str(pdf_path))
        page_count = len(reader.pages)
        if page_count == 0:
            raise ECIClientError(f"Downloaded PDF has no pages: {pdf_path}")
        if page_count <= 2:
            return

        writer = PdfWriter()
        writer.add_page(reader.pages[0])
        writer.add_page(reader.pages[-1])
        if reader.metadata:
            writer.add_metadata({
                key: str(value)
                for key, value in reader.metadata.items()
                if value is not None
            })

        with tempfile.NamedTemporaryFile(
            dir=pdf_path.parent,
            prefix=f"{pdf_path.stem}.",
            suffix=".tmp.pdf",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)

        writer.write(str(temporary_path))
        temporary_path.chmod(pdf_path.stat().st_mode)
        os.replace(temporary_path, pdf_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main(metadata=False):

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    with ECIClient() as client:

        # 1. Fetch CAPTCHA
        print("\nFetching CAPTCHA...")

        captcha_id, captcha_text = client.prompt_captcha(
            str(OUTPUT_DIR / "captcha.jpg")
        )

        # 2. Fetch all parts for the assembly
        part_payload = {
            "stateCd": STATE_CODE,
            "acNumber": str(AC_NUMBER),
            "districtCd": DISTRICT_CODE,
            "rollTypeRefId": ROLL_TYPE,
            "pdfGenType": PDF_GEN_TYPE,
            "revisionNo": REVISION_NO,
            "year": YEAR,
            "misKey": MIS_KEY
        }

        print("\nFetching assembly part list...")

        parts_response = client.get_publish_part_list(part_payload)

        print("\nPart-list response:")
        print(parts_response)

        # 3. Extract part numbers
        # Inspect the response structure and adjust this mapping if needed.
        parts = parts_response.get("payload", [])

        if not parts:
            print("No parts returned. Check the part-list response.")
            return

        part_numbers = []
        part_names = {}

        for part in parts:
            if isinstance(part, dict):
                part_number = (
                    part.get("partNumber")
                    or part.get("partNo")
                )
                if part_number is not None:
                    part_numbers.append(int(part_number))
                    part_names[str(int(part_number))] = (
                        part.get("partName")
                        or part.get("partNameEn")
                        or part.get("partNameKannada")
                        or ""
                    )

        if not part_numbers:
            print("Could not identify part numbers in the response.")
            print("Please inspect the response structure.")
            return

        part_numbers = sorted(set(part_numbers))

        print(f"\nTotal parts found: {len(part_numbers)}")
        print("Part numbers:", part_numbers)

        # 4. Generate and download all PDFs in one request
        payload = {
            "stateCd": STATE_CODE,
            "acNumber": AC_NUMBER,
            "partNumberList": part_numbers,
            "districtCd": AC_NUMBER,
            "captcha": captcha_text,
            "captchaId": captcha_id,
            "langCd": LANGUAGE_CODE,
            "publishedRollId": PUBLISHED_ROLL_ID,
            "misKey": MIS_KEY
        }

        print("\nRequesting PDF generation for all parts...")

        def rename_downloaded_file(file_index, downloaded_path):
            if file_index >= len(part_numbers):
                raise ECIClientError(
                    f"ECI returned more PDFs than requested parts ({len(part_numbers)})."
                )

            source_path = Path(downloaded_path)
            part_number = part_numbers[file_index]
            target_path = source_path.with_name(
                _build_download_filename(
                    source_path.name, part_number, LANGUAGE_CODE, part_names
                )
            )
            if target_path.exists() and target_path.resolve() != source_path.resolve():
                raise ECIClientError(
                    f"Refusing to overwrite existing downloaded file: {target_path.name}"
                )
            if target_path.resolve() != source_path.resolve():
                source_path.rename(target_path)
            if metadata:
                _keep_first_and_last_pages(target_path)
            return str(target_path)

        files = client.download_published_pdfs(
            payload,
            output_dir=str(OUTPUT_DIR),
            on_file_downloaded=rename_downloaded_file,
        )

        print("\n========== DOWNLOAD COMPLETE ==========")
        print(f"Assembly: {AC_NUMBER}")
        print(f"Parts requested: {len(part_numbers)}")
        print(f"PDFs downloaded: {len(files)}")
        if metadata:
            print("Metadata mode: kept only the first and last page of each PDF")

        for file_path in files:
            print(file_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download all published PDFs for the configured assembly."
    )
    parser.add_argument(
        "--metadata",
        type=str.lower,
        choices=("true", "false"),
        default="false",
        help="Keep only the first and last page of each downloaded PDF (true/false).",
    )
    args = parser.parse_args()

    try:
        main(metadata=args.metadata == "true")
    except ECIClientError as exc:
        print(f"\nECI ERROR: {exc}")
    except Exception as exc:
        print(f"\nUNEXPECTED ERROR: {exc}")
        raise

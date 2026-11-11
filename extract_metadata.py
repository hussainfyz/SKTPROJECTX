import io
import json
import os
import sys
import time
from pathlib import Path
from google import genai
from google.genai import types
from google.genai.errors import ServerError, ClientError
from pypdf import PdfReader, PdfWriter

PROMPT = """
You are an expert document parser. Analyze the provided PDF document containing the first and last pages of an electoral roll.
Extract all key header and summary details into clean JSON format.

Required structure:
{
  "first_page": {
    "document_title": string,
    "electoral_roll_year": string,
    "assembly_constituency": {"number": string, "name": string},
    "parliamentary_constituency": {"number": string, "name": string},
    "part_number": string,
    "revision_details": {
      "year_of_revision": string,
      "roll_identification": string,
      "qualifying_date": string,
      "type_of_revision": string,
      "date_of_updation": string
    },
    "polling_area": {
      "sections": [string],
      "main_town_or_village": string,
      "post_office": string,
      "police_station": string,
      "tehsil": string,
      "district": string,
      "pin_code": string
    },
    "polling_station_details": {
      "number_and_name": string,
      "address": string
    },
    "number_of_electors": {
      "starting_serial_no": integer,
      "ending_serial_no": integer,
      "male": integer,
      "female": integer,
      "third_gender": integer,
      "total": integer
    }
  },
  "last_page": {
    "page_number": integer,
    "summary_statistics": {
      "mother_roll": {"male": integer, "female": integer, "third_gender": integer, "total": integer},
      "supplement_1_additions": {"male": integer, "female": integer, "third_gender": integer, "total": integer},
      "supplement_1_deletions": {"male": integer, "female": integer, "third_gender": integer, "total": integer},
      "net_electors_after_revision": {"male": integer, "female": integer, "third_gender": integer, "total": integer}
    },
    "number_of_modifications": {
      "supplement_1": integer
    }
  }
}
Extract only the factual values present in the text and tables.
"""

def extract_first_and_last_pages(pdf_path: str) -> bytes:
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    if total_pages == 0:
        raise ValueError(f"File {pdf_path} has 0 pages.")

    writer = PdfWriter()
    writer.add_page(reader.pages[0])

    if total_pages > 1:
        writer.add_page(reader.pages[-1])

    pdf_buffer = io.BytesIO()
    writer.write(pdf_buffer)
    pdf_buffer.seek(0)
    return pdf_buffer.getvalue()


def process_pdf(pdf_path: str, output_json: str = None):
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("Error: GEMINI_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"[*] Extracting first and last pages from: {pdf_path}")
    two_page_pdf_bytes = extract_first_and_last_pages(pdf_path)

    client = genai.Client(api_key=api_key)

    attempt = 1
    wait_time = 10  # Initial wait duration in seconds
    max_wait = 60   # Maximum delay cap

    while True:
        try:
            print(f"[*] Submitting to Gemini Free Tier (Attempt {attempt})...")
            response = client.models.generate_content(
                model="gemini-3.5-flash-lite",
                contents=[
                    types.Part.from_bytes(
                        data=two_page_pdf_bytes,
                        mime_type="application/pdf",
                    ),
                    PROMPT,
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            
            if response and response.text:
                result_json = json.loads(response.text)
                break
            else:
                raise RuntimeError("Empty response received from Gemini.")

        except (ServerError, ClientError, Exception) as err:
            err_msg = str(err)
            is_transient = (
                "503" in err_msg or 
                "429" in err_msg or 
                "UNAVAILABLE" in err_msg or 
                "RESOURCE_EXHAUSTED" in err_msg
            )
            
            if is_transient:
                print(f"    [!] Server busy / rate limit hit ({err_msg[:80]}...).")
                print(f"    [!] Pausing on this file. Retrying in {wait_time}s (Attempt {attempt})...")
                time.sleep(wait_time)
                # Exponential backoff capped at max_wait
                wait_time = min(int(wait_time * 1.5), max_wait)
                attempt += 1
                continue
            else:
                # Fatal errors (bad file, unrecoverable payload) are raised
                print(f"    [X] Non-retryable error: {err_msg}", file=sys.stderr)
                raise err

    if output_json:
        out_file = Path(output_json)
        out_file.parent.mkdir(parents=True, exist_ok=True)
        with open(out_file, "w", encoding="utf-8") as f:
            json.dump(result_json, f, indent=2, ensure_ascii=False)
        print(f"[+] Saved output to {out_file}")
    else:
        print("\n=== Result JSON ===")
        print(json.dumps(result_json, indent=2, ensure_ascii=False))

    return result_json


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python extract_metadata.py <path_to_pdf> [output_json_path]")
        sys.exit(1)

    input_pdf = sys.argv[1]
    out_json = sys.argv[2] if len(sys.argv) > 2 else None
    process_pdf(input_pdf, out_json)
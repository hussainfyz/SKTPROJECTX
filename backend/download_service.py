import hashlib, json, re, shutil
from datetime import datetime, timezone
from pathlib import Path
from .utils import safe_component
from .repository import DownloadRepository
from .eci_client import ECIClientError

class DownloadService:
    """Download orchestration; independent of PyQt and suitable for other clients."""
    def __init__(self, client, config, repository=None):
        self.client = client
        self.config = config
        self.repository = repository or DownloadRepository(config.database_path)

    def build_folder(self, meta):
        parts = [
            safe_component(meta.get("state_name")),
            safe_component(meta.get("year")),
            safe_component(meta.get("revision_folder") or meta.get("revision_label")),
            safe_component(meta.get("district_name")),
            "AC_" + safe_component(meta.get("assembly_number"), "Unknown")
        ]
        return Path(self.config.download_root).joinpath(*parts)

    @staticmethod
    def _part_name_for(number, meta):
        raw = (meta.get("part_names") or {}).get(str(number), "")
        raw = str(raw or "").strip()
        # Preserve word spaces, underscores and parentheses from ECI partName.
        raw = re.sub(r"[^A-Za-z0-9()_ ]+", "", raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        raw = re.sub(r"\s+\(", "_(", raw)
        raw = re.sub(r"\)\s+", ")_", raw)
        return raw or f"Part_{number}"

    @staticmethod
    def _build_download_filename(original_name, part_number, language_code, meta):
        """Build the requested normalized filename from ECI's generated name."""
        stem = Path(original_name).stem
        tokens = stem.split("-")
        revision_index = next(
            (i for i, token in enumerate(tokens)
             if re.fullmatch(r"Revision\d+", token, flags=re.IGNORECASE)),
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
        part_name = DownloadService._part_name_for(part_number, meta)
        components = [f"part{part_number}", source_language, revision_number]
        if roll_suffix:
            components.append(roll_suffix)
        components.append(part_name)

        filename = "_".join(components) + ".pdf"
        # Windows-invalid characters removed. Parentheses are retained because
        # they appear in the required example; no leading/trailing spaces remain.
        filename = re.sub(r'[<>:"/\\|?*]', "", filename).strip()
        filename = re.sub(r"_+", "_", filename)
        return filename

    def download_parts(self, meta, part_numbers, language_code, captcha_id, captcha_text):
        """Generate and download all PDFs for the provided parts in one request."""
        if not part_numbers:
            raise ValueError("Select at least one part")
        payload = {
            "stateCd": str(meta["state_code"]),
            "acNumber": int(meta["assembly_number"]) if str(meta["assembly_number"]).isdigit() else str(meta["assembly_number"]),
            "partNumberList": [int(p) if str(p).isdigit() else str(p) for p in part_numbers],
            "districtCd": str(meta.get("district_code") or ""),
            "captcha": captcha_text,
            "captchaId": captcha_id,
            "langCd": language_code,
            "publishedRollId": meta["revision_id"],
            "misKey": getattr(self.config, "mis_key", "EROLLA32DVI09AJH")
        }
        folder = self.build_folder(meta)
        def rename_immediately(file_index, downloaded_path):
            if file_index >= len(part_numbers):
                raise ECIClientError(
                    f"ECI returned more PDFs than requested parts ({len(part_numbers)})."
                )
            part_number = str(part_numbers[file_index])
            source_path = Path(downloaded_path)
            target_name = self._build_download_filename(
                source_path.name, part_number, language_code, meta
            )
            target_path = source_path.with_name(target_name)
            if target_path.exists() and target_path.resolve() != source_path.resolve():
                raise ECIClientError(
                    f"Refusing to overwrite existing downloaded file: {target_path.name}"
                )
            if target_path.resolve() != source_path.resolve():
                source_path.rename(target_path)
            return str(target_path)

        saved = self.client.download_published_pdfs(
            payload,
            output_dir=str(folder),
            on_file_downloaded=rename_immediately,
        )
        records = []
        for file_index, path in enumerate(saved):
            p = Path(path)
            if file_index >= len(part_numbers):
                raise ECIClientError(
                    f"ECI returned more PDFs ({len(saved)}) than requested parts "
                    f"({len(part_numbers)}); cannot safely match part names."
                )
            part = str(part_numbers[file_index])
            part_name = self._part_name_for(part, meta)
            part_folder = p.parent / part_name
            part_folder.mkdir(parents=True, exist_ok=True)
            part_copy = part_folder / p.name
            if p.resolve() != part_copy.resolve():
                shutil.copy2(p, part_copy)
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            record = {
                "state_name": meta.get("state_name"), "state_code": meta.get("state_code"),
                "year": str(meta.get("year")), "revision_label": meta.get("revision_label"),
                "revision_id": meta.get("revision_id"), "district_name": meta.get("district_name"),
                "district_code": meta.get("district_code"), "assembly_name": meta.get("assembly_name"),
                "assembly_number": str(meta.get("assembly_number")), "part_number": part,
                "language_code": language_code, "file_path": str(p.resolve()),
                "file_size": p.stat().st_size, "sha256": digest,
                "downloaded_at": datetime.now(timezone.utc).isoformat(), "status": "completed",
                "part_name": part_name, "part_folder": str(part_folder.resolve()),
                "extraction_path": ""
            }
            self.repository.record(record)
            records.append(record)
        self._write_manifest(folder, meta)
        return records

    def _write_manifest(self, folder, meta):
        manifest = folder / "download_manifest.json"
        existing = []
        if manifest.exists():
            try:
                existing = json.loads(manifest.read_text(encoding="utf-8")).get("files", [])
            except Exception:
                existing = []
        known = {x.get("file_path") for x in existing}
        for row in self.repository.list_downloads(
            state_code=meta.get("state_code"), year=meta.get("year"),
            district_code=meta.get("district_code"), assembly_number=meta.get("assembly_number")
        ):
            if row.get("file_path") not in known and Path(row.get("file_path", "")).exists():
                existing.append(row)
        manifest.write_text(json.dumps({"scope": meta, "files": existing}, indent=2, ensure_ascii=False), encoding="utf-8")

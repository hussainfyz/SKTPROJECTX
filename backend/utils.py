import re
from pathlib import Path

def safe_component(value, fallback="Unknown"):
    value = str(value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", "_", value).strip(" ._")
    return value[:100] or fallback

def normalize_revision(label):
    return safe_component(label, "Revision")

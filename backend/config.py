from dataclasses import dataclass
from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]

@dataclass
class AppConfig:
    api_base_url: str = "https://gateway-voters.eci.gov.in"
    cdn_base_url: str = "https://voters.eci.gov.in"
    content_loader_ext_base_url: str = "https://gateway-vpd.eci.gov.in"
    mis_key: str = "EROLLA32DVI09AJH"
    download_root: Path = ROOT / "data" / "downloads"
    database_path: Path = ROOT / "data" / "app.db"
    states_file: Path = ROOT / "config" / "states.json"

    def load_states(self):
        return json.loads(self.states_file.read_text(encoding="utf-8"))

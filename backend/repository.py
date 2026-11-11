import sqlite3
from pathlib import Path
from datetime import datetime

class DownloadRepository:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self):
        with self._connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    state_name TEXT, state_code TEXT, year TEXT,
                    revision_label TEXT, revision_id TEXT,
                    district_name TEXT, district_code TEXT,
                    assembly_name TEXT, assembly_number TEXT,
                    part_number TEXT, language_code TEXT,
                    file_path TEXT UNIQUE, file_size INTEGER,
                    sha256 TEXT, downloaded_at TEXT, status TEXT,
                    part_name TEXT, part_folder TEXT, extraction_path TEXT
                )
            """)
            # Upgrade databases created by earlier ZIP versions.
            existing = {row[1] for row in db.execute("PRAGMA table_info(downloads)")}
            for column in ("part_name", "part_folder", "extraction_path"):
                if column not in existing:
                    db.execute(f"ALTER TABLE downloads ADD COLUMN {column} TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS idx_download_scope ON downloads(state_code, year, district_code, assembly_number)")
            db.commit()

    def record(self, record):
        fields = [
            "state_name","state_code","year","revision_label","revision_id",
            "district_name","district_code","assembly_name","assembly_number",
            "part_number","language_code","file_path","file_size","sha256",
            "downloaded_at","status","part_name","part_folder","extraction_path"
        ]
        values = [record.get(k) for k in fields]
        with self._connect() as db:
            db.execute(
                f"INSERT OR REPLACE INTO downloads ({','.join(fields)}) VALUES ({','.join(['?']*len(fields))})",
                values
            )
            db.commit()

    def list_downloads(self, state_code=None, year=None, district_code=None, assembly_number=None):
        clauses, args = [], []
        for col, val in [
            ("state_code", state_code), ("year", year),
            ("district_code", district_code), ("assembly_number", assembly_number)
        ]:
            if val not in (None, ""):
                clauses.append(f"{col}=?")
                args.append(str(val))
        sql = "SELECT * FROM downloads"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CAST(part_number AS INTEGER), downloaded_at DESC"
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, args).fetchall()]

    def get_by_path(self, file_path):
        with self._connect() as db:
            row = db.execute("SELECT * FROM downloads WHERE file_path=?", (str(Path(file_path).resolve()),)).fetchone()
            return dict(row) if row else None

    def delete_by_path(self, file_path):
        with self._connect() as db:
            db.execute("DELETE FROM downloads WHERE file_path=?", (str(Path(file_path).resolve()),))
            db.commit()

    def distinct_values(self, column):
        allowed = {"state_name","state_code","year","revision_label","district_name","district_code","assembly_name","assembly_number"}
        if column not in allowed:
            raise ValueError("Unsupported column")
        with self._connect() as db:
            return [r[0] for r in db.execute(
                f"SELECT DISTINCT {column} FROM downloads WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}"
            ).fetchall()]

    def update_status(self, file_path, status, extraction_path=None, part_folder=None):
        """Update a download's processing status without replacing its metadata."""
        with self._connect() as db:
            if extraction_path is None and part_folder is None:
                db.execute(
                    "UPDATE downloads SET status=? WHERE file_path=?",
                    (status, str(Path(file_path).resolve()))
                )
            else:
                db.execute(
                    "UPDATE downloads SET status=?, extraction_path=COALESCE(?, extraction_path), "
                    "part_folder=COALESCE(?, part_folder) WHERE file_path=?",
                    (status, extraction_path, part_folder, str(Path(file_path).resolve()))
                )
            db.commit()


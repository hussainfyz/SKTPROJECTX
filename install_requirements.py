import subprocess
import sys
from pathlib import Path


def main():
    requirements_file = Path(__file__).with_name("requirements.txt")
    if not requirements_file.is_file():
        raise FileNotFoundError(f"Requirements file not found: {requirements_file}")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            str(requirements_file),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()

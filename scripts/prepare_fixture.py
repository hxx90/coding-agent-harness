"""Copy an immutable acceptance fixture into a new temporary workspace."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", choices=["e01", "e02"])
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    fixture_name = "e01_bugfix" if args.case == "e01" else "e02_feature"
    source = project_root / "tests" / "fixtures" / fixture_name
    destination_root = Path(tempfile.mkdtemp(prefix="harness-fixture-"))
    destination = destination_root / fixture_name
    shutil.copytree(source, destination)
    config_path = destination / ".agent-harness.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    command = list(config["validation_command"])
    if command[0] in {"python", "python3"}:
        command[0] = sys.executable
    config["validation_command"] = command
    config_path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(destination)


if __name__ == "__main__":
    main()

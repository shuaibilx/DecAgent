"""Export Compose's owner file to a new protected host file without printing the token."""

import argparse
import re
import subprocess
from pathlib import Path

from ops.control.prepare_secrets import write_new


def export_owner(destination):
    path = Path(destination).expanduser().resolve()
    if path.exists():
        raise FileExistsError("Existing owner file is preserved")
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "api",
            "python",
            "-c",
            "from pathlib import Path; print(Path('/data/owner.token').read_text(),end='')",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    token = result.stdout.strip()
    if not re.fullmatch(r"dca_[A-Za-z0-9_-]{43,128}", token):
        raise ValueError("API container did not return an owner credential")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_new(path, token)
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--destination", type=Path, default=Path(".decagent/compose-cli/owner.token")
    )
    args = parser.parse_args()
    print("Owner credential saved to " + str(export_owner(args.destination)))


if __name__ == "__main__":
    main()

"""Independent synthetic enterprise service ticket plugin."""

import json
from importlib.resources import files


def builtin_manifest():
    return json.loads(files(__package__).joinpath("manifest.json").read_text(encoding="utf-8"))

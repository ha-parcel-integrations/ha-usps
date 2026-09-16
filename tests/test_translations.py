"""Translation structure must remain identical across supported languages."""
import json
from pathlib import Path

ROOT = Path(__file__).parents[1] / "custom_components" / "usps"


def _keys(value: object, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return {prefix}
    return {key for name, child in value.items() for key in _keys(child, f"{prefix}.{name}".strip("."))}


def test_translation_key_structure_matches_strings():
    source = _keys(json.loads((ROOT / "strings.json").read_text()))
    for language in ("en", "nl"):
        assert _keys(json.loads((ROOT / "translations" / f"{language}.json").read_text())) == source

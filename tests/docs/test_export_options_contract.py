"""The documented export choices must remain accepted by the command."""
from pathlib import Path

from isar.cli.main import build_parser
from isar.workspace_api.export import ExportOptions


def test_export_choices_and_origin_headers_are_documented():
    root = Path(__file__).resolve().parents[2]
    with (root / "docs/WORKSPACE_AND_CLI.md").open(encoding="utf-8", newline="") as stream:
        contract = stream.read()
    with (root / "docs/USING_ISAR.md").open(encoding="utf-8", newline="") as stream:
        guide = stream.read()
    for key in ExportOptions.__dataclass_fields__:
        assert f"`{key}`" in contract
    args = build_parser().parse_args(["export", "--workspace", "example", "--options-json", "{}",
                                      "--destination", "exports/example", "--overwrite"])
    assert args.options_json == "{}" and args.overwrite
    assert "Long Name" in guide and "column designation" in guide and "row 5" in guide

"""Extracting AL source from .alpackages: NAVX header + zip, newest version wins,
symbol-only packages are skipped."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from graphify.al_packages import extract


def _app(path: Path, publisher: str, name: str, version: str, files: dict[str, str]) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("NavxManifest.xml",
                   f'<Package><App Id="x" Name="{name}" Publisher="{publisher}" Version="{version}" /></Package>')
        z.writestr("SymbolReference.json", "{}")
        for n, text in files.items():
            z.writestr(n, text)
    path.write_bytes(b"NAVX" + b"\0" * 36 + buf.getvalue())


def test_extracts_source_packages_and_skips_symbol_only(tmp_path: Path):
    pk = tmp_path / ".alpackages"
    pk.mkdir()
    _app(pk / "Microsoft_Base Application_28.0.app", "Microsoft", "Base Application", "28.0.1.0",
         {"src/Sales/SalesLine.Table.al": "table 37 \"Sales Line\" { }"})
    _app(pk / "Microsoft_Base Application_28.5.app", "Microsoft", "Base Application", "28.5.1.0",
         {"src/Sales/SalesLine.Table.al": "table 37 \"Sales Line\" { }",
          "src/Sales/SalesHeader.Table.al": "table 36 \"Sales Header\" { }"})
    _app(pk / "Partner_Closed_1.0.app", "Partner", "Closed", "1.0.0.0", {})
    done = extract(pk, tmp_path / "out")
    assert done == [("Microsoft_Base Application", "28.5.1.0", 2)]
    base = tmp_path / "out" / "Microsoft_Base Application"
    assert (base / "src" / "Sales" / "SalesHeader.Table.al").is_file()
    assert "28.5.1.0" in (base / "version.txt").read_text(encoding="utf-8")
    assert not (tmp_path / "out" / "Partner_Closed").exists()


def test_only_filters_by_folder_name(tmp_path: Path):
    pk = tmp_path / ".alpackages"
    pk.mkdir()
    _app(pk / "a.app", "Microsoft", "Base Application", "1.0.0.0", {"src/A.Codeunit.al": "codeunit 1 A { }"})
    _app(pk / "b.app", "Microsoft", "System Application", "1.0.0.0", {"src/B.Codeunit.al": "codeunit 2 B { }"})
    done = extract(pk, tmp_path / "out", only=["base"])
    assert [d[0] for d in done] == ["Microsoft_Base Application"]

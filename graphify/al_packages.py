"""Extract AL source from the .app packages in an AL project's .alpackages folder.

An .app file is a 40-byte NAVX header followed by a zip. Packages whose publisher
allows it (every Microsoft app, and partner apps that expose their code) carry the
full AL source under src/, which is what the graph needs; symbol-only packages carry
just SymbolReference.json and are skipped. Using the project's own packages gives a
graph that matches the exact versions the project compiles against.

usage: python -m graphify.al_packages <.alpackages folder> <output folder> [--only NAME_PART ...]

Each package with source goes to <output>/<publisher>_<name>/ (the version is in
version.txt there). When a folder holds several versions of one app, the newest wins.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

_NAVX_HEADER = 40


def _open_package(path: Path) -> zipfile.ZipFile | None:
    data = path.read_bytes()
    for offset in (_NAVX_HEADER, 0):
        try:
            return zipfile.ZipFile(io.BytesIO(data[offset:]))
        except zipfile.BadZipFile:
            continue
    return None


def _manifest(z: zipfile.ZipFile) -> dict:
    try:
        text = z.read("NavxManifest.xml").decode("utf-8", errors="replace")
    except KeyError:
        return {}
    m = re.search(r"<App\s([^>]*)>", text)
    return dict(re.findall(r'(\w+)="([^"]*)"', m.group(1))) if m else {}


def _long(path: Path) -> str:
    """Windows paths past 260 chars need the \\\\?\\ prefix (Base App has deep src trees)."""
    s = str(path)
    return "\\\\?\\" + s if os.name == "nt" and not s.startswith("\\\\?\\") else s


def _version_key(v: str) -> tuple:
    return tuple(int(p) if p.isdigit() else 0 for p in v.split("."))


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]+', "_", name).strip(" .")


def extract(packages: Path, out: Path, only: list[str] | None = None) -> list[tuple[str, str, int]]:
    """Extract every package with AL source; returns (folder, version, .al file count)."""
    newest: dict[str, tuple[tuple, Path, dict]] = {}
    for app in sorted(packages.glob("*.app")):
        z = _open_package(app)
        if z is None:
            continue
        info = _manifest(z)
        key = _safe(f"{info.get('Publisher', 'unknown')}_{info.get('Name', app.stem)}")
        if only and not any(o.lower() in key.lower() for o in only):
            continue
        ver = _version_key(info.get("Version", "0"))
        if key not in newest or ver > newest[key][0]:
            newest[key] = (ver, app, info)
    done = []
    for key, (_, app, info) in sorted(newest.items()):
        z = _open_package(app)
        al = [n for n in z.namelist() if n.lower().endswith(".al")]
        if not al:
            print(f"  skip {app.name}: no AL source (symbols only)")
            continue
        target = out.resolve() / key
        if target.exists():
            shutil.rmtree(_long(target))
        for name in al:
            dest = target / Path(*Path(name).parts)
            os.makedirs(_long(dest.parent), exist_ok=True)
            with open(_long(dest), "wb") as f:
                f.write(z.read(name))
        (target / "version.txt").write_text(f"{info.get('Name', '')} {info.get('Version', '')}\n", encoding="utf-8")
        done.append((key, info.get("Version", ""), len(al)))
        print(f"  {key} {info.get('Version', '')}: {len(al)} .al files")
    return done


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    only = None
    if "--only" in args:
        i = args.index("--only")
        only, args = args[i + 1:], args[:i]
    if len(args) != 2:
        raise SystemExit(__doc__)
    done = extract(Path(args[0]), Path(args[1]), only)
    print(f"{len(done)} packages with source extracted to {args[1]}")


if __name__ == "__main__":
    main()

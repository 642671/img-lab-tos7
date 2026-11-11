#!/usr/bin/env python3
"""Build a deterministic TOS 7 single-package Deb with the Python stdlib."""

from __future__ import annotations

import argparse
import bz2
import configparser
import gzip
import hashlib
import io
import json
import re
import shutil
import tarfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable, Mapping

ROOT = Path(__file__).resolve().parent
APP_ID = "img-lab"
SYSTEM_ID = "imglab-system"
PLATFORM_ARCH = {"x86_64": "amd64", "aarch64": "arm64"}
REQUIRED_LANGUAGES = {
    "zh-cn",
    "zh-hk",
    "en-us",
    "fr-fr",
    "de-de",
    "it-it",
    "es-es",
    "hu-hu",
    "ja-jp",
    "ko-kr",
    "pl-pl",
    "ru-ru",
    "tr-tr",
    "pt-pt",
}
REQUIRED_LANG_FIELDS = {"name", "auth", "descript", "release_note", "important"}
BACKEND_MODULES = (
    "__init__.py",
    "content.py",
    "encoders.py",
    "sizing.py",
    "library.py",
    "speedtest.py",
    "server.py",
)
MTIME = 1_704_067_200
ICON_MTIME_EPOCH = 1_792_627_200


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return value


def read_version() -> str:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+(?:\.\d+){0,2}", version):
        raise SystemExit("version must contain 1 to 3 numeric segments")
    return version


def validate_config(config: dict, version: str) -> None:
    required = {
        "id",
        "icon",
        "publisher",
        "exec",
        "version",
        "low_version",
        "category",
        "platform",
        "user",
        "system_id",
        "package",
        "application_type",
        "path",
        "type",
    }
    missing = sorted(required - set(config))
    if missing:
        raise SystemExit("config.ini is missing fields: " + ", ".join(missing))
    checks = {
        "id": APP_ID,
        "icon": f"/images/icons/{APP_ID}.svg",
        "publisher": "Yuii",
        "system_id": SYSTEM_ID,
        "package": APP_ID,
        "user": APP_ID,
        "application_type": "deb",
        "type": "iframe",
        "path": f"/{APP_ID}/",
        "version": version,
    }
    for field, expected in checks.items():
        if config.get(field) != expected:
            raise SystemExit(
                f"config.ini {field} must be {expected!r}, got {config.get(field)!r}"
            )
    if "open_path" in config:
        raise SystemExit("config.ini must not contain open_path with type=iframe")
    if config.get("platform") not in PLATFORM_ARCH:
        raise SystemExit("config.ini platform must be x86_64 or aarch64")
    if not isinstance(config.get("category"), list) or not config["category"]:
        raise SystemExit("config.ini category must be a non-empty list")


def validate_language(path: Path) -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    sections = set(parser.sections())
    if sections != REQUIRED_LANGUAGES:
        raise SystemExit(
            "language sections do not match the TOS 14-language set: "
            f"missing={sorted(REQUIRED_LANGUAGES - sections)}, "
            f"extra={sorted(sections - REQUIRED_LANGUAGES)}"
        )
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    for section in REQUIRED_LANGUAGES:
        for field in REQUIRED_LANG_FIELDS:
            if not parser[section].get(field, "").strip().strip('"'):
                raise SystemExit(f"language section {section} is missing {field}")
        if parser[section].get("auth", "").strip().strip('"') != "Yuii":
            raise SystemExit(f"language section {section} auth must be Yuii")
        if not parser[section].get("release_note", "").strip().strip('"').startswith(
            version
        ):
            raise SystemExit(f"language section {section} release_note must start with VERSION")


def validate_icon(path: Path) -> None:
    if not path.is_file():
        raise SystemExit(f"icon not found: {path}")
    raw = path.read_bytes()
    if len(raw) > 50 * 1024:
        raise SystemExit("icon must be at most 50 KB")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SystemExit(f"icon is not valid SVG/XML: {exc}") from exc
    if root.attrib.get("viewBox") != "0 0 64 64":
        raise SystemExit("icon viewBox must be 0 0 64 64")
    forbidden_tags = {"script", "foreignobject", "iframe", "object", "embed"}
    forbidden_prefixes = ("javascript:", "vbscript:", "file:", "ftp:", "about:", "blob:")
    count = 0
    for element in root.iter():
        count += 1
        tag = element.tag.rsplit("}", 1)[-1].lower()
        if tag in forbidden_tags:
            raise SystemExit(f"icon contains forbidden tag: {tag}")
        for name, value in element.attrib.items():
            if name.lower().startswith("on"):
                raise SystemExit(f"icon contains event attribute: {name}")
            if name.lower().endswith("href") and value.lower().startswith(forbidden_prefixes):
                raise SystemExit(f"icon contains unsafe href: {value}")
    if count > 50:
        raise SystemExit("icon contains too many XML elements")


def validate_backend_version(version: str) -> None:
    source = (ROOT / "backend" / "img_lab" / "__init__.py").read_text(
        encoding="utf-8"
    )
    if f'__version__ = "{version}"' not in source:
        raise SystemExit("backend version does not match VERSION")


def validate_service_contract() -> None:
    service = (ROOT / "init.d" / f"{SYSTEM_ID}.service").read_text(encoding="utf-8")
    executable = str(ROOT / "backend" / f"{APP_ID}-service")
    required = {
        f"ExecStart=/usr/bin/python3 /usr/local/{APP_ID}/bin/{APP_ID}-service",
        f"ExecStopPost=-/bin/rm -f /var/api/{APP_ID}.sock",
        f"EnvironmentFile=-/usr/local/{APP_ID}/{APP_ID}.env",
        "TimeoutStopSec=10",
        "KillSignal=SIGTERM",
        "Restart=on-failure",
        f"ReadWritePaths=/usr/local/{APP_ID}/data /usr/local/{APP_ID}/logs /var/api",
    }
    missing = sorted(item for item in required if item not in service)
    if missing:
        raise SystemExit("service unit is missing: " + ", ".join(missing))
    if re.search(r"^\s*Group\s*=", service, flags=re.MULTILINE):
        raise SystemExit("service unit must not set Group=")
    if re.search(r"^\s*PrivateTmp\s*=\s*(?:true|yes|1)\s*$", service, flags=re.MULTILINE | re.IGNORECASE):
        raise SystemExit("service unit must not enable PrivateTmp")
    postinst = (ROOT / "DEBIAN" / "postinst").read_text(encoding="utf-8")
    for marker in (
        "systemctl reset-failed",
        f"[ -S \"${{SOCKET_PATH}}\" ]",
        "systemctl is-active --quiet",
    ):
        if marker not in postinst:
            raise SystemExit(f"postinst is missing: {marker}")
    if not Path(executable).is_file():
        raise SystemExit(f"backend entry point not found: {executable}")


def normalize_lf(path: Path) -> bytes:
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def make_tar_bytes(
    entries: Iterable[tuple[Path, str]],
    content_overrides: Mapping[str, bytes] | None = None,
    mtime_overrides: Mapping[str, int] | None = None,
) -> bytes:
    buffer = io.BytesIO()
    text_suffixes = {
        ".css",
        ".env",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".lang",
        ".md",
        ".py",
        ".service",
        ".sh",
        ".txt",
        ".yml",
    }
    executable_names = {
        f"{APP_ID}-service",
        "postinst",
        "prerm",
        "postrm",
    }
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as archive:
        for source, archive_name in entries:
            if not source.exists():
                raise SystemExit(f"staging entry does not exist: {source}")
            info = archive.gettarinfo(str(source), arcname=archive_name)
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = (
                mtime_overrides.get(archive_name, MTIME)
                if mtime_overrides is not None
                else MTIME
            )
            if info.isdir():
                info.mode = 0o755
                archive.addfile(info)
                continue
            if info.isfile():
                info.mode = 0o755 if source.name in executable_names else 0o644
                override = (
                    content_overrides.get(archive_name)
                    if content_overrides is not None
                    else None
                )
                if override is not None:
                    data = override
                elif source.suffix.lower() in text_suffixes:
                    data = normalize_lf(source)
                else:
                    data = source.read_bytes()
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            else:
                archive.addfile(info)
    return buffer.getvalue()


def make_tar_gz(
    entries: Iterable[tuple[Path, str]],
    mtime_overrides: Mapping[str, int] | None = None,
) -> bytes:
    return gzip.compress(
        make_tar_bytes(entries, mtime_overrides=mtime_overrides),
        compresslevel=9,
        mtime=0,
    )


def make_tar_bz2(
    entries: Iterable[tuple[Path, str]],
    content_overrides: Mapping[str, bytes] | None = None,
) -> bytes:
    return bz2.compress(
        make_tar_bytes(entries, content_overrides=content_overrides),
        compresslevel=9,
    )


def release_icon_mtime(version: str) -> int:
    version_offset = int("".join(re.findall(r"\d+", version)) or "0")
    return ICON_MTIME_EPOCH + version_offset


def write_ar(path: Path, members: list[tuple[str, bytes]]) -> None:
    with path.open("wb") as output:
        output.write(b"!<arch>\n")
        for name, data in members:
            encoded_name = name.encode("ascii")
            if len(encoded_name) > 16:
                raise SystemExit(f"ar member name is too long: {name}")
            header = (
                encoded_name.ljust(16, b" ")
                + b"0".ljust(12, b" ")
                + b"0".ljust(6, b" ")
                + b"0".ljust(6, b" ")
                + b"100644".ljust(8, b" ")
                + str(len(data)).encode("ascii").ljust(10, b" ")
                + b"`\n"
            )
            output.write(header)
            output.write(data)
            if len(data) % 2:
                output.write(b"\n")


def read_ar(path: Path) -> dict[str, bytes]:
    raw = path.read_bytes()
    if not raw.startswith(b"!<arch>\n"):
        raise SystemExit(f"{path} is not an ar archive")
    members: dict[str, bytes] = {}
    offset = 8
    while offset < len(raw):
        header = raw[offset : offset + 60]
        if len(header) != 60 or header[58:60] != b"`\n":
            raise SystemExit("invalid ar member header")
        name = header[0:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        data_start = offset + 60
        data_end = data_start + size
        members[name] = raw[data_start:data_end]
        offset = data_end + (size % 2)
    return members


def verify_package(path: Path, platform: str, version: str) -> None:
    members = read_ar(path)
    if set(members) != {"debian-binary", "control.tar.gz", "data.tar.gz"}:
        raise SystemExit(f"unexpected deb members: {sorted(members)}")
    if members["debian-binary"] != b"2.0\n":
        raise SystemExit("debian-binary must contain 2.0")

    with tarfile.open(
        fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz"
    ) as control:
        names = {member.name for member in control.getmembers()}
        expected = {"./control", "./md5sums", "./postinst", "./prerm", "./postrm"}
        if not expected.issubset(names):
            raise SystemExit("control tar is missing lifecycle files")
        control_file = control.extractfile("./control")
        if control_file is None:
            raise SystemExit("control file cannot be read")
        control_text = control_file.read().decode("utf-8")
        if f"Version: {version}" not in control_text:
            raise SystemExit("control version mismatch")
        if f"Architecture: {PLATFORM_ARCH[platform]}" not in control_text:
            raise SystemExit("control architecture mismatch")
        for script_name in ("./postinst", "./prerm", "./postrm"):
            if not control.getmember(script_name).mode & 0o111:
                raise SystemExit(f"{script_name} is not executable")

    with tarfile.open(
        fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz"
    ) as data:
        data_names = {member.name for member in data.getmembers()}
        required = {
            f"./usr/local/{APP_ID}/config.ini",
            f"./usr/local/{APP_ID}/{APP_ID}.lang",
            f"./usr/local/{APP_ID}/{APP_ID}.env",
            f"./usr/local/{APP_ID}/LICENSE",
            f"./usr/local/{APP_ID}/bin/{APP_ID}-service",
            f"./usr/local/{APP_ID}/images/icons/{APP_ID}.svg",
            f"./usr/local/{APP_ID}/init.d/{SYSTEM_ID}.service",
            f"./usr/local/{APP_ID}/webui.bz2",
        }
        required.update(
            f"./usr/local/{APP_ID}/lib/img_lab/{name}" for name in BACKEND_MODULES
        )
        missing = required - data_names
        if missing:
            raise SystemExit("data tar is missing: " + ", ".join(sorted(missing)))
        config_file = data.extractfile(f"./usr/local/{APP_ID}/config.ini")
        if config_file is None:
            raise SystemExit("packaged config.ini cannot be read")
        packaged = json.loads(config_file.read().decode("utf-8"))
        if packaged.get("platform") != platform or packaged.get("version") != version:
            raise SystemExit("packaged config platform/version mismatch")
        entry = data.getmember(f"./usr/local/{APP_ID}/bin/{APP_ID}-service")
        if not entry.mode & 0o111:
            raise SystemExit("backend entry point is not executable")
        icon = data.getmember(f"./usr/local/{APP_ID}/images/icons/{APP_ID}.svg")
        if icon.mtime != release_icon_mtime(version):
            raise SystemExit("icon modification time was not version-busted")
        webui_file = data.extractfile(f"./usr/local/{APP_ID}/webui.bz2")
        if webui_file is None:
            raise SystemExit("webui.bz2 cannot be read")
        with tarfile.open(fileobj=io.BytesIO(webui_file.read()), mode="r:bz2") as webui:
            webui_names = {member.name for member in webui.getmembers()}
            required_webui = {
                "./index.html",
                "./app.js",
                "./styles.css",
                "./theme.js",
                "./tools.js",
                "./favicon.svg",
            }
            if not required_webui.issubset(webui_names):
                raise SystemExit("webui.bz2 is missing frontend files")
            index_file = webui.extractfile("./index.html")
            if index_file is None:
                raise SystemExit("webui index cannot be read")
            index_text = index_file.read().decode("utf-8")
            if "__APP_VERSION__" in index_text:
                raise SystemExit("webui index still contains __APP_VERSION__")
            if f"?v={version}" not in index_text:
                raise SystemExit("webui index does not use the release version in asset URLs")


def add_tree(entries: list[tuple[Path, str]], root: Path, arcroot: str) -> None:
    entries.append((root, arcroot))
    for path in sorted(root.rglob("*")):
        relative = "./" + path.relative_to(root).as_posix()
        entries.append((path, f"{arcroot.rstrip('/')}/{relative[2:]}"))


def build(platform: str, clean: bool) -> Path:
    if platform not in PLATFORM_ARCH:
        raise SystemExit("platform must be x86_64 or aarch64")
    version = read_version()
    config = read_json(ROOT / "config.ini")
    validate_config(config, version)
    validate_language(ROOT / f"{APP_ID}.lang")
    validate_icon(ROOT / "images" / "icons" / f"{APP_ID}.svg")
    validate_backend_version(version)
    validate_service_contract()

    build_dir = ROOT / "build" / platform
    staging = build_dir / "staging"
    output_dir = ROOT / "dist"
    if clean and build_dir.exists():
        shutil.rmtree(build_dir)
    if staging.exists():
        shutil.rmtree(staging)
    package_root = staging / "usr" / "local" / APP_ID
    for directory in (
        staging / "DEBIAN",
        package_root / "bin",
        package_root / "lib",
        package_root / "images" / "icons",
        package_root / "init.d",
        package_root / "data",
        package_root / "logs",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    staged_config = dict(config)
    staged_config["platform"] = platform
    (package_root / "config.ini").write_text(
        json.dumps(staged_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    shutil.copy2(ROOT / f"{APP_ID}.lang", package_root / f"{APP_ID}.lang")
    shutil.copy2(ROOT / f"{APP_ID}.env", package_root / f"{APP_ID}.env")
    shutil.copy2(ROOT / "LICENSE", package_root / "LICENSE")
    shutil.copy2(
        ROOT / "images" / "icons" / f"{APP_ID}.svg",
        package_root / "images" / "icons" / f"{APP_ID}.svg",
    )
    shutil.copy2(
        ROOT / "init.d" / f"{SYSTEM_ID}.service",
        package_root / "init.d" / f"{SYSTEM_ID}.service",
    )
    shutil.copy2(
        ROOT / "backend" / f"{APP_ID}-service",
        package_root / "bin" / f"{APP_ID}-service",
    )
    shutil.copytree(
        ROOT / "backend" / "img_lab",
        package_root / "lib" / "img_lab",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    frontend_entries: list[tuple[Path, str]] = []
    for source in sorted((ROOT / "frontend").rglob("*")):
        if source.is_file():
            frontend_entries.append(
                (source, "./" + source.relative_to(ROOT / "frontend").as_posix())
            )
    index_source = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    if "__APP_VERSION__" not in index_source:
        raise SystemExit("frontend index.html does not contain __APP_VERSION__")
    index_built = index_source.replace("__APP_VERSION__", version)
    (package_root / "webui.bz2").write_bytes(
        make_tar_bz2(
            frontend_entries,
            {"./index.html": index_built.encode("utf-8")},
        )
    )

    for script_name in ("control", "postinst", "prerm", "postrm"):
        shutil.copy2(ROOT / "DEBIAN" / script_name, staging / "DEBIAN" / script_name)
    control_path = staging / "DEBIAN" / "control"
    control_text = control_path.read_text(encoding="utf-8")
    control_text = re.sub(
        r"^Version:.*$", f"Version: {version}", control_text, flags=re.MULTILINE
    )
    control_text = re.sub(
        r"^Architecture:.*$",
        f"Architecture: {PLATFORM_ARCH[platform]}",
        control_text,
        flags=re.MULTILINE,
    )
    control_path.write_text(control_text, encoding="utf-8", newline="\n")

    data_entries: list[tuple[Path, str]] = []
    add_tree(data_entries, staging / "usr", "./usr")
    checksums: list[str] = []
    for path in sorted((staging / "usr").rglob("*")):
        if path.is_file():
            relative = path.relative_to(staging).as_posix()
            digest = hashlib.md5(path.read_bytes()).hexdigest()
            checksums.append(f"{digest}  {relative}\n")
    (staging / "DEBIAN" / "md5sums").write_text(
        "".join(checksums),
        encoding="ascii",
        newline="\n",
    )

    control_entries = [
        (staging / "DEBIAN" / name, f"./{name}")
        for name in ("control", "md5sums", "postinst", "prerm", "postrm")
    ]
    icon_archive_name = f"./usr/local/{APP_ID}/images/icons/{APP_ID}.svg"
    archive_path = output_dir / f"{APP_ID}_{version}_{platform}.deb"
    write_ar(
        archive_path,
        [
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", make_tar_gz(control_entries)),
            (
                "data.tar.gz",
                make_tar_gz(
                    data_entries,
                    {icon_archive_name: release_icon_mtime(version)},
                ),
            ),
        ],
    )
    verify_package(archive_path, platform, version)
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    (output_dir / f"{archive_path.name}.sha256").write_text(
        f"{checksum}  {archive_path.name}\n",
        encoding="ascii",
        newline="\n",
    )
    print(f"Built: {archive_path}")
    print("Verification: OK")
    print(f"SHA256: {checksum}")
    print(f"Size: {archive_path.stat().st_size} bytes")
    return archive_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Image Lab TOS 7 package")
    parser.add_argument(
        "--platform",
        choices=sorted(PLATFORM_ARCH),
        default="x86_64",
        help="target platform",
    )
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()
    build(args.platform, clean=not args.no_clean)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

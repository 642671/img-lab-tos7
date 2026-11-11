"""Static package-contract checks mirroring the TOS review gate."""

from __future__ import annotations

import configparser
import json
import re
import shutil
import subprocess
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LANGUAGES = {
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


class PackageContractTests(unittest.TestCase):
    def test_versions_match(self) -> None:
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        config = json.loads((ROOT / "config.ini").read_text(encoding="utf-8"))
        control = (ROOT / "DEBIAN" / "control").read_text(encoding="utf-8")
        backend = (ROOT / "backend" / "img_lab" / "__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertEqual(config["version"], version)
        self.assertIn(f"Version: {version}", control)
        self.assertIn(f'__version__ = "{version}"', backend)
        self.assertRegex(version, r"^\d+(?:\.\d+){0,2}$")

    def test_config_contract(self) -> None:
        config = json.loads((ROOT / "config.ini").read_text(encoding="utf-8"))
        self.assertEqual(config["id"], "img-lab")
        self.assertEqual(config["publisher"], "Yuii")
        self.assertEqual(config["user"], "img-lab")
        self.assertEqual(config["package"], "img-lab")
        self.assertEqual(config["system_id"], "imglab-system")
        self.assertEqual(config["icon"], "/images/icons/img-lab.svg")
        self.assertEqual(config["path"], "/img-lab/")
        self.assertEqual(config["type"], "iframe")
        self.assertEqual(config["application_type"], "deb")
        self.assertIn("Utilities", config["category"])
        self.assertNotIn("open_path", config)
        self.assertEqual(config["platform"], "x86_64")
        self.assertTrue(config["resize"])
        self.assertTrue(config["maxmin"])

    def test_service_contract(self) -> None:
        unit = (ROOT / "init.d" / "imglab-system.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("User=img-lab", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ProtectHome=true", unit)
        self.assertNotRegex(unit, r"(?m)^\s*Group\s*=")
        self.assertNotRegex(unit, r"(?mi)^\s*PrivateTmp\s*=\s*(true|yes|1)\s*$")
        self.assertIn(
            "ExecStart=/usr/bin/python3 /usr/local/img-lab/bin/img-lab-service",
            unit,
        )
        self.assertIn(
            "ExecStopPost=-/bin/rm -f /var/api/img-lab.sock",
            unit,
        )
        self.assertIn("EnvironmentFile=-/usr/local/img-lab/img-lab.env", unit)
        self.assertIn("TimeoutStopSec=10", unit)
        self.assertIn("KillSignal=SIGTERM", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("MemoryMax=1G", unit)
        self.assertIn("CPUQuota=200%", unit)
        self.assertIn(
            "ReadWritePaths=/usr/local/img-lab/data "
            "/usr/local/img-lab/logs /var/api",
            unit,
        )

    def test_environment_contract(self) -> None:
        env = (ROOT / "img-lab.env").read_text(encoding="utf-8")
        self.assertIn("IMG_LAB_DATA_DIR=/usr/local/img-lab/data", env)
        self.assertIn("IMG_LAB_FRONTEND_DIR=/usr/local/img-lab/webui", env)
        self.assertIn("IMG_LAB_SOCKET=/var/api/img-lab.sock", env)

    def test_language_contract(self) -> None:
        raw = (ROOT / "img-lab.lang").read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r\n", raw)
        self.assertNotIn(b"\r", raw)
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(raw.decode("utf-8"))
        self.assertEqual(set(parser.sections()), LANGUAGES)
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        for section in LANGUAGES:
            for field in ("name", "auth", "descript", "release_note", "important"):
                self.assertTrue(parser[section][field].strip(), f"{section}.{field}")
            self.assertEqual(parser[section]["auth"].strip().strip('"'), "Yuii")
            self.assertTrue(
                parser[section]["release_note"].strip().strip('"').startswith(version),
                section,
            )

    def test_lifecycle_safety_contract(self) -> None:
        for name in ("postinst", "prerm", "postrm"):
            path = ROOT / "DEBIAN" / name
            raw = path.read_bytes()
            self.assertTrue(raw.startswith(b"#!/bin/sh\n"), name)
            self.assertNotIn(b"\r\n", raw, name)
            self.assertNotIn(b"\r", raw, name)
            text = raw.decode("utf-8")
            self.assertNotIn("apt install", text)
            self.assertNotIn("chmod 0770 /var/api", text)
            self.assertNotIn("[[", text)
            self.assertNotIn("function ", text)
            self.assertNotIn("source ", text)

        postinst = (ROOT / "DEBIAN" / "postinst").read_text(encoding="utf-8")
        prerm = (ROOT / "DEBIAN" / "prerm").read_text(encoding="utf-8")
        postrm = (ROOT / "DEBIAN" / "postrm").read_text(encoding="utf-8")
        self.assertIn("systemctl reset-failed", postinst)
        self.assertIn("systemctl is-active --quiet", postinst)
        self.assertIn('[ -S "${SOCKET_PATH}" ]', postinst)
        self.assertIn("--gid allusers", postinst)
        self.assertIn("chmod 1777 /var/api", postinst)
        self.assertIn("systemctl stop", prerm)
        self.assertIn("systemctl disable", prerm)
        self.assertIn('[ -S "${SOCKET_PATH}" ]', prerm)
        self.assertIn('rm -rf "${APP_DIR}"', postrm)
        self.assertIn('if [ "$1" = "purge" ]', postrm)
        self.assertIn("userdel", postrm)
        self.assertNotIn("rm -rf /var/api", postrm)

    @unittest.skipUnless(shutil.which("sh"), "sh is not available on this host")
    def test_lifecycle_scripts_parse_with_sh(self) -> None:
        for name in ("postinst", "prerm", "postrm"):
            result = subprocess.run(
                ["sh", "-n", str(ROOT / "DEBIAN" / name)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_icon_and_favicon_contract(self) -> None:
        config = json.loads((ROOT / "config.ini").read_text(encoding="utf-8"))
        icon = ROOT / config["icon"].lstrip("/")
        self.assertTrue(icon.is_file())
        self.assertLessEqual(icon.stat().st_size, 50 * 1024)
        raw = icon.read_bytes()
        root = ET.fromstring(raw)
        self.assertEqual(root.attrib.get("viewBox"), "0 0 64 64")
        self.assertIsNone(re.search(r"<script\b", raw.decode("utf-8"), re.I))
        self.assertTrue((ROOT / "frontend" / "favicon.svg").is_file())

    def test_frontend_contract(self) -> None:
        html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
        tools = (ROOT / "frontend" / "tools.js").read_text(encoding="utf-8")
        theme = (ROOT / "frontend" / "theme.js").read_text(encoding="utf-8")
        server = (ROOT / "backend" / "img_lab" / "server.py").read_text(
            encoding="utf-8"
        )
        for element_id in (
            "generateForm",
            "modeSegmented",
            "widthInput",
            "heightInput",
            "targetSize",
            "formatSelect",
            "qualityRange",
            "contentOptions",
            "probeToggle",
            "generateButton",
            "cancelGenerateButton",
            "planCard",
            "jobBanner",
            "libraryList",
            "previewDialog",
            "speedForm",
            "speedUrl",
            "speedConcurrency",
            "speedRounds",
            "speedTimeout",
            "speedButton",
            "speedSamples",
            "themeToggle",
            "toast",
        ):
            self.assertIn(f'id="{element_id}"', html)
        self.assertIn('data-theme="light"', html)
        self.assertIn("__APP_VERSION__", html)
        self.assertNotRegex(html, r"<script(?![^>]*\bsrc=)")
        self.assertNotRegex(html, r"\son[a-z]+\s*=")
        self.assertIn("credentials: \"include\"", script)
        self.assertIn("X-Csrf-Token", script)
        self.assertIn('cache: "no-store"', script)
        self.assertIn('data.app === APP_ID', script)
        self.assertIn('var APP_ID = "img-lab"', script)
        self.assertIn('"/v2/proxy/" + APP_ID + "/"', script)
        self.assertIn('"/" + APP_ID + "/"', script)
        self.assertIn("window.parent.localStorage.getItem", theme)
        self.assertIn("root.ImgLabTools", tools)
        self.assertIn("var TOOLS = window.ImgLabTools", script)
        self.assertIn('"/health"', server)
        self.assertIn('"app": APP_ID', server)
        self.assertIn("Content-Security-Policy", server)
        self.assertIn("frame-ancestors", server)
        self.assertIn("X-Content-Type-Options", server)
        self.assertIn("/api/generate", server)
        self.assertIn("/api/speedtest", server)
        self.assertIn("/api/manifest.csv", server)

    def test_calibration_constants_match_between_backend_and_frontend(self) -> None:
        sizing = (ROOT / "backend" / "img_lab" / "sizing.py").read_text(
            encoding="utf-8"
        )
        tools = (ROOT / "frontend" / "tools.js").read_text(encoding="utf-8")
        for value in ("(85, 0.239)", "noise\": 3.172", "gradient\": 0.088"):
            self.assertIn(value, sizing)
        self.assertIn("[85, 0.239]", tools)
        self.assertIn("noise: 3.172", tools)
        self.assertIn("gradient: 0.088", tools)
        self.assertIn("photo: 1.92", tools)
        self.assertIn('"photo": 1.92', sizing)

    def test_build_contract(self) -> None:
        build = (ROOT / "build.py").read_text(encoding="utf-8")
        for marker in (
            'APP_ID = "img-lab"',
            'SYSTEM_ID = "imglab-system"',
            "PLATFORM_ARCH",
            "aarch64",
            "verify_package",
            "validate_service_contract",
            "webui.bz2",
        ):
            self.assertIn(marker, build)

    def test_docs_and_license_exist(self) -> None:
        for relative in (
            "README.md",
            "CHANGELOG.md",
            "CLAUDE.md",
            "LICENSE",
            "docs/PROJECT_LAYOUT.md",
            "docs/TOS7_SPEC_COMPLIANCE_CN.md",
            ".github/workflows/build.yml",
            ".gitignore",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("Copyright (c) 2026 Yuii", license_text)
        self.assertIn("MIT License", license_text)


if __name__ == "__main__":
    unittest.main()

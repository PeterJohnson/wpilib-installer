"""Build the private Qt worker and inspect its authoritative inventory."""
from pathlib import Path
import shutil
import tempfile
import xml.etree.ElementTree as ET
from .common import run
from .repository import tool, xml_file


def build_installer(ifw, output, *, maintenance_name="maintenancetool"):
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ifw-installer-") as tmp:
        root = Path(tmp)
        config = ET.Element("Installer")
        for tag, value in {"Name": "WPILib Package Engine", "Version": "0.1.0",
                           "Title": "WPILib package installation",
                           "Publisher": "WPILib", "TargetDir": "@HomeDir@/wpilib-packages",
                           "MaintenanceToolName": maintenance_name, "RemoveTargetDir": "false",
                           "RepositorySettingsPageVisible": "false", "SaveDefaultRepositories": "false",
                           "ControlScript": "controller.js"}.items():
            ET.SubElement(config, tag).text = value
        xml_file(config, root / "config.xml")
        shutil.copyfile(Path(__file__).parent / "assets" / "controller.js", root / "controller.js")
        # binarycreator requires an input package directory, even for online-only.
        meta = root / "packages" / "org.wpilib.bootstrap" / "meta"
        meta.mkdir(parents=True)
        (meta / "package.xml").write_text("<Package><DisplayName>Bootstrap</DisplayName>"
            "<Description>Internal bootstrap</Description><Version>1.0.0</Version>"
            "<ReleaseDate>2026-11-26</ReleaseDate><Default>false</Default></Package>")
        run([tool(ifw, "binarycreator"), "--online-only", "-c", root / "config.xml",
             "-p", root / "packages", output], timeout=180)
    return output


def executable(path):
    path = Path(path).resolve()
    if path.suffix == ".app":
        candidates = list((path / "Contents" / "MacOS").iterdir())
        if len(candidates) != 1:
            raise ValueError(f"Cannot identify application executable: {path}")
        path = candidates[0]
    return path


def inventory(root):
    path = Path(root) / "components.xml"
    if not path.is_file():
        raise ValueError(f"No Qt installation inventory at {path}")
    xml = ET.parse(path).getroot()
    result = {}
    for package in xml.findall("Package"):
        result[package.findtext("Name")] = package.findtext("Version")
    return result

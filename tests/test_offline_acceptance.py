import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from installer_tooling.offline_acceptance import (CHECKS, check_candidate, profile_closure,
    runner_for, windows_firewall)


class OfflineAcceptanceTests(unittest.TestCase):
    def test_unsupported_native_targets_fail_closed(self):
        for target in ("windows-arm64", "linux-arm64", "unknown"):
            with self.assertRaisesRegex(ValueError, "publication is blocked"):
                runner_for(target)
        self.assertEqual(runner_for("macos-arm64"), "macos-15")
        self.assertEqual(runner_for("macos-x64"), "macos-15-intel")

    def test_profiles_include_transitive_dependencies_and_reject_missing_components(self):
        packages = {"a": {"dependencies": {"b": "1"}}, "b": {"dependencies": {"c": "1"}}, "c": {}}
        self.assertEqual(profile_closure(packages, ["a"]), {"a", "b", "c"})
        with self.assertRaisesRegex(ValueError, "absent package"):
            profile_closure(packages, ["missing"])

    def test_windows_firewall_restored_when_install_or_rule_setup_fails(self):
        with patch("installer_tooling.offline_acceptance.subprocess.run") as command:
            with self.assertRaisesRegex(RuntimeError, "installation failed"):
                with windows_firewall([Path("C:/python.exe"), Path("C:/installer.exe")]):
                    raise RuntimeError("installation failed")
            calls = command.call_args_list
            self.assertEqual(len(calls), 2)
            self.assertIn("New-NetFirewallRule", calls[0].args[0][-1])
            self.assertIn("Remove-NetFirewallRule", calls[1].args[0][-1])
            self.assertEqual(calls[0].kwargs["env"]["OFFLINE_RULE_GROUP"], calls[1].kwargs["env"]["OFFLINE_RULE_GROUP"])
        with patch("installer_tooling.offline_acceptance.subprocess.run",
                   side_effect=[subprocess.CalledProcessError(1, "pwsh"), None]) as command:
            with self.assertRaises(subprocess.CalledProcessError):
                with windows_firewall([]):
                    self.fail("Failed firewall setup must not run the installer")
            self.assertEqual(command.call_count, 2)


@unittest.skipUnless(os.environ.get("IFW_PATH"), "Set IFW_PATH to enable native offline acceptance")
class NativeOfflineAcceptanceTests(unittest.TestCase):
    def test_native_exact_candidate_upgrade_and_initial_publication(self):
        from tools.check_offline_fixture import fixture
        with tempfile.TemporaryDirectory(prefix="offline-acceptance-") as tmp:
            root = Path(tmp)
            ifw = Path(os.environ["IFW_PATH"])
            result = fixture(ifw, root / "fixture", root / "upgrade.json")
            self.assertEqual(result["checks"], CHECKS)
            self.assertEqual(result["upgrade_mode"], "previous-bundle")
            self.assertNotEqual(result["sha256"], result["baseline_sha256"])
            self.assertEqual(result["inventory"]["org.wpilib.elastic"], "2.0.0")
            result = check_candidate(root / "fixture/initial", root / "fixture/demo/registry-source.json",
                                     ifw, root / "initial-check", root / "initial.json")
            self.assertEqual(result["upgrade_mode"], "initial-reinstall")
            self.assertEqual(result["sha256"], result["baseline_sha256"])
            self.assertEqual(result["status"], "passed")


if __name__ == "__main__":
    unittest.main()

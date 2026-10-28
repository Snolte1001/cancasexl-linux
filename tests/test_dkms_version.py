"""Check installer version selection without root, DKMS or system changes."""
import pathlib
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

class VersionSelection(unittest.TestCase):
    def run_selection(self, config, module):
        source = (ROOT / "kernel/install-dkms.sh").read_text()
        block = source[source.index("# Read the package version"):source.index("[[ $EUID")]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            (path / "dkms.conf").write_text(config)
            (path / "cancasexl.c").write_text(module)
            script = 'set -Eeuo pipefail\nKERNEL_DIR=$1\ndie() { echo "$*" >&2; exit 1; }\n'
            script += block + '\nprintf "%s" "$SOURCE"\n'
            return subprocess.run(["bash", "-c", script, "test", directory], capture_output=True, text=True)

    def test_current_and_future_versions(self):
        for version in ("0.1.20", "0.2.0"):
            result = self.run_selection(f'PACKAGE_VERSION="{version}"\n', f'MODULE_VERSION("{version}");\n')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, f"/usr/src/cancasexl-{version}")

    def test_mismatch(self):
        result = self.run_selection('PACKAGE_VERSION="0.1.20"\n', 'MODULE_VERSION("0.1.19");\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("differ", result.stderr)

    def test_missing_or_duplicate_version(self):
        for config in ("", 'PACKAGE_VERSION="0.1.20"\n' * 2):
            result = self.run_selection(config, 'MODULE_VERSION("0.1.20");\n')
            self.assertNotEqual(result.returncode, 0)

if __name__ == "__main__":
    unittest.main()

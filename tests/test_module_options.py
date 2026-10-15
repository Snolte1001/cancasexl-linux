"""Synthetic effective kmod configurations; no host module operations."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

CHECK = Path(__file__).resolve().parents[1] / 'kernel/check-module-options.sh'
BASE = 'options cancasexl isolate_can_errors=0 diagnose_channel=0 managed_handoff=1\n'

class ModuleOptionTests(unittest.TestCase):
    def run_check(self, config, status=0):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            (p/'modprobe').write_text('#!/bin/sh\nprintf "%s" "$FAKE_CONFIG"\nexit "$FAKE_STATUS"\n')
            (p/'modprobe').chmod(0o755)
            env = dict(os.environ, PATH=tmp+os.pathsep+os.environ['PATH'],
                       FAKE_CONFIG=config, FAKE_STATUS=str(status))
            return subprocess.run(['bash', str(CHECK)], env=env, capture_output=True, text=True)

    def test_defaults(self):
        self.assertEqual(self.run_check(BASE).returncode, 0)

    def test_identical_duplicates_and_boolean_aliases(self):
        self.assertEqual(self.run_check(BASE+'options cancasexl isolate_can_errors=N managed_handoff=Y\n').returncode, 0)

    def test_conflicts_in_either_order(self):
        override='options cancasexl isolate_can_errors=1\n'
        for value in (BASE+override, override+BASE):
            r=self.run_check(value)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('Conflicting cancasexl option isolate_can_errors',r.stderr)

    def test_other_managed_options(self):
        for value in ('diagnose_channel=1','managed_handoff=N'):
            self.assertNotEqual(self.run_check(BASE+'options cancasexl '+value+'\n').returncode,0)

    def test_missing_and_failed_query(self):
        self.assertNotEqual(self.run_check('').returncode,0)
        self.assertNotEqual(self.run_check(BASE,1).returncode,0)

    def test_unrelated_and_identical_optional_options(self):
        self.assertEqual(self.run_check(BASE+'options other isolate_can_errors=1\noptions cancasexl fast_poll=Y\noptions cancasexl fast_poll=1\n').returncode,0)

    def test_optional_conflict(self):
        self.assertNotEqual(self.run_check(BASE+'options cancasexl fast_poll=0\noptions cancasexl fast_poll=1\n').returncode,0)

if __name__ == '__main__':
    unittest.main()

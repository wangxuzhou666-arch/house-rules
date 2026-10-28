import json
import subprocess
import sys

from support import REPO_ROOT, RepoCase


class SampleTest(RepoCase):
    def test_failure_controls_and_permission_preservation(self):
        command = [sys.executable, "-B", str(REPO_ROOT / "samples/config-replacement/check_examples.py")]
        proc = subprocess.run(command, cwd=self.tmp, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertFalse(result["unsafe_config_preserves_after_io_failure"])
        self.assertTrue(result["safe_config_preserves_after_io_failure"])
        self.assertFalse(result["heldout_unsafe_preserves_after_serialization_failure"])
        self.assertTrue(result["heldout_safe_preserves_after_serialization_failure"])
        self.assertTrue(result["existing_private_permissions_preserved"])

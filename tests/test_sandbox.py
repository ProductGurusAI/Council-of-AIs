import unittest
import os
os.environ["TOTAL_BUDGET"] = "75.00"
import shutil
import json
import sys
from unittest.mock import patch
from io import StringIO
from council.sandbox import run_sandboxed, is_docker_available

class TestSandboxExecution(unittest.TestCase):
    def setUp(self):
        os.environ["TOTAL_BUDGET"] = "75.00"
        self.test_dir = "test_workspace_sandbox"
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)
        os.makedirs(self.test_dir)

    def tearDown(self):
        if os.path.exists(self.test_dir):
            shutil.rmtree(self.test_dir)

    def test_timeout_limit(self):
        # Run an infinite loop and verify it terminates with timeout
        # Using python -c "import time; time.sleep(10)"
        cmd = [sys.executable, "-c", "import time; time.sleep(10)"]
        rc, stdout, stderr = run_sandboxed(cmd, workdir=self.test_dir, timeout=1)
        self.assertEqual(rc, -1)
        self.assertIn("expired", stderr)

    def test_fallback_warning(self):
        # Mock docker availability to False
        # Verify warning "running without container isolation" is printed
        with patch("council.sandbox.is_docker_available", return_value=False):
            stderr_capture = StringIO()
            with patch("sys.stderr", stderr_capture):
                rc, stdout, stderr = run_sandboxed("echo 'hello'", workdir=self.test_dir, timeout=5)
                self.assertEqual(rc, 0)
                self.assertIn("hello", stdout.strip())
                self.assertIn("running without container isolation", stderr_capture.getvalue())

    def test_sandbox_required_lock(self):
        # Create a models.json with sandbox.required: true
        config = {
            "sandbox": {
                "required": True
            }
        }
        with open(os.path.join(self.test_dir, "models.json"), "w") as f:
            json.dump(config, f)

        # Mock docker to False, verify it refuses to execute
        with patch("council.sandbox.is_docker_available", return_value=False):
            rc, stdout, stderr = run_sandboxed("echo 'should fail'", workdir=self.test_dir, timeout=5)
            self.assertEqual(rc, -99)
            self.assertIn("Docker sandbox is required but Docker is not available", stderr)

    @unittest.skipUnless(is_docker_available(), "Docker is not available on host")
    def test_docker_isolation_file_write_outside_workdir(self):
        # Script writes a file to /tmp/docker_test_isolated.txt inside the container
        # We verify that no such file is created on the host /tmp/docker_test_isolated.txt
        host_target = "/tmp/docker_test_isolated.txt"
        if os.path.exists(host_target):
            os.remove(host_target)

        code = (
            "with open('/tmp/docker_test_isolated.txt', 'w') as f:\n"
            "    f.write('isolated')\n"
        )
        
        filename = os.path.join(self.test_dir, "write_test.py")
        with open(filename, "w") as f:
            f.write(code)

        rc, stdout, stderr = run_sandboxed(["python3", "write_test.py"], workdir=self.test_dir, timeout=10)
        self.assertEqual(rc, 0)
        
        # Host file must NOT exist (isolated inside container)
        self.assertFalse(os.path.exists(host_target))

    @unittest.skipUnless(is_docker_available(), "Docker is not available on host")
    def test_docker_isolation_network_block(self):
        # Try to make a network request to google.com inside container
        # Since --network=none, it must raise a connection error
        code = (
            "import urllib.request\n"
            "try:\n"
            "    urllib.request.urlopen('https://www.google.com', timeout=3)\n"
            "    print('network_ok')\n"
            "except Exception as e:\n"
            "    print('network_blocked:', type(e).__name__)\n"
        )
        filename = os.path.join(self.test_dir, "net_test.py")
        with open(filename, "w") as f:
            f.write(code)

        rc, stdout, stderr = run_sandboxed(["python3", "net_test.py"], workdir=self.test_dir, timeout=10)
        self.assertEqual(rc, 0)
        self.assertIn("network_blocked", stdout)
        self.assertNotIn("network_ok", stdout)

if __name__ == "__main__":
    unittest.main()

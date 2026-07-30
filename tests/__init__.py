"""Shared isolation for persistent storage created by the test suite."""

import atexit
import os
import shutil
import tempfile

_TEST_STORAGE_HOME = tempfile.mkdtemp(prefix="simple-agent-tests-")
os.environ.setdefault("SIMPLE_AGENT_HOME", _TEST_STORAGE_HOME)
atexit.register(shutil.rmtree, _TEST_STORAGE_HOME, ignore_errors=True)

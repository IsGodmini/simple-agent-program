from tests import _TEST_STORAGE_HOME  # noqa: F401

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from simple_agent.storage import ProjectStorage
from simple_agent.workspace import Workspace


class ProjectStorageTests(unittest.TestCase):
    def test_defaults_to_user_level_project_directory(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            home = base / "home"
            workspace_root = base / "workspace"
            home.mkdir()
            workspace_root.mkdir()
            with patch.dict(os.environ, {"SIMPLE_AGENT_HOME": ""}):
                with patch(
                    "simple_agent.storage.Path.home",
                    return_value=home,
                ):
                    storage = ProjectStorage(Workspace(workspace_root))

            expected_home = (home / ".simple-agent").resolve()
            self.assertEqual(storage.home, expected_home)
            self.assertEqual(storage.root.parent, expected_home / "projects")
            self.assertFalse(
                storage.root.is_relative_to(workspace_root)
            )

    def test_custom_home_keeps_projects_in_distinct_regions(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            custom = base / "state"
            first = base / "first"
            second = base / "second"
            first.mkdir()
            second.mkdir()
            with patch.dict(
                os.environ,
                {"SIMPLE_AGENT_HOME": str(custom)},
            ):
                first_storage = ProjectStorage(Workspace(first))
                second_storage = ProjectStorage(Workspace(second))

            self.assertNotEqual(
                first_storage.project_id,
                second_storage.project_id,
            )
            expected_projects = custom.resolve() / "projects"
            self.assertEqual(first_storage.root.parent, expected_projects)
            self.assertEqual(second_storage.root.parent, expected_projects)

    def test_copies_legacy_workspace_state_without_deleting_it(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            custom = base / "state"
            workspace_root = base / "workspace"
            legacy = workspace_root / ".simple-agent" / "memory"
            legacy.mkdir(parents=True)
            (legacy / "task_summaries.json").write_text(
                '{"tasks": []}',
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"SIMPLE_AGENT_HOME": str(custom)},
            ):
                storage = ProjectStorage(Workspace(workspace_root))

            migrated = storage.root / "memory" / "task_summaries.json"
            self.assertTrue(migrated.exists())
            self.assertTrue((legacy / "task_summaries.json").exists())

    def test_rejects_custom_storage_inside_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(
                os.environ,
                {"SIMPLE_AGENT_HOME": str(root / ".state")},
            ):
                with self.assertRaisesRegex(ValueError, "outside"):
                    ProjectStorage(Workspace(root))


if __name__ == "__main__":
    unittest.main()

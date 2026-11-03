from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from rss_path import register_user_path, update_path_value
from rss_update import main


class PathValueTests(unittest.TestCase):
    def test_append_preserves_existing_variables_and_separators(self):
        directory = r"D:\工具\RSS App"
        for old in ("", r"%USERPROFILE%\bin;C:\Tools", "C:\Tools;;"):
            with self.subTest(old=old):
                expected = old + (";" if old and not old.endswith(";") else "") + directory
                self.assertEqual(update_path_value(old, directory), expected)

    def test_install_is_idempotent_with_equivalent_windows_paths(self):
        directory = r"D:\Apps\RSS"
        with patch.dict(os.environ, {"RSS_TEST_ROOT": r"D:\Apps"}):
            for existing in ('"d:\\APPS\\rss\\"', r"%RSS_TEST_ROOT%\RSS", "D:/Apps/RSS"):
                old = "C:\\Tools;" + existing + ";;"
                with self.subTest(existing=existing):
                    self.assertEqual(update_path_value(old, directory), old)

    def test_remove_only_equivalent_entries_not_other_installations(self):
        old = r"C:\Tools;;D:\Apps\RSS;D:\Apps\RSS-old;d:\apps\rss\;%USERPROFILE%\bin;"
        self.assertEqual(update_path_value(old, r"D:\Apps\RSS", remove=True),
                         r"C:\Tools;;D:\Apps\RSS-old;%USERPROFILE%\bin;")
        self.assertEqual(update_path_value(old, r"D:\Other", remove=True), old)

    def test_invalid_directory_cannot_inject_a_second_path(self):
        for directory in ("", "D:\\RSS;D:\\Extra", "D:\\RSS\0"):
            with self.subTest(directory=directory), self.assertRaises(ValueError):
                update_path_value("C:\\Tools", directory)


class RegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="RSS 路径 ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        for name in ("rss.cmd", "rss-tui.cmd", "rss_update.py"):
            (self.directory / name).touch()
        self.key = object()
        context = MagicMock()
        context.__enter__.return_value = self.key
        self.registry = SimpleNamespace(
            HKEY_CURRENT_USER="HKCU", KEY_QUERY_VALUE=1, KEY_SET_VALUE=2,
            REG_SZ=1, REG_EXPAND_SZ=2,
            CreateKeyEx=MagicMock(return_value=context),
            QueryValueEx=MagicMock(return_value=(r"%USERPROFILE%\bin", 2)),
            SetValueEx=MagicMock(),
        )
        self.broadcast = self.enter_patch(patch("rss_path.broadcast_environment_change"))
        self.enter_patch(patch.dict(sys.modules, {"winreg": self.registry}))
        self.enter_patch(patch("rss_path.sys.platform", "win32"))

    def enter_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def test_writes_current_user_only_and_preserves_raw_value_and_type(self):
        for kind in (1, 2):
            with self.subTest(kind=kind):
                self.registry.QueryValueEx.return_value = (r"%USERPROFILE%\bin", kind)
                self.assertEqual(register_user_path(self.directory), (True, ""))
                self.registry.CreateKeyEx.assert_called_with("HKCU", "Environment", 0, 3)
                self.registry.SetValueEx.assert_called_with(
                    self.key, "Path", 0, kind, "%USERPROFILE%\\bin;" + str(self.directory))
        self.assertEqual(self.broadcast.call_count, 2)

    def test_duplicate_install_does_not_write_or_broadcast(self):
        self.registry.QueryValueEx.return_value = (str(self.directory), 2)
        self.assertEqual(register_user_path(self.directory), (False, ""))
        self.registry.SetValueEx.assert_not_called()
        self.broadcast.assert_not_called()

    def test_remove_keeps_other_paths(self):
        self.registry.QueryValueEx.return_value = ("C:\\Tools;" + str(self.directory), 1)
        self.assertEqual(register_user_path(self.directory, remove=True), (True, ""))
        self.registry.SetValueEx.assert_called_once_with(self.key, "Path", 0, 1, "C:\\Tools")

    def test_missing_path_is_created_as_expandable_string(self):
        self.registry.QueryValueEx.side_effect = FileNotFoundError
        self.assertEqual(register_user_path(self.directory), (True, ""))
        self.registry.SetValueEx.assert_called_once_with(self.key, "Path", 0, 2, str(self.directory))

    def test_unexpected_registry_value_is_left_untouched(self):
        for old, kind in ((42, 4), (42, 1)):
            self.registry.QueryValueEx.return_value = (old, kind)
            with self.subTest(old=old, kind=kind), self.assertRaises(ValueError):
                register_user_path(self.directory)
        self.registry.SetValueEx.assert_not_called()

    def test_broadcast_failure_reports_saved_path_with_warning(self):
        self.broadcast.side_effect = OSError("Timed out")
        self.assertEqual(register_user_path(self.directory), (True, "Timed out"))
        self.registry.SetValueEx.assert_called_once()

    def test_wsl_refuses_registry_changes(self):
        with patch("rss_path.sys.platform", "linux"), self.assertRaisesRegex(RuntimeError, "in Windows"):
            register_user_path(self.directory)
        self.registry.CreateKeyEx.assert_not_called()


class CommandTests(unittest.TestCase):
    def test_tui_aliases_use_default_installation_config_from_another_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "config.json").write_text(json.dumps({"feeds_file": "feeds.json", "data_dir": "data"}))
            previous = Path.cwd()
            try:
                os.chdir(directory.parent)
                with patch("rss_update.ROOT", directory), patch("rss_tui.RSSApp") as app:
                    for args in (["-tui"], ["--tui"], ["tui"]):
                        with self.subTest(args=args):
                            self.assertEqual(main(args), 0)
                            config = app.call_args.args[0]
                            self.assertEqual(config.path, directory / "config.json")
                            self.assertEqual(config.data_dir, directory / "data")
                    self.assertEqual(app.return_value.run.call_count, 3)
            finally:
                os.chdir(previous)
            self.assertFalse((directory / "data").exists())

    def test_path_setup_does_not_require_a_working_rss_config(self):
        with patch("rss_update.configure_path", return_value=0) as configure, patch("rss_update.load_config") as load:
            for command in ("install-path", "uninstall-path"):
                self.assertEqual(main(["--config", "missing.json", command]), 0)
                configure.assert_called_with(remove=command == "uninstall-path")
            load.assert_not_called()

    def test_tui_flag_rejects_conflicting_command(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(["-tui", "stop"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

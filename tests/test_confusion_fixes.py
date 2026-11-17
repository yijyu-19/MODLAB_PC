"""Checks for modern spelling, blank templates, access mode, and tool_only."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.access_mode import (
    AccessMode,
    GUEST_INSTALL_HOURLY_LIMIT,
    access_profile_fields,
    guest_install_payload,
    install_limits_enabled,
    open_session_args,
    resolve_access_mode,
    shows_guest_ui,
    skips_login_page,
)
from core.game_status import annotate_game_payload, is_tool_only, status_label
from core.mod_names import (
    CANONICAL_MOD_CLASS,
    LEGACY_DEPENDENCY_DIR,
    LEGACY_MOD_CLASS,
    MODERN_DEPENDENCY_DIR,
    canonicalize_dependency_path,
    classify_mod,
    dependency_id_from_launcher,
    fallback_dependency_path,
    form_token,
    normalize_install_type,
    resolve_dependency_dir,
    type_icon_filename,
)
from tools.sanitize_release_gameconfigs import (
    personal_path_hits,
    prepare_release_templates,
    repo_root,
    sanitize_text,
)

ROOT = repo_root()


class ModNamesTests(unittest.TestCase):
    def test_classify_and_icon_use_modern(self) -> None:
        self.assertEqual(classify_mod("assetbundle"), "classical_mod")
        self.assertEqual(classify_mod("mobile"), "mobile_mod")
        self.assertEqual(classify_mod(""), CANONICAL_MOD_CLASS)
        self.assertEqual(classify_mod("morden"), CANONICAL_MOD_CLASS)
        self.assertEqual(classify_mod(LEGACY_MOD_CLASS), CANONICAL_MOD_CLASS)
        self.assertEqual(form_token("morden_mod"), "modern")
        self.assertEqual(form_token("skinpack"), "")
        self.assertEqual(normalize_install_type("morden_mod"), "modern")
        self.assertEqual(normalize_install_type("classical_mod"), "classical")
        self.assertEqual(normalize_install_type("assetbundle"), "")
        self.assertEqual(normalize_install_type("skinpack"), "")
        self.assertEqual(type_icon_filename(LEGACY_MOD_CLASS), "modern.png")
        self.assertEqual(type_icon_filename("classical_mod"), "classical.png")

    def test_dual_read_and_create_modern(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            created = resolve_dependency_dir(game, create=True)
            self.assertEqual(created.name, MODERN_DEPENDENCY_DIR)
            self.assertTrue(created.is_dir())

        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            legacy = game / LEGACY_DEPENDENCY_DIR
            legacy.mkdir()
            (legacy / "M4A").mkdir()
            found = resolve_dependency_dir(game)
            self.assertEqual(found, legacy)
            self.assertFalse((game / MODERN_DEPENDENCY_DIR).exists())

        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp)
            (game / LEGACY_DEPENDENCY_DIR).mkdir()
            modern = game / MODERN_DEPENDENCY_DIR
            modern.mkdir()
            self.assertEqual(resolve_dependency_dir(game), modern)

    def test_path_rewrite_and_launcher_parse(self) -> None:
        raw = r"game\arknights\morden_dependency\M4A\mods"
        self.assertEqual(
            canonicalize_dependency_path(raw),
            r"game\arknights\modern_dependency\M4A\mods",
        )
        self.assertEqual(
            dependency_id_from_launcher(r"game\majsoul\morden_dependency\MajsoulMax\x.bat"),
            "MajsoulMax",
        )
        self.assertEqual(
            dependency_id_from_launcher("game/arknights/modern_dependency/M4A/M4ALauncher.exe"),
            "M4A",
        )

    def test_fallback_uses_legacy_only_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            game = root / "game" / "arknights"
            (game / LEGACY_DEPENDENCY_DIR / "M4A").mkdir(parents=True)
            path = fallback_dependency_path(root, "arknights", "M4A")
            self.assertEqual(path, game / LEGACY_DEPENDENCY_DIR / "M4A")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = fallback_dependency_path(root, "arknights", "M4A")
            self.assertEqual(
                path,
                root / "game" / "arknights" / MODERN_DEPENDENCY_DIR / "M4A",
            )


class AccessModeTests(unittest.TestCase):
    def test_shipping_default_is_open_full_access(self) -> None:
        mode = resolve_access_mode(login_enabled=False, is_logged_in=True, full_access=True)
        self.assertIs(mode, AccessMode.OPEN)
        self.assertFalse(install_limits_enabled(mode))
        self.assertFalse(shows_guest_ui(mode))
        self.assertTrue(skips_login_page(mode))
        fields = access_profile_fields(login_enabled=False, is_logged_in=True, full_access=True)
        self.assertEqual(fields["access_mode"], "open")
        self.assertFalse(fields["login_enabled"])
        self.assertFalse(fields["is_guest"])
        self.assertTrue(fields["full_access"])
        session = open_session_args()
        self.assertTrue(session["full_access_enabled"])
        self.assertFalse(session["debug"])

    def test_guest_quota_only_in_guest_mode(self) -> None:
        guest = resolve_access_mode(login_enabled=True, is_logged_in=True, full_access=False)
        self.assertIs(guest, AccessMode.GUEST)
        payload = guest_install_payload(
            login_enabled=True,
            is_logged_in=True,
            full_access=False,
            quota_used=1,
            wait_remaining=9,
        )
        self.assertTrue(payload["is_guest"])
        self.assertEqual(payload["quota_limit"], GUEST_INSTALL_HOURLY_LIMIT)
        self.assertEqual(payload["wait_remaining"], 9)

        full = guest_install_payload(login_enabled=True, is_logged_in=True, full_access=True, quota_used=4)
        self.assertEqual(full["access_mode"], "full")
        self.assertFalse(full["is_guest"])
        self.assertEqual(full["quota_limit"], 0)
        self.assertEqual(full["wait_remaining"], 0)

        debug = resolve_access_mode(login_enabled=True, is_debug=True, full_access=False)
        self.assertIs(debug, AccessMode.FULL)
        self.assertFalse(install_limits_enabled(debug))

        opened = guest_install_payload(login_enabled=False, quota_used=9, wait_remaining=30)
        self.assertEqual(opened["access_mode"], "open")
        self.assertEqual(opened["quota_used"], 0)


class GameStatusTests(unittest.TestCase):
    def test_half_finished_defaults_and_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mine = root / "minecraft"
            dol = root / "degrees_of_lewdity"
            ark = root / "arknights"
            mine.mkdir()
            dol.mkdir()
            ark.mkdir()
            self.assertTrue(is_tool_only(mine))
            self.assertTrue(is_tool_only(dol))
            self.assertFalse(is_tool_only(ark))
            self.assertEqual(status_label(mine), "仅工具")
            (ark / "gameconfig.ini").write_text("[Game]\ntool_only = true\n", encoding="utf-8")
            self.assertTrue(is_tool_only(ark))
            (mine / "gameconfig.ini").write_text("[Game]\ntool_only = false\n", encoding="utf-8")
            self.assertFalse(is_tool_only(mine))
            payload = annotate_game_payload({"name": "我的世界"}, dol)
            self.assertTrue(payload["tool_only"])
            self.assertIn("仅工具", payload["name"])

    def test_committed_templates(self) -> None:
        mine = ROOT / "game" / "minecraft"
        dol = ROOT / "game" / "degrees_of_lewdity"
        ark = ROOT / "game" / "arknights"
        self.assertTrue(is_tool_only(mine))
        self.assertTrue(is_tool_only(dol))
        self.assertFalse(is_tool_only(ark))
        self.assertFalse(is_tool_only(ROOT / "game" / "endfield"))


class TemplateTests(unittest.TestCase):
    def test_sanitize_clears_personal_paths_and_spelling(self) -> None:
        raw = "\n".join([
            "[Game]",
            r"exe = D:\Hypergryph Launcher\games\Arknights Game\Arknights.exe",
            r"launcher = C:\Users\11785\Desktop\tool.exe",
            r"mod_target_dir = game\arknights\morden_dependency\M4A\mods",
            "modern_dependency = M4A",
            "",
        ])
        updated, cleared = sanitize_text(raw)
        self.assertGreaterEqual(cleared, 2)
        self.assertEqual(personal_path_hits(updated), [])
        self.assertIn("modern_dependency", updated)
        self.assertNotIn(LEGACY_DEPENDENCY_DIR, updated)
        self.assertIn("exe =", updated)

    def test_repo_templates_are_blank(self) -> None:
        paths = prepare_release_templates(ROOT)
        self.assertGreaterEqual(len(paths), 6)
        for path in paths:
            text = path.read_text(encoding="utf-8")
            self.assertEqual(personal_path_hits(text), [], path.name)
            self.assertNotIn(LEGACY_DEPENDENCY_DIR, text, path.name)
            self.assertNotIn("C:\\Users", text)
            self.assertNotIn("D:\\Hypergryph", text)

    def test_first_party_morden_is_legacy_alias_only(self) -> None:
        allowed = {ROOT / "core" / "mod_names.py"}
        hits: list[str] = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in {".py", ".ini", ".js", ".md", ".spec"}:
                continue
            if ".git" in path.parts or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if "morden" not in text.lower():
                continue
            if path in allowed:
                continue
            hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [])


def _install_stub(name: str, **attrs):
    import sys
    import types

    module = sys.modules.get(name)
    if module is None or not isinstance(module, types.ModuleType):
        module = types.ModuleType(name)
        sys.modules[name] = module
    for key, value in attrs.items():
        setattr(module, key, value)
    parent_name, _, child = name.rpartition(".")
    parent = sys.modules.get(parent_name)
    if parent is not None and child:
        setattr(parent, child, module)
    return module


class _Paths:
    root = ROOT


class _Prefs:
    selected = ""
    guest = {"hour_key": "", "count": 0, "wait_started_at": 0.0}
    sponsor: dict = {}


def _ensure_runtime_stubs() -> None:
    """桌面模块依赖尚未入库的邻居。测试只补导入所需的空壳。"""
    _install_stub("core.paths", get_app_root=lambda: _Paths.root)
    _install_stub(
        "core.i18n",
        current_language=lambda: "ZH_CN",
        t=lambda _key, fallback="": fallback,
        list_languages=lambda: [],
    )
    _install_stub(
        "core.prefs",
        get_selected_game_id=lambda: _Prefs.selected,
        set_selected_game_id=lambda value: setattr(_Prefs, "selected", value),
        get_guest_install=lambda: dict(_Prefs.guest),
        set_guest_install=lambda data: setattr(_Prefs, "guest", dict(data)),
        get_sponsor_background=lambda: dict(_Prefs.sponsor),
        set_sponsor_background=lambda data: setattr(_Prefs, "sponsor", dict(data)),
    )
    _install_stub("core.dialogs", pick_file=lambda *args, **kwargs: "", pick_path=lambda *args, **kwargs: "")
    _install_stub(
        "core.dol",
        supports_mods=lambda *args, **kwargs: True,
        supports_mod_toggle=lambda *args, **kwargs: True,
        package_display_name=lambda path: getattr(path, "name", ""),
    )
    _install_stub(
        "core.emulator",
        adb_pull=None,
        adb_resolve_in_dir=None,
        connect_adb=None,
        get_adb_status=None,
        resolve_android_bundles_root=None,
    )
    _install_stub("core.pinyin_search", pinyin_keys=lambda *args, **kwargs: [])
    _install_stub(
        "core.py_script",
        run_python_script=None,
        popen_python_script=None,
        python_script_command=None,
    )
    _install_stub(
        "core.purchases",
        can_edit_sponsor_background=lambda: False,
        is_sponsor_eligible=lambda: False,
    )
    _install_stub("core.game_caps", capabilities=lambda folder: {}, has=lambda *args, **kwargs: False)


class DesktopWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _ensure_runtime_stubs()
        import core.app_state as app_state
        import core.arknights_mod_adapt as adapt
        import core.dependency as dependency
        import core.games as games
        import core.install as install
        import core.mod_convert as mod_convert
        import core.mods as mods
        import core.settings as settings
        import core.sponsor_bg as sponsor_bg

        cls.app_state = app_state
        cls.adapt = adapt
        cls.dependency = dependency
        cls.games = games
        cls.install = install
        cls.mod_convert = mod_convert
        cls.mods = mods
        cls.settings = settings
        cls.sponsor_bg = sponsor_bg

    def setUp(self) -> None:
        state = self.app_state
        state.login_enabled = False
        state.is_logged_in = False
        state.full_access = False
        state.is_debug = False
        state.user_profile = {}
        state.selected_game = {}
        _Prefs.selected = ""
        _Prefs.guest = {"hour_key": "", "count": 0, "wait_started_at": 0.0}
        _Prefs.sponsor = {}
        _Paths.root = ROOT

    def test_dependency_root_and_launcher_use_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "arknights"
            legacy = game / LEGACY_DEPENDENCY_DIR / "M4A"
            legacy.mkdir(parents=True)
            (legacy / "keep.txt").write_text("x", encoding="utf-8")
            self.assertEqual(self.dependency.dependency_root(game), legacy.parent)
            (game / "gameconfig.ini").write_text(
                "[Game]\nlauncher = game/arknights/morden_dependency/M4A/M4ALauncher.exe\n",
                encoding="utf-8",
            )
            self.assertEqual(self.dependency.infer_default_dependency(game), "M4A")
            modern = game / MODERN_DEPENDENCY_DIR / "MajsoulMax"
            modern.mkdir(parents=True)
            (modern / "keep.txt").write_text("x", encoding="utf-8")
            self.assertEqual(self.dependency.dependency_root(game), modern.parent)
            (game / "gameconfig.ini").write_text(
                "[Game]\nlauncher = game/arknights/modern_dependency/MajsoulMax/run.bat\n",
                encoding="utf-8",
            )
            self.assertEqual(self.dependency.infer_default_dependency(game), "MajsoulMax")

    def test_mod_class_and_install_type_delegate(self) -> None:
        self.assertEqual(self.mods._classify_mod("morden"), CANONICAL_MOD_CLASS)
        self.assertEqual(self.mods._classify_mod("assetbundle"), "classical_mod")
        self.assertEqual(self.mods._form_token("ui_ab"), "modern")
        self.assertEqual(self.mods._form_token("skinpack"), "")
        self.assertEqual(self.mods._dominant_mod_class([]), CANONICAL_MOD_CLASS)
        self.assertEqual(self.mod_convert.normalize_mod_type("morden"), "modern")
        self.assertEqual(self.mod_convert.normalize_mod_type("mobile_mod"), "mobile")

    def test_m4a_fallbacks_follow_on_disk_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _Paths.root = root
            legacy_root = root / "game" / "arknights" / LEGACY_DEPENDENCY_DIR
            legacy_root.mkdir(parents=True)
            legacy = legacy_root / "M4A"
            self.assertEqual(self.mods._arknights_dependency_dir(), legacy)
            self.assertEqual(self.settings._m4a_config_path(), legacy / "config.ini")
            self.assertEqual(self.adapt.bootstrapper_dll_path(), legacy / "Bootstrapper.dll")
            modern = root / "game" / "arknights" / MODERN_DEPENDENCY_DIR / "M4A"
            modern.parent.mkdir()
            self.assertEqual(self.mods._arknights_dependency_dir(), modern)
            self.assertEqual(self.settings._m4a_config_path(), modern / "config.ini")

    def test_game_payloads_carry_tool_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _Paths.root = root
            mine = root / "game" / "minecraft"
            ark = root / "game" / "arknights"
            mine.mkdir(parents=True)
            ark.mkdir(parents=True)
            (mine / "gameconfig.ini").write_text("[Game]\n", encoding="utf-8")
            (ark / "gameconfig.ini").write_text("[Game]\n", encoding="utf-8")
            listed = self.games.list_games()
            by_id = {item["id"]: item for item in listed["games"]}
            self.assertTrue(by_id["minecraft"]["tool_only"])
            self.assertEqual(by_id["minecraft"]["status"], "tool_only")
            self.assertIn("仅工具", by_id["minecraft"]["name"])
            self.assertFalse(by_id["arknights"]["tool_only"])
            loaded = self.games.select_game("degrees_of_lewdity")
            self.assertFalse(loaded.get("ok"))
            dol = root / "game" / "degrees_of_lewdity"
            dol.mkdir()
            (dol / "gameconfig.ini").write_text("[Game]\n", encoding="utf-8")
            loaded = self.games.select_game("degrees_of_lewdity")
            self.assertTrue(loaded["tool_only"])
            self.assertEqual(loaded["status"], "tool_only")

    def test_open_mode_is_not_guest_for_profile_install_and_sponsor(self) -> None:
        state = self.app_state
        state.login_enabled = False
        state.is_logged_in = True
        state.full_access = False
        session = state.get_session_info()
        self.assertEqual(session["access_mode"], "open")
        self.assertFalse(session["login_enabled"])
        self.assertFalse(session["is_guest"])
        self.assertTrue(session["full_access"])
        self.assertFalse(self.install._is_guest_install_user())
        status = self.install.guest_install_status()
        self.assertFalse(status["is_guest"])
        self.assertEqual(status["quota_limit"], 0)
        self.assertEqual(status["wait_remaining"], 0)
        self.assertIn("hour_key", status)
        self.assertIsNone(self.install._install_access())
        sponsor = self.sponsor_bg.get_sponsor_background_state()
        self.assertFalse(sponsor["is_guest"])
        self.assertEqual(sponsor["access_mode"], "open")
        self.assertFalse(sponsor["login_enabled"])

        state.login_enabled = True
        state.is_logged_in = True
        state.full_access = False
        state.is_debug = False
        self.assertTrue(self.install._is_guest_install_user())
        guest = self.install.guest_install_status()
        self.assertTrue(guest["is_guest"])
        self.assertEqual(guest["quota_limit"], GUEST_INSTALL_HOURLY_LIMIT)
        self.assertEqual(guest["access_mode"], "guest")
        self.assertTrue(state.get_session_info()["is_guest"])
        _Prefs.guest = {"hour_key": guest["hour_key"], "count": GUEST_INSTALL_HOURLY_LIMIT, "wait_started_at": 0.0}
        blocked = self.install._install_access()
        self.assertIsNotNone(blocked)
        self.assertEqual(blocked["code"], "guest_quota")

        state.is_debug = True
        self.assertFalse(self.install._is_guest_install_user())
        self.assertIsNone(self.install._install_access())

    def test_packagers_call_template_helpers(self) -> None:
        import build_release
        import rebuild_zip_only

        names = {part for path in build_release.POST_CLEAN_FILES for part in Path(path).parts}
        self.assertIn(MODERN_DEPENDENCY_DIR, names)
        self.assertIn(LEGACY_DEPENDENCY_DIR, names)
        for path in (ROOT / "build_release.py", ROOT / "rebuild_zip_only.py", ROOT / "MODLAB_PC.spec"):
            text = path.read_text(encoding="utf-8")
            self.assertIn("prepare_release_templates", text, path.name)
            self.assertIn("gameconfig_datas", text, path.name)
            self.assertNotIn("morden", text.lower(), path.name)
        self.assertTrue(callable(rebuild_zip_only.main))


if __name__ == "__main__":
    unittest.main()

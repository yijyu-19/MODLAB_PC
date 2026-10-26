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
            if path.suffix not in {".py", ".ini", ".js", ".md"}:
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


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

import savething as g

DATA = Path(__file__).parent / "testdata"
HOME = "C:/Users/player"
STEAM = ["E:/steam"]
SKIP = STEAM + ["C:/Program Files/WindowsApps"]


def load():
    preview = json.loads((DATA / "preview_sample.json").read_text(encoding="utf-8"))
    manifest = json.loads((DATA / "manifest_sample.json").read_text(encoding="utf-8"))
    return preview, manifest


class PortablePath(unittest.TestCase):
    def test_home_relative_round_trip_with_other_username(self):
        p = g.to_portable("C:\\Users\\player\\AppData\\LocalLow\\Team Cherry\\Hollow Knight", HOME)
        self.assertEqual(p, "<home>/AppData/LocalLow/Team Cherry/Hollow Knight")
        self.assertEqual(g.from_portable(p, "C:/Users/friend"),
                         "C:/Users/friend/AppData/LocalLow/Team Cherry/Hollow Knight")

    def test_case_insensitive_home(self):
        self.assertEqual(g.to_portable("c:/users/PLAYER/Saved Games/X", HOME), "<home>/Saved Games/X")

    def test_outside_home_stays_absolute(self):
        p = g.to_portable("C:/XboxGames/GameSave/pgs/u_1", HOME)
        self.assertEqual(p, "C:/XboxGames/GameSave/pgs/u_1")
        self.assertEqual(g.from_portable(p, "C:/Users/friend"), p)

    def test_similar_prefix_is_not_home(self):
        self.assertEqual(g.to_portable("C:/Users/player2/x", HOME), "C:/Users/player2/x")


class Roots(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preview, cls.manifest = load()

    def roots(self, name):
        files = list(self.preview[name]["files"])
        pats = list(self.manifest.get(name, {}).get("files", {}))
        return g.compute_roots(files, pats, HOME, SKIP)

    def test_absolum_file_patterns_use_parent_dir(self):
        self.assertEqual(self.roots("Absolum")[0], [HOME + "/AppData/Local/Absolum_SaveGame_Steam"])

    def test_hades(self):
        self.assertEqual(self.roots("Hades II")[0], [HOME + "/Saved Games/Hades II"])

    def test_outer_wilds(self):
        self.assertEqual(self.roots("Outer Wilds")[0],
                         [HOME + "/AppData/LocalLow/Mobius Digital/Outer Wilds/SteamSaves"])

    def test_my_games_is_not_synced_whole(self):
        roots = self.roots("Dragon Quest I & II HD-2D Remake")[0]
        self.assertEqual(len(roots), 1)
        self.assertTrue(roots[0].startswith(HOME + "/Documents/My Games/DRAGON QUEST"))

    def test_forza_two_locations(self):
        roots = self.roots("Forza Horizon 6")[0]
        self.assertEqual(sorted(roots), sorted([
            "C:/XboxGames/GameSave/pgs/u_1111222233334444_16D460",
            HOME + "/AppData/Local/ForzaHorizon6/LocalStorage_Shared"]))

    def test_sibling_dirs_merge(self):
        self.assertEqual(self.roots("Disco Elysium")[0],
                         [HOME + "/AppData/LocalLow/ZAUM Studio/Disco Elysium"])

    def test_different_bases_do_not_merge(self):
        self.assertEqual(len(self.roots("Sid Meier's Civilization VI")[0]), 2)

    def test_steam_userdata_skipped(self):
        roots, skipped, _ = self.roots("Astlibra Revision")
        self.assertEqual(roots, [HOME + "/AppData/Local/ASTLIBRA/SAVE"])
        self.assertTrue(skipped and all(s.startswith("E:/steam") for s in skipped))

    def test_no_root_is_broad(self):
        broad = {g.pkey(b) for b in g.broad_dirs(HOME)}
        for name in self.preview:
            for r in self.roots(name)[0]:
                self.assertNotIn(g.pkey(r), broad, f"{name}: {r}")

    def test_file_directly_in_broad_dir_is_problem(self):
        roots, _, problems = g.compute_roots([HOME + "/AppData/Local/loose.sav"], [], HOME)
        self.assertEqual(roots, [])
        self.assertEqual(problems, [HOME + "/AppData/Local/loose.sav"])

    def test_unmatched_file_uses_anchor(self):
        roots, _, _ = g.compute_roots([HOME + "/AppData/Roaming/Foo/sub/a.sav"], [], HOME)
        self.assertEqual(roots, [HOME + "/AppData/Roaming/Foo"])


class Steam(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preview, cls.manifest = load()

    def skipped(self, name):
        return g.all_skipped(list(self.preview[name]["files"]), SKIP)

    def test_steam_games_are_not_excluded(self):
        self.assertFalse(self.skipped("PUBG: Battlegrounds"))  # installed in Steam, saves in AppData
        self.assertFalse(self.skipped("Astlibra Revision"))  # Steam userdata plus AppData
        self.assertFalse(self.skipped("Hades II"))

    def test_saves_only_in_steam_dir(self):
        self.assertTrue(self.skipped("Dota 2"))  # only Steam userdata

    def test_prefix_in_steam_library_is_not_skipped(self):
        env = g.prefix_env(f"{LIB}/steamapps/compatdata/570/pfx")
        self.assertEqual(g.row_skip_dirs(env, [LIB, "/x"]), ["/x"])
        self.assertEqual(g.row_skip_dirs(None, [LIB]), [LIB])

    def test_library_parsing(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "steamapps").mkdir()
            (Path(d) / "steamapps" / "appmanifest_123.acf").write_text("x")
            (Path(d) / "steamapps" / "appmanifest_bad.acf").write_text("x")
            self.assertEqual(g.installed_steam_ids([d]), {123})


class Misc(unittest.TestCase):
    def test_parse_selection(self):
        self.assertEqual(g.parse_selection("1,3,5-7", 8), [0, 2, 4, 5, 6])
        self.assertEqual(g.parse_selection("2\uff0c1 2", 3), [1, 0])  # full-width comma
        self.assertEqual(g.parse_selection("3-1", 3), [0, 1, 2])
        with self.assertRaises(ValueError):
            g.parse_selection("9", 3)
        with self.assertRaises(ValueError):
            g.parse_selection("a", 3)

    def test_slugify(self):
        self.assertEqual(g.slugify("Hades II"), "hades-ii")
        self.assertEqual(g.slugify("Sid Meier's Civilization VI"), "sid-meier-s-civilization-vi")
        s = g.slugify("\u30ec\u30a4\u30b8\u30f3\u30b0")  # non-ASCII name
        self.assertTrue(s.startswith("g") and len(s) == 9)

    def test_pattern_prefix(self):
        self.assertEqual(g.pattern_prefix("<winDocuments>/My Games/X/<storeUserId>/*.sav", HOME),
                         HOME + "/Documents/My Games/X")
        self.assertIsNone(g.pattern_prefix("<base>/save/*", HOME))

    def test_registry_skips_conflicts(self):
        with tempfile.TemporaryDirectory() as d:
            g.write_entry(Path(d), {"folder_id": "save-a", "game": "A"})
            (Path(d) / "save-a.sync-conflict-20260101-000000-ABCDEFG.json").write_text('{"folder_id":"save-x"}')
            (Path(d) / "save-broken.json").write_text("{")
            self.assertEqual(list(g.read_registry(Path(d))), ["save-a"])

    def test_nesting_conflict(self):
        folders = [{"id": "save-a", "path": "C:\\Users\\x\\AppData\\Local\\Game\\Saved\\Config"}]
        self.assertIsNotNone(g.nesting_conflict("C:/Users/x/AppData/Local/Game/Saved", folders))
        self.assertIsNone(g.nesting_conflict("C:/Users/x/AppData/Local/Game2", folders))


class Settings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preview, cls.manifest = load()

    def split(self, name):
        return g.classify_files(list(self.preview[name]["files"]), self.manifest.get(name, {}), HOME)

    def plan(self, name):
        saves, configs = self.split(name)
        pats = list(self.manifest.get(name, {}).get("files", {}))
        roots, _, _ = g.compute_roots(saves, pats, HOME, SKIP)
        return roots, {r: g.config_ignores(r, saves, configs) for r in roots}

    def test_pattern_regex(self):
        rx = g.pattern_regex("<winLocalAppData>/Foo/*/save?.dat", HOME)
        self.assertTrue(rx.match(HOME + "/AppData/Local/Foo/profile/save1.dat"))
        self.assertTrue(rx.match("c:/users/PLAYER/appdata/local/foo/p/SAVE1.DAT"))
        self.assertFalse(rx.match(HOME + "/AppData/Local/Foo/a/b/save1.dat"))
        deep = g.pattern_regex("<winDocuments>/Game/**/*.sav", HOME)
        self.assertTrue(deep.match(HOME + "/Documents/Game/a/b/c.sav"))
        folder = g.pattern_regex("<winAppData>/Game/Saves", HOME)
        self.assertTrue(folder.match(HOME + "/AppData/Roaming/Game/Saves/slot1/data.bin"))
        self.assertIsNone(g.pattern_regex("<base>/cfg/*", HOME))

    def test_hades_settings_file(self):
        saves, configs = self.split("Hades II")
        self.assertEqual([c.rsplit("/", 1)[1] for c in configs], ["GlobalSettingsWin.sjson"])
        self.assertIn(HOME + "/Saved Games/Hades II/Profile1.sav", saves)
        roots, ign = self.plan("Hades II")
        self.assertEqual(roots, [HOME + "/Saved Games/Hades II"])
        self.assertEqual(ign[roots[0]], ["GlobalSettingsWin.sjson"])

    def test_last_of_us_screen_settings(self):
        _, configs = self.split("The Last of Us Part I")
        self.assertTrue(any(c.endswith("screeninfo.cfg") for c in configs))
        roots, ign = self.plan("The Last of Us Part I")
        self.assertTrue(any(i.endswith("screeninfo.cfg") for r in roots for i in ign[r]))

    def test_fragpunk_config_dir_not_synced(self):
        saves, configs = self.split("FragPunk")
        self.assertTrue(saves and configs)
        self.assertTrue(all("/Saved/Config/" in c for c in configs))
        roots, ign = self.plan("FragPunk")
        self.assertEqual(roots, [HOME + "/AppData/Local/FragPunk/Steam/Saved/SaveGames"])
        self.assertEqual(ign[roots[0]], [])

    def test_snowbreak_settings_only(self):
        saves, configs = self.split("Snowbreak: Containment Zone")
        self.assertEqual(saves, [])
        self.assertTrue(configs)

    def test_config_dir_collapses(self):
        root = "C:/g"
        saves = ["C:/g/SaveGames/1.sav"]
        configs = ["C:/g/Config/Win/a.ini", "C:/g/Config/Win/b.ini", "C:/g/Config/c.ini", "C:/g/SaveGames/opts.ini"]
        self.assertEqual(g.config_ignores(root, saves, configs), ["Config", "SaveGames/opts.ini"])

    def test_ignore_line_escaping(self):
        self.assertEqual(g.ignore_line("Config", True), "/Config")
        self.assertEqual(g.ignore_line("a[1]/b*.ini", True), "/a|[1|]/b|*.ini")
        self.assertEqual(g.ignore_line("a[1]/b*.ini", False), "/a\\[1\\]/b\\*.ini")

    def test_untagged_files_count_as_saves(self):
        saves, configs = g.classify_files([HOME + "/AppData/Local/X/s.dat"], {}, HOME)
        self.assertEqual((len(saves), configs), (1, []))


class Unshare(unittest.TestCase):
    def test_split_registry(self):
        me, other = "ME", "OTHER"
        reg = {
            "save-a": {"folder_id": "save-a", "devices": [other, me]},                   # offered, not here
            "save-b": {"folder_id": "save-b", "devices": [other, me]},                   # already here
            "save-c": {"folder_id": "save-c", "devices": [other, me], "removed": True},  # unshared, still here
            "save-d": {"folder_id": "save-d", "devices": [other, me], "removed": True},  # unshared, gone here
            "save-e": {"folder_id": "save-e", "devices": [other]},                       # not for me
        }
        to_remove, to_accept = g.split_registry(reg, me, {"save-b", "save-c"})
        self.assertEqual([e["folder_id"] for e in to_remove], ["save-c"])
        self.assertEqual([e["folder_id"] for e in to_accept], ["save-a"])

    def test_active(self):
        self.assertTrue(g.active({"folder_id": "x"}))
        self.assertFalse(g.active({"folder_id": "x", "removed": True}))


LIB = "/home/deck/.local/share/Steam"
SC_ID = 3141592653  # non-Steam shortcut app ID
PFX = f"{LIB}/steamapps/compatdata/{SC_ID}/pfx"
PHOME = PFX + "/drive_c/users/steamuser"


def vdf_shortcut(idx, appid, name, exe):
    def s(t, k, v=b""):
        return bytes([t]) + k.encode() + b"\0" + v
    body = s(2, "appid", appid.to_bytes(4, "little")) if appid is not None else b""
    body += s(1, "AppName", name.encode() + b"\0") + s(1, "Exe", exe.encode() + b"\0")
    body += s(0, "tags") + b"\x08"
    return s(0, str(idx)) + body + b"\x08"


class Proton(unittest.TestCase):
    def setUp(self):
        self.env = g.prefix_env(PFX)

    def test_shortcuts_vdf(self):
        data = (b"\x00shortcuts\x00" + vdf_shortcut(0, SC_ID, "Hollow Knight", '"/games/hk.exe"')
                + vdf_shortcut(1, None, "Old", "x.exe") + b"\x08\x08")
        sc = g.shortcut_entries(data)
        self.assertEqual(sc[SC_ID], g.Shortcut("Hollow Knight", '"/games/hk.exe"', ""))
        old = [a for a, x in sc.items() if x.name == "Old"][0]
        self.assertTrue(old & 0x80000000)

    def test_prefix_of(self):
        self.assertEqual(g.proton_prefix_of(PHOME + "/AppData/x.sav", [LIB]), (SC_ID, PFX))
        self.assertIsNone(g.proton_prefix_of(PFX + "/user.reg", [LIB]))
        self.assertIsNone(g.proton_prefix_of("/home/deck/.local/share/Game/x", [LIB]))

    def test_windows_entry_maps_into_prefix(self):
        self.assertEqual(g.from_portable("<home>/AppData/LocalLow/Team Cherry/Hollow Knight", self.env),
                         PHOME + "/AppData/LocalLow/Team Cherry/Hollow Knight")
        self.assertEqual(g.from_portable("C:/ProgramData/Game", self.env), PFX + "/drive_c/ProgramData/Game")
        self.assertIsNone(g.from_portable("D:/Games/Foo/save", self.env))

    def test_prefix_path_to_portable(self):
        self.assertEqual(g.to_portable(PHOME + "/Saved Games/Hades II", self.env), "<home>/Saved Games/Hades II")
        self.assertEqual(g.to_portable(PFX + "/drive_c/ProgramData/Game", self.env), "C:/ProgramData/Game")

    def test_roots_in_prefix(self):
        files = [PHOME + "/AppData/LocalLow/Team Cherry/Hollow Knight/user1.dat",
                 PHOME + "/AppData/LocalLow/Team Cherry/Hollow Knight/user2.dat"]
        roots, skipped, problems = g.compute_roots(
            files, ["<winLocalAppDataLow>/Team Cherry/Hollow Knight/*.dat"], self.env, [])
        self.assertEqual(roots, [PHOME + "/AppData/LocalLow/Team Cherry/Hollow Knight"])
        self.assertEqual((skipped, problems), ([], []))

    def test_file_directly_in_prefix_broad_dir(self):
        roots, _, problems = g.compute_roots([PHOME + "/Documents/save.dat"], [], self.env, [])
        self.assertEqual((roots, problems), ([], [PHOME + "/Documents/save.dat"]))

    def test_settings_match_case_insensitively(self):
        entry = {"files": {"<winDocuments>/My Games/X/Config": {"tags": ["config"]},
                           "<winDocuments>/My Games/X/Saves": {"tags": ["save"]}}}
        files = [PHOME + "/documents/my games/X/config/a.ini", PHOME + "/documents/my games/X/Saves/1.sav"]
        saves, configs = g.classify_files(files, entry, self.env)
        self.assertEqual(configs, [files[0]])

    def test_pick_files(self):
        other = f"{LIB}/steamapps/compatdata/570/pfx/drive_c/users/steamuser/AppData/x"
        sc = {SC_ID: g.Shortcut("Game")}
        env, files, reason = g.pick_proton_files([PHOME + "/AppData/Local/G/a", PFX + "/user.reg"], [LIB], sc)
        self.assertEqual((env, files, reason), (self.env, [PHOME + "/AppData/Local/G/a"], None))
        steam_pfx = f"{LIB}/steamapps/compatdata/570/pfx"
        self.assertEqual(g.pick_proton_files([other], [LIB], sc), (g.prefix_env(steam_pfx), [other], None))
        self.assertEqual(g.pick_proton_files([PFX + "/user.reg"], [LIB], sc)[2], "registry-only saves, cannot sync")
        self.assertIn("not in a Proton prefix", g.pick_proton_files(["/home/deck/.local/share/G/a"], [LIB], sc)[2])

    def test_shortcut_keys(self):
        sc = g.Shortcut("HK", '"/home/deck/Games/Hollow Knight/hollow_knight.exe"', '"/home/deck/Games/Hollow Knight/"')
        self.assertTrue({"hk", "hollowknight", "games"} <= sc.keys())

    @unittest.skipIf(g.IS_WINDOWS, "Linux paths")
    def test_match_shortcut(self):
        with tempfile.TemporaryDirectory() as d:
            for a in (1, 2, 3):
                (Path(d) / f"steamapps/compatdata/{a}/pfx/drive_c/users/steamuser").mkdir(parents=True)
            sc = {1: g.Shortcut("hollow_knight.exe", "/x/hollow_knight.exe"),
                  2: g.Shortcut("Game Two", "/g/HadesII/Hades2.exe"),
                  3: g.Shortcut("Misc", "/g/stuff/start.exe")}
            hk = {"game": "Hollow Knight", "portable_path": "<home>/AppData/LocalLow/Team Cherry/Hollow Knight"}
            self.assertEqual(g.match_shortcut(hk, sc, [d]), 1)  # exe name
            hades = {"game": "Hades II", "portable_path": "<home>/Saved Games/Hades II"}
            self.assertEqual(g.match_shortcut(hades, sc, [d]), 2)  # folder on the exe path
            cel = {"game": "Celeste", "portable_path": "<home>/AppData/Local/Celeste"}
            self.assertIsNone(g.match_shortcut(cel, sc, [d]))
            self.assertEqual(g.match_shortcut(cel, sc, [d], lambda: ["stuff"]), 3)  # manifest installDir
            (Path(d) / "steamapps/compatdata/3/pfx/drive_c/users/steamuser/appdata/local/celeste").mkdir(parents=True)
            self.assertEqual(g.match_shortcut(cel, sc, [d]), 3)  # save dir already there

    def test_choose_prefix_steam_game(self):
        import contextlib
        import io
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
            (Path(d) / "steamapps/compatdata/570/pfx/drive_c/users/steamuser").mkdir(parents=True)
            (Path(d) / "steamapps/appmanifest_570.acf").write_text("x")
            ctx = SimpleNamespace(cfg={}, cfg_dir=Path(d))
            e = {"game": "Dota 2", "portable_path": "<home>/x", "steam_ids": [570]}
            env = g.choose_prefix(ctx, e, {}, [d], True)
            self.assertEqual(env, g.prefix_env(g.norm(d) + "/steamapps/compatdata/570/pfx"))
            self.assertEqual(ctx.cfg["prefix_map"], {"Dota 2": 570})
            # old registry entry without steam_ids: ids come from the manifest
            ctx.cfg = {}
            del e["steam_ids"]
            self.assertIsNotNone(g.choose_prefix(ctx, e, {}, [d], True, steam_ids=lambda: {570}))
            ctx.cfg = {}
            self.assertIsNone(g.choose_prefix(ctx, e, {}, [d], True))  # not in Steam, no shortcuts

    @unittest.skipIf(g.IS_WINDOWS, "Linux paths")
    def test_resolve_ci(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "AppData" / "LocalLow" / "team cherry").mkdir(parents=True)
            self.assertEqual(g.resolve_ci(d + "/appdata/LocalLow/Team Cherry/Hollow Knight"),
                             g.norm(d) + "/AppData/LocalLow/team cherry/Hollow Knight")

    def test_find_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(g.find_prefix(SC_ID, [d]))
            (Path(d) / f"steamapps/compatdata/{SC_ID}/pfx/drive_c/users/steamuser").mkdir(parents=True)
            self.assertEqual(g.find_prefix(SC_ID, [d]), g.norm(d) + f"/steamapps/compatdata/{SC_ID}/pfx")


class Config(unittest.TestCase):
    def test_ludusavi_cmd(self):
        self.assertEqual(g.ludusavi_cmd({"ludusavi": ["flatpak", "run", "x"]}), ["flatpak", "run", "x"])
        self.assertEqual(g.ludusavi_cmd({"ludusavi": "C:/Tools/ludusavi.exe"}), ["C:/Tools/ludusavi.exe"])


class Menu(unittest.TestCase):
    def run_menu(self, inputs):
        import contextlib
        import io
        from unittest import mock
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("builtins.input", side_effect=list(inputs)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = g.main(["--config-dir", d])
        return code, out.getvalue(), err.getvalue()

    def test_menu_runs_command_and_returns(self):
        # 9 = invalid, 5 = status (fails: not configured), Enter, 0 = exit
        code, out, err = self.run_menu(["9", "5", "", "0"])
        self.assertEqual(code, 0)
        self.assertIn("not set up yet", out)
        self.assertIn("Invalid choice", out)
        self.assertIn("--- status ---", out)
        self.assertIn("Server is not configured", err)
        self.assertEqual(out.count("=== Savething"), 3)  # menu shown again after each step

    def test_menu_exit_on_eof(self):
        code, out, _ = self.run_menu([EOFError()])
        self.assertEqual(code, 0)

    def test_selection_commands(self):
        import contextlib
        import io
        from unittest import mock
        with mock.patch("builtins.input", side_effect=["ShowAll", "showall", "9", "2"]), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(g.ask_selection("> ", 3, commands=("showall",)), "showall")
            self.assertEqual(g.ask_selection("> ", 3), [1])  # not a command here: invalid, then 9 out of range


if __name__ == "__main__":
    unittest.main()

"""The site assembler: phase selection, the publication manifest and the isolated
generated directories. Run with python -m unittest leaderboard.test_site."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from leaderboard import build, build_macro, site
from leaderboard import run as micro
from leaderboard.test_macro import CAMPAIGN, SOURCE, attempt_payload, campaign_record, evidence_summary, good_evidence, outcome
from leaderboard.validate import BOARD, SCHEMA_VERSION, sha256
from leaderboard.validate_macro import SCENARIOS

FULL = {**CAMPAIGN, "preset": "full", "id": "0000f0ff", "started": "2026-10-11T10:00:00+00:00"}


class Phase(unittest.TestCase):
    def test_default_env_cli_and_invalid(self):
        self.assertEqual(site.phase(env={}), "prod", "production is the default")
        self.assertEqual(site.phase(env={"LEADERBOARD_PHASE": "local"}), "local")
        self.assertEqual(site.phase(env={"LEADERBOARD_PHASE": "alpha"}), "alpha")
        self.assertEqual(site.phase("local", env={"LEADERBOARD_PHASE": "prod"}), "local", "the CLI wins over the environment")
        self.assertEqual(site.phase("alpha", env={"LEADERBOARD_PHASE": "local"}), "alpha", "the CLI wins over the environment")
        self.assertEqual(site.phase(None, env={"LEADERBOARD_PHASE": ""}), "prod", "an empty variable is the default")
        for bad in ("production", "dev", "LOCAL", "ALPHA", "staging"):
            with self.subTest(bad=bad), self.assertRaisesRegex(site.SiteError, "must be prod, alpha or local, not"):
                site.phase(env={"LEADERBOARD_PHASE": bad})
        with self.assertRaisesRegex(site.SiteError, "'staging'"):
            site.phase("staging")

    def test_site_metadata_never_dirties_a_run(self):
        """The generated directory, the legacy generated data files and the manifest are
        website outputs or configuration: none is in the source snapshot, so promoting
        a run cannot change its identity or comparison group."""
        outputs = ("/.site/", "/publish.json", "leaderboard/data.js", "leaderboard/macro-data.js")
        snapshot = micro.source_snapshot(micro.CODE_PATHS)
        self.assertFalse(any(o in f for f in snapshot["files"] for o in outputs), [f for f in snapshot["files"] if any(o in f for o in outputs)])
        self.assertFalse(any(o.strip("/") in line for line in snapshot["status"].splitlines() for o in outputs))
        self.assertTrue(any(f.endswith("leaderboard/site.py") for f in snapshot["files"]), "the assembler's code is still an input")


class Board(unittest.TestCase):
    """A scratch board with every system folder, suite.json, the mocks and helpers for both lanes."""

    def setUp(self):
        self.board = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.board)
        shutil.copy(BOARD / "suite.json", self.board)
        for system in ("dolt", "dolt_mysql", "neon", "tiger", "xata"):
            shutil.copytree(BOARD / system, self.board / system, ignore=shutil.ignore_patterns("results", "macro", "__pycache__"))
        shutil.copytree(BOARD / "mock", self.board / "mock")
        self.suite = json.loads((BOARD / "suite.json").read_text())
        for name in site.PAGES["local"]:
            (self.board / name).parent.mkdir(exist_ok=True)
            (self.board / name).write_text(f"<!-- {name} -->")
        (self.board / "workflow-diagrams").mkdir()
        for rel in site.WORKFLOW_DIAGRAMS:
            shutil.copy(BOARD / rel, self.board / rel)  # the real rendered maps: binary assets the site must carry byte for byte
        self.site = self.board / ".site"
        self.manifest = self.board / "publish.json"
        self.publish({"micro": [], "macro": []})
        self.day = "20261011"

    def publish(self, manifest) -> None:
        self.manifest.write_text(json.dumps(manifest))

    def payload(self, date: str = "2026-09-30", system: str = "dolt", **changes) -> dict:
        meta = json.loads((BOARD / system / "system.json").read_text())
        return {"schema_version": SCHEMA_VERSION, "run_id": "0123abcd", **meta, "date": date, "machine": "box",
                "version": "0.54.10", "suite": self.suite["suite"], "suite_sha256": sha256(BOARD / "suite.json"),
                "seed": self.suite["seed"], "commit": "0" * 40, "dirty": False,
                "dataset_sha256": self.suite["dataset"]["sha256"], "python": "3.13.14", "lock_sha256": "0" * 64,
                "provision_time": 0.07, "load_time": 2.3,
                "result": [[0.01] * self.suite["tries"] for _ in self.suite["rows"]], **changes}

    def write(self, payload: dict, day_dir: str = "20260930", system: str = "dolt") -> Path:
        path = self.board / system / "results" / day_dir / "box.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path

    def write_macro(self, payload: dict, system: str = "dolt_mysql", day: str = "20261011") -> str:
        stem = f"campaign_{payload['id']}" if "id" in payload else payload["attempt_id"]
        path = self.board / system / "macro" / day / f"{stem}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
        return path.relative_to(self.board).as_posix()

    def write_campaign(self, campaign: dict, ids: str = "1", broken: dict | None = None, **record) -> str:
        """A campaign with one attempt per scenario; `broken` maps a scenario to a non-verified outcome."""
        attempts, day = {}, campaign["started"][:10].replace("-", "")
        for i, key in enumerate(campaign["scenarios"]):
            attempt_id = f"bb_{ids}000000{i}"
            attempts[key] = attempt_id
            changes = {}
            if key in (broken or {}):
                changes = {"outcome": outcome("failed", ["2 of 8 work units failed"], 3, 1.5),
                           "evidence": evidence_summary(good_evidence(run_id=attempt_id, scenario=key, elapsed_sec=3, work={"expected": 8, "succeeded": 6, "failed": 2, "skipped": 0}))}
            self.write_macro(attempt_payload(attempt_id=attempt_id, scenario=key, campaign=campaign, date=campaign["started"][:10], **changes), day=day)
        return self.write_macro(campaign_record(**campaign, attempts=attempts, **record), day=day)

    def assemble(self, phase: str) -> Path:
        return site.assemble(phase, self.board)

    @staticmethod
    def read(target: Path) -> dict:
        """Every const of both data files as JSON."""
        out = {}
        for name in ("data.js", "macro-data.js"):
            for chunk in (target / name).read_text().split(";\nconst ")[0:]:
                key, _, value = chunk.removeprefix("const ").partition(" = ")
                out[key.strip()] = json.loads(value.rstrip(";\n"))
        return out

    @staticmethod
    def files(target: Path) -> list[str]:
        return sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file())


class Local(Board):
    def test_local_carries_everything_and_prod_carries_nothing_unpublished(self):
        self.write(self.payload())
        self.write_campaign(dict(CAMPAIGN))
        local = self.assemble("local")
        data = self.read(local)
        self.assertEqual(data["site"], {"phase": "local"})
        self.assertEqual([(e["system"], "source" in e) for e in data["data"]], [("Doltgres", True), ("Neon", False), ("Tiger Cloud", False), ("Xata", False)], "mock rows follow the real ones and link to no record")
        self.assertFalse([k for e in data["data"] for k in ("mock", "basis", "note") if k in e], "a mock row looks like any other row")
        self.assertEqual([c["preset"] for c in data["macroCampaigns"]], ["mini"], "a quick campaign is a result on the machine that ran it")
        self.assertIn("run.html", self.files(local)); self.assertIn("README.md", self.files(local)); self.assertNotIn("mock/neon.json", self.files(local))
        self.assertIn("dolt/results/20260930/box.json", self.files(local))
        self.assertIn("dolt_mysql/macro/20261010/bb_10000000.json", self.files(local))
        prod = self.assemble("prod")
        data = self.read(prod)
        self.assertEqual(data["site"], {"phase": "prod"})
        self.assertEqual((data["data"], data["excluded"], data["macroCampaigns"], data["macroExcluded"]), ([], [], [], []), "an empty manifest publishes nothing")
        self.assertEqual(self.files(prod), sorted(["data.js", "index.html", "macro-data.js", "styles.css", *site.SCRIPTS, *site.WORKFLOW_DIAGRAMS]), "no wizard, README, mock or raw record")
        self.assertEqual(sorted(p.name for p in self.site.iterdir()), ["local", "prod"], "the two phases are isolated directories")
        self.assertTrue((local / "run.html").exists(), "building prod did not touch local")
        alpha = self.assemble("alpha")
        alpha_data = self.read(alpha)
        self.assertEqual(alpha_data["site"], {"phase": "alpha"})
        self.assertEqual([(e["system"], "source" in e) for e in alpha_data["data"]], [("Doltgres", True)], "alpha carries real validated runs without mock rows")
        self.assertEqual([c["preset"] for c in alpha_data["macroCampaigns"]], ["mini"], "alpha carries validated mini and full campaigns without requiring publish.json")
        self.assertNotIn("run.html", self.files(alpha), "alpha omits the macOS-only Terminal launcher")
        self.assertIn("architecture.html", self.files(alpha))
        self.assertIn("README.md", self.files(alpha))
        self.assertIn("dolt/results/20260930/box.json", self.files(alpha))
        self.assertIn("dolt_mysql/macro/20261010/bb_10000000.json", self.files(alpha))
        self.assertEqual(sorted(p.name for p in self.site.iterdir()), ["alpha", "local", "prod"], "all three phases are isolated directories")

    def test_generated_directory_is_ignored_by_git(self):
        self.assertIn(".site/", (BOARD / ".gitignore").read_text().split())

    def test_the_page_loads_exactly_the_published_scripts_in_order(self):
        """A script the page loads but the site leaves out would 404 in production; the order is the load order."""
        self.assertEqual(tuple(re.findall(r'<script src="(js/[^"]+)"></script>', (BOARD / "index.html").read_text())), site.SCRIPTS)

    def test_both_phases_carry_exactly_the_six_workflow_diagrams(self):
        """The PNGs are copied byte for byte; the HTML they are rendered from and anything
        else dropped into the folder stay out, in both phases."""
        self.assertEqual(len(site.WORKFLOW_DIAGRAMS), 6)
        self.assertEqual([rel.rsplit("/", 1)[1] for rel in site.WORKFLOW_DIAGRAMS], [f"{key}.png" for key in SCENARIOS], "one map per scenario, in suite order")
        (self.board / "workflow-diagrams" / "source.html").write_text("<!doctype html>")
        (self.board / "workflow-diagrams" / "draft.png").write_bytes(b"\x89PNG stray")
        (self.board / "workflow-diagrams" / "rl_env.svg").write_text("<svg/>")
        self.write(self.payload())
        for phase in ("prod", "alpha", "local"):
            target = self.assemble(phase)
            files = self.files(target)
            for rel in site.WORKFLOW_DIAGRAMS:
                self.assertIn(rel, files, phase)
                self.assertEqual((target / rel).read_bytes(), (BOARD / rel).read_bytes(), f"{phase}: {rel} is the real rendering")
                self.assertEqual((target / rel).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual([f for f in files if f.startswith("workflow-diagrams/")], sorted(site.WORKFLOW_DIAGRAMS), f"{phase}: nothing else from the folder is published")

    def test_missing_or_symlinked_workflow_diagram_fails_closed(self):
        self.write(self.payload())
        self.publish({"micro": ["dolt/results/20260930/box.json"], "macro": []})
        prod = self.assemble("prod")
        before = self.files(prod)
        png = self.board / "workflow-diagrams" / "ops_agent.png"
        aside = self.board / "ops_agent-copy.png"; shutil.copy(png, aside); self.addCleanup(aside.unlink)
        png.unlink()
        with self.assertRaisesRegex(site.SiteError, "workflow-diagrams/ops_agent.png: not a regular file"):
            self.assemble("prod")
        png.symlink_to(aside)
        with self.assertRaisesRegex(site.SiteError, "ops_agent.png is a symlink"):
            self.assemble("local")
        png.unlink(); shutil.copy(aside, png)
        folder = self.board / "workflow-diagrams"; real = self.board / "maps-elsewhere"
        folder.rename(real); folder.symlink_to(real, target_is_directory=True)
        with self.assertRaisesRegex(site.SiteError, "workflow-diagrams is a symlink"):
            self.assemble("prod")
        self.assertEqual(self.files(prod), before, "the previous site is intact")
        self.assertEqual([p.name for p in self.site.iterdir()], ["prod"], "no staging directory or local site is left behind")

    def test_output_is_only_the_canonical_generated_directory(self):
        self.write(self.payload())
        local = self.assemble("local")
        self.assertEqual(local, self.board.resolve() / ".site" / "local")
        with self.assertRaisesRegex(TypeError, "positional"):
            site.assemble("local", self.board, self.board / "elsewhere")  # no other destination can be named
        stale = local / "dolt" / "results" / "20200101" / "old.json"
        stale.parent.mkdir(parents=True); stale.write_text("{}")
        self.assemble("local")
        self.assertFalse(stale.exists(), "a stale generated file never survives a rebuild")
        with self.assertRaisesRegex(site.SiteError, "unknown phase"):
            self.assemble("staging")

    def test_symlinked_site_or_target_is_refused_and_nothing_behind_it_removed(self):
        self.write(self.payload())
        elsewhere = self.board / "valuable"; (elsewhere / "prod").mkdir(parents=True)
        precious = elsewhere / "prod" / "precious.csv"; precious.write_text("KEEP_ME")
        self.site.symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaisesRegex(site.SiteError, "is a symlink"):
            self.assemble("prod")
        self.assertEqual(precious.read_text(), "KEEP_ME")
        self.site.unlink(); self.site.mkdir()
        (self.site / "prod").symlink_to(elsewhere / "prod", target_is_directory=True)
        with self.assertRaisesRegex(site.SiteError, "is a symlink"):
            self.assemble("prod")
        self.assertEqual(precious.read_text(), "KEEP_ME")
        (self.site / "prod").unlink(); (self.site / "prod").write_text("not a directory")
        with self.assertRaisesRegex(site.SiteError, "not a directory"):
            self.assemble("prod")

    def test_previous_output_survives_a_failed_build(self):
        self.write(self.payload())
        self.publish({"micro": ["dolt/results/20260930/box.json"], "macro": []})
        prod = self.assemble("prod")
        before = self.files(prod)
        self.assertIn("dolt/results/20260930/box.json", before)
        self.publish({"micro": ["dolt/results/20260930/missing.json"], "macro": []})
        with self.assertRaisesRegex(site.SiteError, "not a regular file"):
            self.assemble("prod")
        self.assertEqual(self.files(prod), before, "the complete previous site is still there")
        self.assertEqual([p.name for p in self.site.iterdir()], ["prod"], "no staging directory is left behind")
        (self.board / "index.html").unlink()
        with self.assertRaisesRegex(site.SiteError, "index.html: not a regular file"):
            self.assemble("local")
        self.assertFalse((self.site / "local").exists())
        self.assertEqual([p.name for p in self.site.iterdir()], ["prod"])
        (self.board / "index.html").write_text("<!-- index.html -->")
        self.publish({"micro": ["dolt/results/20260930/box.json"], "macro": []})
        real, injected = Path.rename, []
        def failing_swap(path, destination):
            # Fail exactly the install of the staging directory, once; the rollback rename of the backup must succeed.
            if not injected and destination == prod and path.name.startswith(".prod-") and not path.name.startswith(".old-"):
                injected.append(path)
                raise OSError("injected swap failure")
            return real(path, destination)
        with mock.patch.object(Path, "rename", failing_swap), self.assertRaisesRegex(OSError, "injected"):
            self.assemble("prod")
        self.assertEqual(len(injected), 1, "the injected failure hit the staging install, not the rollback")
        self.assertEqual(self.files(prod), before, "a failed installation puts the previous site back at its path")
        self.assertEqual([p.name for p in self.site.iterdir()], ["prod"], "no staging or backup directory is left behind")

    def test_system_sidecars_must_be_regular_files(self):
        self.write(self.payload())
        self.publish({"micro": ["dolt/results/20260930/box.json"], "macro": []})
        meta = self.board / "dolt" / "system.json"; meta.unlink(); meta.symlink_to(BOARD / "dolt" / "system.json")
        for phase in ("prod", "alpha", "local"):
            with self.assertRaisesRegex(site.SiteError, "dolt/system.json is a symlink"):
                self.assemble(phase)
        self.assertFalse(self.site.exists() and any(self.site.iterdir()))

    def test_pages_suite_and_raw_files_must_be_regular_files(self):
        secret = self.board.parent / "secret.env"; secret.write_text("PRIVATE"); self.addCleanup(secret.unlink)
        (self.board / "index.html").unlink(); (self.board / "index.html").symlink_to(secret)
        with self.assertRaisesRegex(site.SiteError, "index.html is a symlink"):
            self.assemble("prod")
        (self.board / "index.html").unlink(); (self.board / "index.html").write_text("<!-- index.html -->")
        (self.board / "suite.json").unlink(); (self.board / "suite.json").symlink_to(secret)
        with self.assertRaisesRegex(site.SiteError, "suite.json is a symlink"):
            self.assemble("local")
        (self.board / "suite.json").unlink(); (self.board / "suite.json").write_text(json.dumps({"suite": "x"}))
        with self.assertRaisesRegex(site.SiteError, "suite.json must hold"):
            self.assemble("local")
        (self.board / "suite.json").write_text("[]")
        with self.assertRaisesRegex(site.SiteError, "must be a JSON object"):
            self.assemble("local")
        shutil.copy(BOARD / "suite.json", self.board)
        real = self.write(self.payload())
        link = self.board / "dolt" / "results" / "20261001" / "box.json"; link.parent.mkdir(); link.symlink_to(real)
        with self.assertRaisesRegex(site.SiteError, "box.json is a symlink"):
            self.assemble("local")
        link.unlink(); link.parent.rmdir()
        campaign = dict(FULL)
        rel = self.write_campaign(campaign)
        attempt = self.board / "dolt_mysql" / "macro" / self.day / "bb_10000000.json"
        alias = self.board / "dolt_mysql" / "macro" / "20261012" / "bb_10000000.json"; alias.parent.mkdir()
        alias.symlink_to(attempt)  # resolves to the valid file, so the builder sees it as a second, conflicting attempt
        self.publish({"micro": [], "macro": [rel]})
        with self.assertRaisesRegex(site.SiteError, "2 attempts in one campaign"):
            self.assemble("prod")
        self.assertFalse((self.site / "prod").exists(), "nothing is published when a selected campaign's file is a symlink")
        local = self.assemble("local")
        self.assertIn("2 attempts in one campaign", self.read(local)["macroExcluded"][0]["reason"])
        self.assertNotIn("dolt_mysql/macro/20261012/bb_10000000.json", self.files(local), "a file behind a symlink is never copied")


class Manifest(Board):
    def test_shape_and_path_syntax(self):
        for bad in ("[]", "{}", '{"micro": [], "macro": [], "extra": []}', '{"micro": {}, "macro": []}', '{"micro": [1], "macro": []}', "not json"):
            with self.subTest(bad=bad):
                self.manifest.write_text(bad)
                with self.assertRaises(site.SiteError):
                    self.assemble("prod")
        self.manifest.unlink()
        with self.assertRaisesRegex(site.SiteError, "publish.json: not a regular file"):
            self.assemble("prod")
        aside = self.board.parent / "selected.json"; aside.write_text('{"micro": [], "macro": []}'); self.addCleanup(aside.unlink)
        self.manifest.symlink_to(aside)
        with self.assertRaisesRegex(site.SiteError, "publish.json is a symlink"):
            self.assemble("prod")
        self.manifest.unlink(); self.publish({"micro": [], "macro": []})
        for kind, bad in (("micro", "../suite.json"), ("micro", "/etc/passwd"), ("micro", "dolt/results/20260930/../../../x.json"),
                          ("micro", "dolt/macro/20261010/campaign_0000f0ff.json"), ("micro", "mock/neon.json"), ("micro", "dolt/results/20260930/box.txt"),
                          ("macro", "dolt_mysql/macro/20261011/bb_10000000.json"), ("macro", "dolt_mysql/results/20260930/box.json"),
                          ("macro", "dolt_mysql\\macro\\20261011\\campaign_0000f0ff.json"), ("macro", "Dolt/macro/20261011/campaign_0000f0ff.json")):
            with self.subTest(kind=kind, bad=bad):
                self.publish({"micro": [], "macro": [], kind: [bad]})
                with self.assertRaisesRegex(site.SiteError, "is not a relative"):
                    self.assemble("prod")
        self.publish({"micro": ["dolt/results/20260930/box.json", "dolt/results/20260930/box.json"], "macro": []})
        with self.assertRaisesRegex(site.SiteError, "twice"):
            self.assemble("prod")

    def test_entries_must_be_regular_files_under_the_board(self):
        self.publish({"micro": ["dolt/results/20260930/box.json"], "macro": []})
        with self.assertRaisesRegex(site.SiteError, "not a regular file"):
            self.assemble("prod")
        outside = Path(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, outside)
        (outside / "box.json").write_text(json.dumps(self.payload()))
        target = self.board / "dolt" / "results" / "20260930" / "box.json"; target.parent.mkdir(parents=True)
        target.symlink_to(outside / "box.json")
        with self.assertRaisesRegex(site.SiteError, "box.json is a symlink"):
            self.assemble("prod")
        target.unlink(); target.parent.rmdir()
        (self.board / "dolt" / "results" / "20260930").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(site.SiteError, "20260930 is a symlink"):
            self.assemble("prod")

    def test_micro_selection_competes_only_among_selected_records(self):
        """An unpublished newer run never supersedes the official record; among selected
        records the newest wins, so a newer selected error record is shown and the older
        selected success is omitted; an invalid selected record fails closed."""
        official = "dolt/results/20260930/box.json"
        self.write(self.payload())
        self.publish({"micro": [official], "macro": []})
        data = self.read(self.assemble("prod"))
        self.assertEqual([e["source"] for e in data["data"]], [official])
        self.assertTrue(all("source" in e for e in data["data"]), "no mock row stands in for an unpublished system")
        self.assertEqual(self.files(self.site / "prod"), sorted(["data.js", official, "index.html", "macro-data.js", "styles.css", *site.SCRIPTS, *site.WORKFLOW_DIAGRAMS]))
        for newer in ({"error": "newer unpublished failure"}, self.payload(date="2026-10-01")):
            self.write(newer, day_dir="20261001")
            data = self.read(self.assemble("prod"))
            self.assertEqual(([e["source"] for e in data["data"]], data["excluded"]), ([official], []), "an unselected newer file changes nothing")
            self.assertNotIn("dolt/results/20261001/box.json", self.files(self.site / "prod"))
        self.assertEqual(len(site.everything(self.board)[0]), 1, "development still shows the newest file")
        self.write({"error": "server did not start"}, day_dir="20261002")
        self.publish({"micro": [official, "dolt/results/20261002/box.json"], "macro": []})
        data = self.read(self.assemble("prod"))
        self.assertEqual(data["data"], [], "the older selected success is superseded by the newer selected error record")
        self.assertEqual([(x["source"], x["reason"]) for x in data["excluded"]], [("dolt/results/20261002/box.json", "server did not start")], "a published failure stays visible")
        self.assertNotIn(official, self.files(self.site / "prod")); self.assertIn("dolt/results/20261002/box.json", self.files(self.site / "prod"))
        self.write(self.payload(date="2026-10-03", dirty=True), day_dir="20261003")
        self.publish({"micro": ["dolt/results/20261003/box.json"], "macro": []})
        with self.assertRaisesRegex(site.SiteError, "uncommitted changes; an invalid record is never published"):
            self.assemble("prod")
        self.publish({"micro": [official, "dolt/results/20261003/box.json"], "macro": []})
        with self.assertRaisesRegex(site.SiteError, "never published"):
            self.assemble("prod")
        (self.board / "dolt" / "results" / "20260930" / "box.json").write_text("PRIVATE_INVALID_OLDER")
        self.publish({"micro": [official, "dolt/results/20261002/box.json"], "macro": []})
        with self.assertRaisesRegex(site.SiteError, "dolt/results/20260930/box.json: invalid: .*never published"):
            self.assemble("prod")
        self.assertFalse((self.site / "prod" / official).exists(), "an invalid older selected file is validated too and never copied")

    def test_build_source_filter_is_opt_in(self):
        self.write(self.payload())
        self.write({"error": "later"}, day_dir="20261001")
        entries, excluded = build.build(self.board)
        self.assertEqual(([e["source"] for e in entries], [x["source"] for x in excluded]), ([], ["dolt/results/20261001/box.json"]), "the build carries real results only")
        entries, excluded = build.build(self.board, sources={"dolt/results/20260930/box.json"})
        self.assertEqual(([e["source"] for e in entries], excluded), (["dolt/results/20260930/box.json"], []))

    def test_macro_rejects_development_campaigns(self):
        mini = self.write_campaign(dict(CAMPAIGN))
        self.publish({"micro": [], "macro": [mini]})
        with self.assertRaisesRegex(site.SiteError, "a mini campaign stays on the machine that ran it"):
            self.assemble("prod")
        dirty = {**FULL, "id": "0000d1ff", "started": "2026-10-12T10:00:00+00:00"}
        rel = self.write_campaign(dirty, "2", source={**SOURCE, "dirty": True})
        self.publish({"micro": [], "macro": [rel]})
        with self.assertRaisesRegex(site.SiteError, "run from uncommitted sources; a campaign that fails validation is never published"):
            self.assemble("prod")
        broken = self.write_macro({"id": "0000bad0", "record": "campaign"}, day="20261013")
        self.publish({"micro": [], "macro": [broken]})
        with self.assertRaisesRegex(site.SiteError, "invalid campaign record"):
            self.assemble("prod")
        self.publish({"micro": [], "macro": ["dolt_mysql/macro/20261014/campaign_00000000.json"]})
        with self.assertRaisesRegex(site.SiteError, "not a regular file"):
            self.assemble("prod")
        self.assertFalse((self.site / "prod").exists(), "nothing was published")

    def test_published_campaign_keeps_failed_workflows_and_a_campaign_failing_validation_is_refused(self):
        """A failed workflow is a result and is published as one; a campaign whose record fails
        validation (an abort, a duplicate attempt, an invalid named attempt) is never published."""
        campaign = dict(FULL)
        rel = self.write_campaign(campaign, broken={"ops_agent": True})
        day = "20261011"
        record = json.loads((self.board / rel).read_text())
        self.publish({"micro": [], "macro": [rel]})
        data = self.read(self.assemble("prod"))
        [c] = data["macroCampaigns"]
        self.assertEqual(c["preset"], "full")
        self.assertEqual(c["attempts"]["ops_agent"]["outcome"]["status"], "failed", "a failed attempt is published as failed")
        self.assertEqual(c["attempts"]["ops_agent"]["outcome"]["completion_sec"], None)
        self.assertEqual(data["macroExcluded"], [])
        files = self.files(self.site / "prod")
        self.assertIn(rel, files)
        self.assertIn(f"dolt_mysql/macro/{day}/{record['attempts']['ops_agent']}.json", files)
        # An unpublished campaign of the same system stays out of production; development shows the newest first.
        other = self.write_campaign({**FULL, "id": "0000aaaa", "started": "2026-10-12T10:00:00+00:00"}, "3")
        data = self.read(self.assemble("prod"))
        self.assertEqual([c["campaign_id"] for c in data["macroCampaigns"]], ["0000f0ff"])
        self.assertNotIn(other, self.files(self.site / "prod"))
        self.assertEqual(self.read(self.assemble("local"))["macroCampaigns"][0]["campaign_id"], "0000aaaa")
        # Each validation failure alone refuses the publication and names its reason.
        (self.board / "dolt_mysql" / "macro" / day / f"{record['attempts']['data_agent']}.json").write_text('{"password": "hunter2"}')
        with self.assertRaisesRegex(site.SiteError, "evidence missing or unreadable: S6 Data agent"):
            self.assemble("prod")
        self.write_macro(attempt_payload(attempt_id="bb_1000ffff", scenario="multi_agent", campaign=campaign, date="2026-10-11"), day=day)
        with self.assertRaisesRegex(site.SiteError, "2 attempts in one campaign"):
            self.assemble("prod")
        self.write_macro(campaign_record(**campaign, attempts=record["attempts"], aborted="stopped after S6 Data agent: cleanup failed"), day=day)
        with self.assertRaisesRegex(site.SiteError, "campaign aborted"):
            self.assemble("prod")
        self.assertFalse(any(b"hunter2" in (self.site / "prod" / f).read_bytes() for f in self.files(self.site / "prod")), "a refused build leaves the previous site")

    def test_only_selected_campaign_records_are_heads(self):
        """An unselected record of the same campaign at another date neither blocks nor
        replaces the selected head; its attempts are still ingested; selecting a
        campaign twice fails clearly."""
        campaign = dict(FULL)
        rel = self.write_campaign(campaign, broken={"ops_agent": True})
        self.publish({"micro": [], "macro": [rel]})
        [c] = self.read(self.assemble("prod"))["macroCampaigns"]
        self.assertEqual(c["attempts"]["ops_agent"]["outcome"]["status"], "failed")
        twin = self.write_macro(json.loads((self.board / rel).read_text()), day="20261012")  # identical head, unselected
        [c] = self.read(self.assemble("prod"))["macroCampaigns"]
        self.assertEqual((c["source"], sorted(c["attempts"])), (rel, sorted(SCENARIOS)), "the selected head is published with every attempt")
        self.assertNotIn(twin, self.files(self.site / "prod"))
        self.publish({"micro": [], "macro": [rel, twin]})
        with self.assertRaisesRegex(site.SiteError, "selected more than once|also selected at"):
            self.assemble("prod")
        self.assertEqual(len(self.read(self.assemble("local"))["macroCampaigns"]), 1, "the default build is unchanged: the newest record of a campaign wins")
        self.publish({"micro": [], "macro": [rel]})
        self.write_macro(attempt_payload(attempt_id="bb_1000ffff", scenario="rl_env", campaign=campaign, date="2026-10-11"), day=self.day)
        with self.assertRaisesRegex(site.SiteError, "2 attempts in one campaign"):
            self.assemble("prod")  # every attempt file is still ingested, so a conflict is still a conflict

    def test_macro_source_filter_is_opt_in(self):
        campaign = dict(FULL)
        rel = self.write_campaign(campaign)
        twin = self.write_macro(json.loads((self.board / rel).read_text()), day="20261012")
        campaigns, excluded = build_macro.build(self.board)
        self.assertEqual(([c["source"] for c in campaigns], excluded), ([twin], []), "by default the newest record of a campaign is its head")
        campaigns, excluded = build_macro.build(self.board, sources={rel})
        self.assertEqual(([c["source"] for c in campaigns], excluded), ([rel], []))
        campaigns, excluded = build_macro.build(self.board, sources={rel, twin})
        self.assertEqual(campaigns, [])
        self.assertTrue({rel, twin} <= {x["source"] for x in excluded}, "both selected heads are reported, and their attempt is then an orphan")
        self.assertTrue(all("selected" in x["reason"] for x in excluded if x["source"] in (rel, twin)))

    def test_changed_during_run_is_never_published(self):
        rel = self.write_campaign(dict(FULL), source={**SOURCE, "changed_during_run": True})
        self.publish({"micro": [], "macro": [rel]})
        with self.assertRaisesRegex(site.SiteError, "sources changed during the run"):
            self.assemble("prod")

    def test_mixed_micro_or_macro_provenance_is_rejected(self):
        self.write(self.payload(), system="dolt")
        for field, value in (("commit", "f" * 40), ("lock_sha256", "e" * 64), ("python", "3.12.0")):
            with self.subTest(field=field):
                self.write(self.payload(system="dolt_mysql", **{field: value}), system="dolt_mysql")
                self.publish({"micro": ["dolt/results/20260930/box.json", "dolt_mysql/results/20260930/box.json"], "macro": []})
                with self.assertRaisesRegex(site.SiteError, f"mix different {field}"):
                    self.assemble("prod")
        other_machine = self.board / "dolt_mysql" / "results" / "20260930" / "other.json"
        other_machine.write_text(json.dumps(self.payload(system="dolt_mysql", machine="other")))
        self.publish({"micro": ["dolt/results/20260930/box.json", "dolt_mysql/results/20260930/other.json"], "macro": []})
        with self.assertRaisesRegex(site.SiteError, "mix different machine labels"):
            self.assemble("prod")
        c1 = self.write_campaign(dict(FULL), "1")
        c2_meta = {**FULL, "id": "0000f0fe"}
        c2_attempts = {}
        for i, key in enumerate(c2_meta["scenarios"]):
            aid = f"bb_2000000{i}"
            c2_attempts[key] = aid
            self.write_macro(attempt_payload(attempt_id=aid, scenario=key, campaign=c2_meta, date="2026-10-11", python="3.12.0"), day="20261011")
        c2 = self.write_macro(campaign_record(**c2_meta, attempts=c2_attempts, python="3.12.0"), day="20261011")
        self.publish({"micro": [], "macro": [c1, c2]})
        with self.assertRaisesRegex(site.SiteError, "mix different comparison groups"):
            self.assemble("prod")


class Cli(unittest.TestCase):
    def test_invalid_phase_fails_with_an_actionable_message(self):
        import subprocess, sys
        env = {**os.environ, "LEADERBOARD_PHASE": "staging"}
        process = subprocess.run([sys.executable, "-m", "leaderboard.site"], capture_output=True, text=True, env=env, cwd=BOARD.parent)
        self.assertEqual(process.returncode, 2)
        self.assertIn("must be prod, alpha or local, not 'staging'", process.stderr)
        process = subprocess.run([sys.executable, "-m", "leaderboard.site", "--phase", "dev"], capture_output=True, text=True, cwd=BOARD.parent)
        self.assertEqual(process.returncode, 2)
        self.assertIn("invalid choice", process.stderr)


class AwsAlphaLifecycle(Board):
    """Verify the collapsible personal-AWS alpha lifecycle (up, deploy-site, status, run, collapse)."""

    def setUp(self):
        super().setUp()
        from leaderboard import aws_alpha
        self.aws_alpha = aws_alpha
        self.runs = self.board / ".runs"
        self.calls: list[list[str]] = []
        self.cloud = {
            "instances": [],
            "volumes": [{"VolumeId": "vol-0alpha1", "State": "available"}],
            "sgs": [],
            "keys": [],
            "buckets": set(),
        }

    def fake_aws_runner(self, cmd: list[str], **_kwargs):
        import subprocess
        self.calls.append(cmd)
        # Strip 'aws', '--region <r>', '--profile <p>', '--output <o>'
        tokens = []
        i = 1
        while i < len(cmd):
            if cmd[i] in ("--region", "--profile", "--output"):
                i += 2
            else:
                tokens.append(cmd[i])
                i += 1
        svc, op = tokens[0], tokens[1]
        out = ""
        rc = 0
        if (svc, op) == ("sts", "get-caller-identity"):
            out = json.dumps({"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/dev"})
        elif (svc, op) == ("ssm", "get-parameter"):
            out = "ami-0ubuntu2404amd64"
        elif (svc, op) == ("ec2", "describe-instances"):
            out = json.dumps({"Reservations": [{"Instances": list(self.cloud["instances"])}]})
        elif (svc, op) == ("ec2", "describe-volumes"):
            out = json.dumps({"Volumes": list(self.cloud["volumes"])})
        elif (svc, op) == ("ec2", "describe-security-groups"):
            out = json.dumps({"SecurityGroups": list(self.cloud["sgs"])})
        elif (svc, op) == ("ec2", "describe-key-pairs"):
            out = json.dumps({"KeyPairs": list(self.cloud["keys"])})
        elif (svc, op) == ("ec2", "create-key-pair"):
            kname = tokens[tokens.index("--key-name") + 1]
            self.cloud["keys"].append({"KeyName": kname})
            out = json.dumps({"KeyName": kname, "KeyMaterial": "-----BEGIN OPENSSH PRIVATE KEY-----\nalpha\n"})
        elif (svc, op) == ("ec2", "create-security-group"):
            gname = tokens[tokens.index("--group-name") + 1]
            gid = "sg-0alpha123"
            self.cloud["sgs"].append({"GroupId": gid, "GroupName": gname})
            out = json.dumps({"GroupId": gid})
        elif (svc, op) == ("ec2", "run-instances"):
            iid = "i-0alpha999"
            itype = tokens[tokens.index("--instance-type") + 1]
            self.cloud["instances"].append(
                {
                    "InstanceId": iid,
                    "State": {"Name": "running"},
                    "InstanceType": itype,
                    "PublicIpAddress": "198.51.100.42",
                }
            )
            out = json.dumps({"Instances": [{"InstanceId": iid}]})
        elif (svc, op) == ("ec2", "terminate-instances"):
            for inst in self.cloud["instances"]:
                inst["State"] = {"Name": "terminated"}
            out = "terminated"
        elif (svc, op) == ("ec2", "delete-volume"):
            vid = tokens[tokens.index("--volume-id") + 1]
            self.cloud["volumes"] = [v for v in self.cloud["volumes"] if v["VolumeId"] != vid]
        elif (svc, op) == ("ec2", "delete-security-group"):
            gid = tokens[tokens.index("--group-id") + 1]
            self.cloud["sgs"] = [g for g in self.cloud["sgs"] if g["GroupId"] != gid]
        elif (svc, op) == ("ec2", "delete-key-pair"):
            kname = tokens[tokens.index("--key-name") + 1]
            self.cloud["keys"] = [k for k in self.cloud["keys"] if k["KeyName"] != kname]
        elif (svc, op) == ("s3api", "head-bucket"):
            bname = tokens[tokens.index("--bucket") + 1]
            if bname in self.cloud.get("forbidden_buckets", set()):
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    stdout="",
                    stderr="An error occurred (403) when calling the HeadBucket operation: Forbidden",
                )
            rc = 0 if bname in self.cloud["buckets"] else 1
        elif (svc, op) == ("s3api", "list-buckets"):
            out = json.dumps({"Buckets": [{"Name": b} for b in sorted(self.cloud["buckets"])]})
        elif (svc, op) == ("s3api", "create-bucket"):
            bname = tokens[tokens.index("--bucket") + 1]
            self.cloud["buckets"].add(bname)
            self.cloud.setdefault("bucket_tags", {})[bname] = [
                {"Key": "Project", "Value": "BranchBenchLeaderboard"},
                {"Key": "Phase", "Value": "alpha"},
                {"Key": "Stack", "Value": "branchbench-alpha"},
            ]
            out = json.dumps({"Location": f"/{bname}"})
        elif (svc, op) == ("s3api", "get-bucket-tagging"):
            bname = tokens[tokens.index("--bucket") + 1]
            tags = self.cloud.get("bucket_tags", {}).get(bname)
            if tags is None:
                rc = 1
            else:
                out = json.dumps({"TagSet": tags})
        elif (svc, op) == ("s3api", "list-object-versions"):
            out = json.dumps({"Versions": [{"Key": "index.html", "VersionId": "v1"}], "DeleteMarkers": []})
        elif (svc, op) == ("s3api", "delete-bucket"):
            bname = tokens[tokens.index("--bucket") + 1]
            if bname in self.cloud.get("fail_delete_buckets", set()):
                rc = 1
            else:
                self.cloud["buckets"].discard(bname)
        elif (svc, op) == ("s3", "presign"):
            out = "https://s3.amazonaws.com/presigned/index.html"
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

    def test_up_deploy_status_and_collapse_tear_down_all_resources(self):
        self.write(self.payload())
        aws = self.aws_alpha.AwsCli(region="us-east-1", runner=self.fake_aws_runner)
        state = self.aws_alpha.up_alpha(
            aws=aws,
            stack="branchbench-alpha",
            instance_type="i4i.2xlarge",
            spot=True,
            runs_dir=self.runs,
            http_getter=lambda _url: "203.0.113.9\n",
        )
        self.assertEqual(state["phase"], "alpha")
        self.assertEqual(state["instance_id"], "i-0alpha999")
        self.assertEqual(state["ssh_cidr"], "203.0.113.9/32")
        self.assertEqual(state["bucket"], "branchbench-alpha-123456789012-us-east-1")
        self.assertTrue((self.runs / "aws_alpha_state.json").is_file())
        self.assertTrue((self.runs / "branchbench-alpha.pem").is_file())

        # Duplicate `up` while active refuses to leak a second instance
        with self.assertRaisesRegex(self.aws_alpha.AlphaError, "already has an EC2 instance"):
            self.aws_alpha.up_alpha(
                aws=aws,
                stack="branchbench-alpha",
                runs_dir=self.runs,
                http_getter=lambda _url: "203.0.113.9",
            )

        deployed = self.aws_alpha.deploy_alpha_site(
            aws=aws,
            stack="branchbench-alpha",
            board=self.board,
            runs_dir=self.runs,
        )
        self.assertTrue((self.site / "alpha" / "index.html").is_file())
        self.assertEqual(deployed["bucket"], "branchbench-alpha-123456789012-us-east-1")

        active_status = self.aws_alpha.status_alpha(
            aws=aws,
            stack="branchbench-alpha",
            board=self.board,
            runs_dir=self.runs,
        )
        self.assertFalse(active_status["collapsed"])

        # Even if state file is completely lost before collapse, tag discovery (including list-buckets + tag check) tears everything down
        (self.runs / "aws_alpha_state.json").unlink()
        collapsed = self.aws_alpha.collapse_alpha(
            aws=aws,
            stack="branchbench-alpha",
            board=self.board,
            runs_dir=self.runs,
        )
        self.assertTrue(collapsed["collapsed"])
        self.assertEqual(collapsed["terminated_instances"], ["i-0alpha999"])
        self.assertEqual(collapsed["deleted_volumes"], ["vol-0alpha1"])
        self.assertEqual(collapsed["deleted_security_groups"], ["sg-0alpha123"])
        self.assertEqual(collapsed["deleted_bucket"], "branchbench-alpha-123456789012-us-east-1")
        self.assertEqual(collapsed["ongoing_aws_cost"], "$0.00/hr")
        self.assertFalse((self.site / "alpha").exists())
        self.assertFalse((self.runs / "aws_alpha_state.json").exists())
        self.assertFalse((self.runs / "branchbench-alpha.pem").exists())

        final_status = self.aws_alpha.status_alpha(
            aws=aws,
            stack="branchbench-alpha",
            board=self.board,
            runs_dir=self.runs,
        )
        self.assertTrue(final_status["collapsed"])

    def test_run_with_collapse_after_pulls_results_builds_alpha_site_and_collapses_aws(self):
        import subprocess
        self.write(self.payload())
        aws = self.aws_alpha.AwsCli(region="us-east-1", runner=self.fake_aws_runner)
        self.aws_alpha.up_alpha(
            aws=aws,
            stack="branchbench-alpha",
            runs_dir=self.runs,
            http_getter=lambda _url: "203.0.113.9",
        )
        ssh_rsync_calls = []

        def fake_cmd_runner(cmd: list[str], **_kwargs):
            ssh_rsync_calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        rc = self.aws_alpha.run_alpha_benchmarks(
            aws=aws,
            stack="branchbench-alpha",
            preset="mini",
            systems="dolt dolt_mysql",
            collapse_after=True,
            board=self.board,
            runs_dir=self.runs,
            cmd_runner=fake_cmd_runner,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(len(ssh_rsync_calls), 4, "wait for bootstrap, rsync up, ssh worker run, rsync pull")
        self.assertTrue((self.site / "alpha" / "data.js").is_file(), "--collapse-after keeps local .site/alpha while tearing down AWS")
        self.assertFalse((self.runs / "aws_alpha_state.json").exists(), "AWS state is collapsed after the run")
        self.assertEqual(self.cloud["sgs"], [])
        self.assertEqual(self.cloud["buckets"], set())

    def test_foreign_or_untagged_bucket_is_refused(self):
        aws = self.aws_alpha.AwsCli(region="us-east-1", runner=self.fake_aws_runner)
        self.cloud["buckets"].add("foreign-prod-bucket")
        self.cloud.setdefault("bucket_tags", {})["foreign-prod-bucket"] = [
            {"Key": "Project", "Value": "OtherApp"},
            {"Key": "Phase", "Value": "prod"},
        ]
        with self.assertRaisesRegex(self.aws_alpha.AlphaError, "refusing to modify or delete pre-existing S3 bucket"):
            self.aws_alpha.up_alpha(
                aws=aws,
                stack="branchbench-alpha",
                bucket="foreign-prod-bucket",
                runs_dir=self.runs,
                http_getter=lambda _url: "203.0.113.9",
            )
        self.assertIn("foreign-prod-bucket", self.cloud["buckets"])

    def test_collapse_after_runs_on_failure_and_stdin_carries_secrets(self):
        import os
        import subprocess
        from unittest import mock

        self.write(self.payload())
        aws = self.aws_alpha.AwsCli(region="us-east-1", runner=self.fake_aws_runner)
        self.aws_alpha.up_alpha(
            aws=aws,
            stack="branchbench-alpha",
            runs_dir=self.runs,
            http_getter=lambda _url: "203.0.113.9",
        )
        captured_calls = []

        def failing_on_pull(cmd: list[str], **kwargs):
            captured_calls.append((cmd, kwargs))
            if len(captured_calls) == 4:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="pull failed")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {"NEON_API_KEY_ORG": "super-secret-neon-key"}):
            with self.assertRaisesRegex(self.aws_alpha.AlphaError, "failed to pull results"):
                self.aws_alpha.run_alpha_benchmarks(
                    aws=aws,
                    stack="branchbench-alpha",
                    preset="mini",
                    lane="macro",
                    systems="dolt",
                    collapse_after=True,
                    board=self.board,
                    runs_dir=self.runs,
                    cmd_runner=failing_on_pull,
                )
        ssh_cmd, ssh_kwargs = captured_calls[2]
        self.assertIn("--lane macro", " ".join(ssh_cmd))
        self.assertNotIn("super-secret-neon-key", " ".join(ssh_cmd), "secret must not appear in ssh argv")
        self.assertIn("super-secret-neon-key", ssh_kwargs.get("input", ""), "secret is passed via ssh stdin")
        self.assertFalse((self.runs / "aws_alpha_state.json").exists(), "collapse_after ran in finally despite pull failure")
        self.assertEqual(self.cloud["sgs"], [])
        self.assertEqual(self.cloud["buckets"], set())

    def test_head_bucket_403_fails_closed_and_partial_collapse_retains_state_and_Sync_excludes_untracked(self):
        aws = self.aws_alpha.AwsCli(region="us-east-1", runner=self.fake_aws_runner)
        self.cloud.setdefault("forbidden_buckets", set()).add("forbidden-bucket")
        with self.assertRaisesRegex(self.aws_alpha.AlphaError, "403|Forbidden"):
            self.aws_alpha.up_alpha(
                aws=aws,
                stack="branchbench-alpha",
                bucket="forbidden-bucket",
                runs_dir=self.runs,
                http_getter=lambda _url: "203.0.113.9",
            )

        # Partial collapse keeps state file and returns collapsed=False
        state = self.aws_alpha.up_alpha(
            aws=aws,
            stack="branchbench-alpha",
            runs_dir=self.runs,
            http_getter=lambda _url: "203.0.113.9",
        )
        self.cloud.setdefault("fail_delete_buckets", set()).add(state["bucket"])
        partial = self.aws_alpha.collapse_alpha(
            aws=aws,
            stack="branchbench-alpha",
            board=self.board,
            runs_dir=self.runs,
        )
        self.assertFalse(partial["collapsed"])
        self.assertEqual(partial["remaining_bucket"], state["bucket"])
        self.assertTrue((self.runs / "aws_alpha_state.json").is_file(), "state file retained when bucket delete fails")

        # write_sync_filelist never includes untracked files or .git/config
        untracked = self.board / "untracked_secret_notes.txt"
        untracked.write_text("TOP_SECRET")
        flist = self.aws_alpha.write_sync_filelist(self.runs, "branchbench-alpha", self.aws_alpha.ROOT)
        lines = set(flist.read_text().splitlines())
        self.assertNotIn(".git/config", lines)
        self.assertFalse(any("untracked_secret_notes.txt" in ln for ln in lines))


if __name__ == "__main__":
    unittest.main()

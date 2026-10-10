from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

# Ensure scripts module is accessible
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import update_contributions as engine


class TestPrShowcaseEngine(unittest.TestCase):
    def setUp(self):
        fixture_path = (
            Path(__file__).resolve().parent / "fixtures" / "graphql_response.json"
        )
        with open(fixture_path, encoding="utf-8") as f:
            self.graphql_data = json.load(f)

    def test_parse_graphql_prs_and_repos(self):
        nodes = self.graphql_data["data"]["search"]["nodes"]
        prs, repos = engine.parse_graphql_nodes(nodes)

        self.assertEqual(len(prs), 2)

        merged_pr = next(p for p in prs if p["state"] == "merged")
        self.assertEqual(merged_pr["number"], 101)
        self.assertEqual(merged_pr["repo"], "octocat/super-engine")
        self.assertEqual(merged_pr["title"], "feat(core): add async event dispatcher")

        open_pr = next(p for p in prs if p["state"] == "open")
        self.assertEqual(open_pr["number"], 42)
        self.assertEqual(open_pr["repo"], "dev-org/code-parser")

        self.assertIn("octocat/super-engine", repos)
        repo_info = repos["octocat/super-engine"]
        self.assertEqual(repo_info["stars"], 14200)
        self.assertEqual(
            repo_info["owner_avatar"], "https://github.com/octocat.png?size=64"
        )

    def test_render_contains_key_elements(self):
        nodes = self.graphql_data["data"]["search"]["nodes"]
        prs, repos = engine.parse_graphql_nodes(nodes)
        config = engine.get_default_config()
        config["show_stars"] = True

        rendered = engine.render(prs, repos, config)

        # Check headline metrics
        self.assertIn("pull_requests-2", rendered)
        self.assertIn("merged-1", rendered)
        self.assertIn("in_review-1", rendered)
        self.assertIn("projects-2", rendered)

        # Check repository header and PR titles
        self.assertIn("octocat/super-engine", rendered)
        self.assertIn("feat(core): add async event dispatcher", rendered)
        self.assertIn("dev-org/code-parser", rendered)
        self.assertIn("fix(parser): resolve token stream overflow", rendered)

        # Check star badge in cloud
        self.assertIn("img.shields.io/github/stars/octocat/super-engine", rendered)

    def test_inject_markers_existing(self):
        nodes = self.graphql_data["data"]["search"]["nodes"]
        prs, repos = engine.parse_graphql_nodes(nodes)
        initial = (
            "# My Profile\n\n"
            "<!-- CONTRIB:START -->\n"
            "old content\n"
            "<!-- CONTRIB:END -->\n\n"
            "Footer"
        )
        updated = engine.inject_content(initial, prs, repos)
        self.assertIn("<!-- CONTRIB:START -->", updated)
        self.assertIn("pull_requests-2", updated)
        self.assertIn("<!-- CONTRIB:END -->", updated)
        self.assertTrue(updated.startswith("# My Profile\n\n"))
        self.assertTrue(updated.endswith("\n\nFooter"))

    def test_inject_modular_sub_markers_claimed(self):
        nodes = self.graphql_data["data"]["search"]["nodes"]
        prs, repos = engine.parse_graphql_nodes(nodes)
        initial = (
            "# My Profile\n\n"
            "<!-- CONTRIB:METRICS:START -->\n<!-- CONTRIB:METRICS:END -->\n\n"
            "Some content in between.\n\n"
            "<!-- CONTRIB:START -->\n<!-- CONTRIB:END -->\n"
        )
        updated = engine.inject_content(initial, prs, repos)
        # Metrics injected into SUB_MARKER
        self.assertIn('<!-- CONTRIB:METRICS:START -->\n<p align="center">', updated)
        # Global block should NOT duplicate metrics (Claimed Section Principle)
        global_block = updated.split("<!-- CONTRIB:START -->")[1].split(
            "<!-- CONTRIB:END -->"
        )[0]
        self.assertNotIn("pull_requests-2", global_block)
        self.assertIn("### Merged upstream", global_block)

    def test_idempotent_injection(self):
        nodes = self.graphql_data["data"]["search"]["nodes"]
        prs, repos = engine.parse_graphql_nodes(nodes)
        initial = "# My Profile\n"
        first = engine.inject_content(initial, prs, repos)
        second = engine.inject_content(first, prs, repos)
        self.assertEqual(first, second)
        self.assertEqual(second.count("<!-- CONTRIB:START -->"), 1)
        self.assertEqual(second.count("<!-- CONTRIB:END -->"), 1)

    def test_alignment_options(self):
        nodes = self.graphql_data["data"]["search"]["nodes"]
        prs, repos = engine.parse_graphql_nodes(nodes)

        # Default alignment: badge_alignment="center", contributed_to_alignment="left"
        cfg_default = engine.get_default_config()
        rendered_default = engine.render(prs, repos, cfg_default)
        self.assertIn('<p align="center">', rendered_default)
        self.assertIn('<p align="left">', rendered_default)

        # Custom alignment: badge_alignment="left", contributed_to_alignment="center"
        cfg_custom = engine.get_default_config()
        cfg_custom["badge_alignment"] = "left"
        cfg_custom["contributed_to_alignment"] = "center"
        metrics_block = engine.render_metrics_section(prs, repos, cfg_custom)
        projects_block = engine.render_projects_section(prs, repos, cfg_custom)
        self.assertTrue(metrics_block.startswith('<p align="left">'))
        self.assertIn('<p align="center">', projects_block)

    def test_synthetic_merge_closer(self):
        node = {
            "number": 5083,
            "title": "Default potential-bad-keyword-argument severity to ignore",
            "url": "https://github.com/facebook/pyrefly/pull/5083",
            "state": "CLOSED",
            "merged": false if False else False,
            "mergedAt": None,
            "createdAt": "2026-10-02T06:05:00Z",
            "timelineItems": {
                "nodes": [
                    {
                        "closer": {
                            "oid": "16b85b9b2de5fd25c35ef209cedd188a17e1f6a1",
                            "url": "https://github.com/facebook/pyrefly/commit/16b85b9",
                        }
                    }
                ]
            },
            "repository": {
                "nameWithOwner": "facebook/pyrefly",
                "name": "pyrefly",
                "description": "A fast type checker and language server for Python",
                "stargazerCount": 5000,
                "owner": {"login": "facebook", "avatarUrl": "https://github.com/facebook.png"},
            },
        }
        prs, repos = engine.parse_graphql_nodes([node])
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0]["state"], "merged")
        self.assertEqual(prs[0]["repo"], "facebook/pyrefly")

    def test_synthetic_merge_bot_comment(self):
        node = {
            "number": 123,
            "title": "fix: update internal sync logic",
            "url": "https://github.com/google/benchmark/pull/123",
            "state": "CLOSED",
            "merged": False,
            "mergedAt": None,
            "createdAt": "2026-09-01T10:00:00Z",
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "copybara-service"},
                        "body": "Closed by commit abc1234def5678",
                    }
                ]
            },
            "repository": {
                "nameWithOwner": "google/benchmark",
                "name": "benchmark",
                "description": "Benchmark framework",
                "stargazerCount": 8000,
                "owner": {"login": "google"},
            },
        }
        prs, _ = engine.parse_graphql_nodes([node])
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0]["state"], "merged")

    def test_synthetic_merge_rejected_discarded(self):
        node = {
            "number": 5041,
            "title": "for test stack pr",
            "url": "https://github.com/facebook/pyrefly/pull/5041",
            "state": "CLOSED",
            "merged": False,
            "mergedAt": None,
            "createdAt": "2026-10-01T10:00:00Z",
            "timelineItems": {"nodes": [{"closer": None}]},
            "comments": {
                "nodes": [
                    {
                        "author": {"login": "meta-codesync"},
                        "body": "This pull request has been imported.",
                    }
                ]
            },
            "repository": {
                "nameWithOwner": "facebook/pyrefly",
                "name": "pyrefly",
            },
        }
        prs, _ = engine.parse_graphql_nodes([node])
        self.assertEqual(len(prs), 0)

    def test_force_merged_override(self):
        node = {
            "number": 999,
            "title": "fix: rare edge case",
            "url": "https://github.com/custom/project/pull/999",
            "state": "CLOSED",
            "merged": False,
            "mergedAt": None,
            "createdAt": "2026-08-01T10:00:00Z",
            "repository": {
                "nameWithOwner": "custom/project",
                "name": "project",
            },
        }
        prs, _ = engine.parse_graphql_nodes([node], force_merged_prs={"custom/project#999"})
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0]["state"], "merged")

    def test_coauthored_pr_rendering(self):
        prs = [
            {
                "repo": "facebook/pyrefly",
                "number": 5091,
                "title": "Fix/protocol explicit self",
                "url": "https://github.com/facebook/pyrefly/pull/5091",
                "state": "merged",
                "created": "2026-10-03",
                "is_coauthor": True,
            }
        ]
        repos = {
            "facebook/pyrefly": {
                "name": "facebook/pyrefly",
                "description": "Python type checker",
                "stars": 5000,
                "owner_avatar": "https://github.com/facebook.png",
            }
        }
        rendered = engine.render(prs, repos)
        self.assertIn("- [#5091](https://github.com/facebook/pyrefly/pull/5091) Fix/protocol explicit self *(Co-author)*", rendered)
        self.assertIn("img.shields.io/github/stars/facebook/pyrefly", rendered)

    def test_parse_pr_references(self):
        raw = "facebook/pyrefly#5091, https://github.com/astral-sh/uv/pull/1234\nother/repo#42"
        refs = engine.parse_pr_references(raw)
        self.assertEqual(
            refs,
            [
                ("facebook", "pyrefly", 5091),
                ("astral-sh", "uv", 1234),
                ("other", "repo", 42),
            ],
        )


if __name__ == "__main__":
    unittest.main()

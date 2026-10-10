#!/usr/bin/env python3
"""pr-showcase: Automated open-source pull request showcase engine.

Fetches public pull requests authored by a user in upstream repositories,
enriches them with live repository stats (descriptions, star counts, owner avatars),
and injects a visual portfolio into the target markdown file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

START_MARKER = "<!-- CONTRIB:START -->"
END_MARKER = "<!-- CONTRIB:END -->"

# Modular Sub-Markers for Granular Placement
SUB_MARKERS = {
    "metrics": ("<!-- CONTRIB:METRICS:START -->", "<!-- CONTRIB:METRICS:END -->"),
    "merged": ("<!-- CONTRIB:MERGED:START -->", "<!-- CONTRIB:MERGED:END -->"),
    "in_review": ("<!-- CONTRIB:IN_REVIEW:START -->", "<!-- CONTRIB:IN_REVIEW:END -->"),
    "projects": ("<!-- CONTRIB:PROJECTS:START -->", "<!-- CONTRIB:PROJECTS:END -->"),
}

GRAPHQL_QUERY = """
query($query: String!, $cursor: String) {
  search(query: $query, type: ISSUE, first: 100, after: $cursor) {
    issueCount
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on PullRequest {
        number
        title
        url
        state
        merged
        mergedAt
        createdAt
        timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {
          nodes {
            ... on ClosedEvent {
              closer {
                ... on Commit {
                  oid
                  url
                }
              }
            }
          }
        }
        comments(last: 3) {
          nodes {
            author {
              login
            }
            body
          }
        }
        labels(first: 5) {
          nodes {
            name
          }
        }
        repository {
          nameWithOwner
          name
          description
          stargazerCount
          owner {
            login
            avatarUrl(size: 64)
          }
        }
      }
    }
  }
}
"""

SYNTHETIC_MERGE_PATTERNS = [
    re.compile(r"This pull request has been merged in [^\s]+@([0-9a-fA-F]{7,40})"),
    re.compile(r"(?:Closed by commit|Merged in)\s+([0-9a-fA-F]{7,40})"),
    re.compile(r"(?:Merged via commit|Pushed to [^\s]+ as commit)\s+([0-9a-fA-F]{7,40})"),
]

MERGE_LABELS = {
    "merged-upstream",
    "status: merged",
    "status:merged",
    "landed",
}


def parse_pr_references(raw_input: str | None) -> list[tuple[str, str, int]]:
    """Parse PR references in format 'owner/repo#number' or full GitHub PR URLs."""
    if not raw_input:
        return []
    refs = []
    tokens = re.split(r"[\s,]+", raw_input.strip())
    for token in tokens:
        if not token:
            continue
        url_match = re.match(
            r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", token, re.IGNORECASE
        )
        if url_match:
            refs.append((url_match.group(1), url_match.group(2), int(url_match.group(3))))
            continue
        ref_match = re.match(r"^([^/]+)/([^#]+)#(\d+)$", token)
        if ref_match:
            refs.append((ref_match.group(1), ref_match.group(2), int(ref_match.group(3))))
            continue
    return refs


def is_synthetically_merged(
    node: dict, force_merged_prs: set[str] | None = None
) -> bool:
    """Determine if a closed PR was merged via an external bot/monorepo sync or whitelist."""
    repo_name = (node.get("repository") or {}).get("nameWithOwner", "")
    pr_num = node.get("number")
    pr_ref = f"{repo_name}#{pr_num}".lower() if repo_name and pr_num else ""

    # 0. Whitelist / force-merged override
    if force_merged_prs and pr_ref and (pr_ref in force_merged_prs or str(pr_num) in force_merged_prs):
        return True

    # 1. GitHub Knowledge Graph: timelineItems ClosedEvent.closer is a Commit
    timeline_nodes = (node.get("timelineItems") or {}).get("nodes") or []
    for item in timeline_nodes:
        closer = (item or {}).get("closer")
        if closer and closer.get("oid"):
            return True

    # 2. Bot Comment Signatures
    comments = (node.get("comments") or {}).get("nodes") or []
    for comment in comments:
        body = (comment or {}).get("body") or ""
        for pattern in SYNTHETIC_MERGE_PATTERNS:
            if pattern.search(body):
                return True

    # 3. Upstream Land/Merge Labels
    labels = (node.get("labels") or {}).get("nodes") or []
    for label in labels:
        name = ((label or {}).get("name") or "").strip().lower()
        if name in MERGE_LABELS:
            return True

    return False



def get_default_config() -> dict:
    """Return default presentation configuration."""
    return {
        "badge_alignment": "center",
        "contributed_to_alignment": "left",
        "show_stars": True,
        "show_metrics": True,
        "show_merged": True,
        "show_in_review": True,
        "show_contributed_to": True,
        "max_featured_merged": 5,
        "collapse_in_review": True,
        "collapse_more_merged": True,
        "section_order": ["metrics", "merged", "in_review", "projects"],
        "descriptions": {
            "featured_merged": "both",
            "more_merged": "tooltip",
            "in_review": "both",
            "star_badges": "tooltip",
        },
    }


def get_owner_avatar(repo_name: str, owner_data: dict | None = None) -> str:
    """Retrieve owner avatar URL with fallback to GitHub avatar service."""
    if owner_data and owner_data.get("avatarUrl"):
        return owner_data["avatarUrl"]
    owner_login = (owner_data or {}).get("login") or repo_name.split("/")[0]
    return f"https://github.com/{owner_login}.png?size=64"


def resolve_token(cli_token: str | None = None) -> str | None:
    """Resolve GitHub token from CLI argument, environment, or GitHub CLI (gh)."""
    if cli_token:
        return cli_token
    token = os.environ.get("PR_SHOWCASE_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    if shutil.which("gh"):
        try:
            res = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            val = res.stdout.strip()
            if val:
                return val
        except (subprocess.SubprocessError, OSError):
            pass
    return None


def request_http(url: str, token: str | None = None, data: bytes | None = None) -> dict:
    """Execute an HTTP request with standard JSON headers and error handling."""
    req = urllib.request.Request(url, data=data)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "pr-showcase-engine")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")

    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def parse_graphql_nodes(
    nodes: list[dict], force_merged_prs: set[str] | None = None
) -> tuple[list[dict], dict[str, dict]]:
    """Normalize raw GraphQL search nodes into structured PR and repository records."""
    prs = []
    repos = {}

    for node in nodes:
        if not node or "repository" not in node:
            continue

        repo_data = node["repository"]
        repo_name = repo_data["nameWithOwner"]

        # Cache repository metadata
        if repo_name not in repos:
            owner = repo_data.get("owner") or {}
            repos[repo_name] = {
                "name": repo_name,
                "description": repo_data.get("description") or repo_name,
                "stars": repo_data.get("stargazerCount", 0),
                "owner_avatar": get_owner_avatar(repo_name, owner),
            }

        merged = bool(node.get("merged") or node.get("mergedAt"))
        if not merged and node.get("state") == "CLOSED":
            merged = is_synthetically_merged(node, force_merged_prs)

        state = (
            "merged"
            if merged
            else ("open" if node.get("state") == "OPEN" else "closed")
        )

        # Discard closed unmerged PRs
        if state not in ("merged", "open"):
            continue

        prs.append(
            {
                "repo": repo_name,
                "number": node["number"],
                "title": node["title"].rstrip("…").strip(),
                "url": node["url"],
                "state": state,
                "created": node.get("createdAt", "")[:10],
            }
        )

    return prs, repos


def fetch_contributions_graphql(
    username: str, token: str, force_merged_prs: set[str] | None = None
) -> tuple[list[dict], dict[str, dict]]:
    """Fetch user PRs and repository metadata in a single GraphQL query."""
    url = "https://api.github.com/graphql"
    search_filter = f"author:{username} is:pr -user:{username} is:public"
    nodes = []
    cursor = None

    while True:
        payload = json.dumps(
            {
                "query": GRAPHQL_QUERY,
                "variables": {
                    "query": search_filter,
                    "cursor": cursor,
                },
            }
        ).encode("utf-8")

        response = request_http(url, token=token, data=payload)
        search_result = response.get("data", {}).get("search", {})
        batch = search_result.get("nodes", [])
        nodes.extend(batch)

        page_info = search_result.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")
        if not cursor:
            break

    return parse_graphql_nodes(nodes, force_merged_prs=force_merged_prs)


def fetch_contributions_rest(
    username: str,
    token: str | None = None,
    force_merged_prs: set[str] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Fallback REST API ingestion when GraphQL is inaccessible."""
    q = urllib.parse.quote(f"author:{username} is:pr -user:{username} is:public")
    items: list[dict] = []
    page = 1

    while True:
        url = f"https://api.github.com/search/issues?q={q}&per_page=100&page={page}&sort=created&order=desc"
        data = request_http(url, token=token)
        batch = data.get("items", [])
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1

    prs = []
    unique_repo_names = set()
    for it in items:
        repo = "/".join(it["repository_url"].split("/")[-2:])
        pr_info = it.get("pull_request") or {}
        pr_ref = f"{repo}#{it['number']}".lower()
        merged = bool(pr_info.get("merged_at"))
        if not merged and it.get("state") == "closed" and force_merged_prs:
            if pr_ref in force_merged_prs or str(it["number"]) in force_merged_prs:
                merged = True

        state = "merged" if merged else ("open" if it["state"] == "open" else "closed")
        if state not in ("merged", "open"):
            continue

        unique_repo_names.add(repo)
        prs.append(
            {
                "repo": repo,
                "number": it["number"],
                "title": it["title"].rstrip("…").strip(),
                "url": it["html_url"],
                "state": state,
                "created": it["created_at"][:10],
            }
        )

    # Fetch individual repo metadata
    repos = {}
    for repo in sorted(unique_repo_names):
        try:
            repo_data = request_http(
                f"https://api.github.com/repos/{repo}", token=token
            )
            owner = repo_data.get("owner", {})
            repos[repo] = {
                "name": repo,
                "description": repo_data.get("description") or repo,
                "stars": repo_data.get("stargazers_count", 0),
                "owner_avatar": get_owner_avatar(repo, owner),
            }
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            OSError,
        ):
            repos[repo] = {
                "name": repo,
                "description": repo,
                "stars": 0,
                "owner_avatar": get_owner_avatar(repo),
            }

    return prs, repos


def fetch_coauthored_prs_graphql(
    refs: list[tuple[str, str, int]],
    token: str,
    force_merged_prs: set[str] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Fetch declared co-authored PRs using an aliased GraphQL batch query."""
    if not refs:
        return [], {}

    query_parts = ["query GetCoauthoredPRs {"]
    for i, (owner, repo, number) in enumerate(refs):
        query_parts.append(
            f"""  pr_{i}: repository(owner: "{owner}", name: "{repo}") {{
    pullRequest(number: {number}) {{
      number
      title
      url
      state
      merged
      mergedAt
      createdAt
      timelineItems(itemTypes: [CLOSED_EVENT], last: 1) {{
        nodes {{
          ... on ClosedEvent {{
            closer {{
              ... on Commit {{
                oid
                url
              }}
            }}
          }}
        }}
      }}
      comments(last: 3) {{
        nodes {{
          author {{ login }}
          body
        }}
      }}
      labels(first: 5) {{
        nodes {{ name }}
      }}
      repository {{
        nameWithOwner
        name
        description
        stargazerCount
        owner {{
          login
          avatarUrl(size: 64)
        }}
      }}
    }}
  }}"""
        )
    query_parts.append("}")
    full_query = "\n".join(query_parts)

    url = "https://api.github.com/graphql"
    payload = json.dumps({"query": full_query}).encode("utf-8")
    try:
        response = request_http(url, token=token, data=payload)
    except Exception as exc:
        print(
            f"[pr-showcase] Failed to fetch co-authored PRs via GraphQL ({exc})",
            file=sys.stderr,
        )
        return [], {}

    data = response.get("data") or {}
    coauthored_prs = []
    repos = {}

    for i, (owner, repo, number) in enumerate(refs):
        alias_data = data.get(f"pr_{i}")
        if not alias_data:
            continue
        pr_node = alias_data.get("pullRequest")
        if not pr_node or not pr_node.get("repository"):
            continue

        repo_data = pr_node["repository"]
        repo_name = repo_data["nameWithOwner"]

        if repo_name not in repos:
            owner_data = repo_data.get("owner") or {}
            repos[repo_name] = {
                "name": repo_name,
                "description": repo_data.get("description") or repo_name,
                "stars": repo_data.get("stargazerCount", 0),
                "owner_avatar": get_owner_avatar(repo_name, owner_data),
            }

        merged = bool(pr_node.get("merged") or pr_node.get("mergedAt"))
        if not merged and pr_node.get("state") == "CLOSED":
            merged = is_synthetically_merged(pr_node, force_merged_prs)

        state = (
            "merged"
            if merged
            else ("open" if pr_node.get("state") == "OPEN" else "closed")
        )

        if state not in ("merged", "open"):
            continue

        coauthored_prs.append(
            {
                "repo": repo_name,
                "number": pr_node["number"],
                "title": pr_node["title"].rstrip("…").strip(),
                "url": pr_node["url"],
                "state": state,
                "created": pr_node.get("createdAt", "")[:10],
                "is_coauthor": True,
            }
        )

    return coauthored_prs, repos


def fetch_coauthored_prs_rest(
    refs: list[tuple[str, str, int]],
    token: str | None = None,
    force_merged_prs: set[str] | None = None,
) -> tuple[list[dict], dict[str, dict]]:
    """Fetch declared co-authored PRs using REST API fallback."""
    if not refs:
        return [], {}

    prs = []
    repos = {}
    for owner, repo_name, number in refs:
        full_repo = f"{owner}/{repo_name}"
        pr_ref = f"{full_repo}#{number}".lower()
        try:
            pr_data = request_http(
                f"https://api.github.com/repos/{full_repo}/pulls/{number}", token=token
            )
            merged = bool(pr_data.get("merged_at"))
            if not merged and pr_data.get("state") == "closed" and force_merged_prs:
                if pr_ref in force_merged_prs or str(number) in force_merged_prs:
                    merged = True

            state = (
                "merged"
                if merged
                else ("open" if pr_data.get("state") == "open" else "closed")
            )
            if state not in ("merged", "open"):
                continue

            if full_repo not in repos:
                repo_data = pr_data.get("base", {}).get("repo") or {}
                owner_data = repo_data.get("owner") or {}
                repos[full_repo] = {
                    "name": full_repo,
                    "description": repo_data.get("description") or full_repo,
                    "stars": repo_data.get("stargazers_count", 0),
                    "owner_avatar": get_owner_avatar(full_repo, owner_data),
                }

            prs.append(
                {
                    "repo": full_repo,
                    "number": pr_data["number"],
                    "title": pr_data["title"].rstrip("…").strip(),
                    "url": pr_data["html_url"],
                    "state": state,
                    "created": pr_data.get("created_at", "")[:10],
                    "is_coauthor": True,
                }
            )
        except Exception:
            continue

    return prs, repos


def escape_attr(text: str) -> str:
    """Escape quotes and newlines for markdown/HTML attributes."""
    return text.replace('"', "&quot;").replace("\n", " ").strip()


def format_repo_header(repo_info: dict, mode: str) -> str:
    """Format repository header with avatar, link, and tooltip description."""
    repo = repo_info["name"]
    desc = repo_info.get("description") or repo
    avatar_url = repo_info.get("owner_avatar") or get_owner_avatar(repo)
    owner_name = repo.split("/")[0]
    avatar = f'<img src="{avatar_url}" width="16" height="16" valign="middle" alt="{owner_name}" />'
    clean_desc = escape_attr(desc)

    if mode in ("tooltip", "both"):
        link = f'[`{repo}`](https://github.com/{repo} "{clean_desc}")'
    else:
        link = f"[`{repo}`](https://github.com/{repo})"

    header = f"**{avatar} {link}**"

    if mode in ("inline", "both") and desc and desc != repo:
        header += f"<br/><sub>{desc}</sub>"
    return header


def render_grouped_prs(
    prs: list[dict],
    repos: dict[str, dict],
    mode: str,
) -> list[str]:
    """Render PRs grouped by repository with avatars and links."""
    by_repo: dict[str, list[dict]] = defaultdict(list)
    for p in prs:
        by_repo[p["repo"]].append(p)

    lines: list[str] = []
    for repo in sorted(by_repo, key=lambda r: (-len(by_repo[r]), r.lower())):
        repo_info = repos.get(
            repo,
            {
                "name": repo,
                "description": repo,
                "stars": 0,
                "owner_avatar": get_owner_avatar(repo),
            },
        )
        lines.append(format_repo_header(repo_info, mode))
        for p in sorted(by_repo[repo], key=lambda x: x["number"], reverse=True):
            suffix = " *(Co-author)*" if p.get("is_coauthor") else ""
            lines.append(f"- [#{p['number']}]({p['url']}) {p['title']}{suffix}")
        lines.append("")
    return lines


def render_metrics_section(prs: list[dict], _repos: dict[str, dict], cfg: dict) -> str:
    """Render headline metric badges."""
    merged = [p for p in prs if p["state"] == "merged"]
    open_prs = [p for p in prs if p["state"] == "open"]
    projects = sorted({p["repo"] for p in prs})
    out: list[str] = []

    align = cfg.get("badge_alignment", "center")
    align_attr = f' align="{align}"' if align in ("center", "left", "right") else ""

    # Headline counts
    out.append(f"<p{align_attr}>")
    out.append(
        f'  <img src="https://img.shields.io/badge/pull_requests-{len(prs)}-1f6feb?style=flat-square&labelColor=161b22&logo=git&logoColor=white" alt="PRs" />'
    )
    out.append(
        f'  <img src="https://img.shields.io/badge/merged-{len(merged)}-8957e5?style=flat-square&labelColor=161b22&logo=github&logoColor=white" alt="Merged" />'
    )
    out.append(
        f'  <img src="https://img.shields.io/badge/in_review-{len(open_prs)}-2da44e?style=flat-square&labelColor=161b22&logo=githubactions&logoColor=white" alt="In Review" />'
    )
    out.append(
        f'  <img src="https://img.shields.io/badge/projects-{len(projects)}-f78166?style=flat-square&labelColor=161b22&logo=opensourceinitiative&logoColor=white" alt="Projects" />'
    )
    out.append("</p>\n")

    return "\n".join(out)


def render_merged_section(prs: list[dict], repos: dict[str, dict], cfg: dict) -> str:
    """Render merged upstream contributions with featured list and overflow drawer."""
    merged = [p for p in prs if p["state"] == "merged"]
    if not merged:
        return ""

    sorted_merged = sorted(merged, key=lambda x: x["created"], reverse=True)
    max_featured = cfg.get("max_featured_merged", 5)
    featured_prs = sorted_merged[:max_featured]
    overflow_prs = sorted_merged[max_featured:]

    out = ["### Merged upstream\n"]
    featured_mode = cfg.get("descriptions", {}).get("featured_merged", "both")
    out.extend(
        render_grouped_prs(
            featured_prs,
            repos,
            featured_mode,
        )
    )

    if overflow_prs:
        collapse_attr = "" if cfg.get("collapse_more_merged", True) else " open"
        out.append(f"<details{collapse_attr}>")
        out.append(
            f"<summary><b>View {len(overflow_prs)} more merged pull requests</b></summary>\n"
        )
        more_mode = cfg.get("descriptions", {}).get("more_merged", "tooltip")
        out.extend(
            render_grouped_prs(
                overflow_prs,
                repos,
                more_mode,
            )
        )
        out.append("</details>\n")

    return "\n".join(out)


def render_in_review_section(prs: list[dict], repos: dict[str, dict], cfg: dict) -> str:
    """Render open pull requests inside an interactive drawer."""
    open_prs = [p for p in prs if p["state"] == "open"]
    if not open_prs:
        return ""

    out = ["### In review\n"]
    collapse_attr = "" if cfg.get("collapse_in_review", True) else " open"
    out.append(f"<details{collapse_attr}>")
    out.append(
        f"<summary><b>{len(open_prs)} open pull requests across {len({p['repo'] for p in open_prs})} repositories</b></summary>\n"
    )
    in_review_mode = cfg.get("descriptions", {}).get("in_review", "tooltip")
    out.extend(
        render_grouped_prs(
            open_prs,
            repos,
            in_review_mode,
        )
    )
    out.append("</details>\n")
    return "\n".join(out)


def render_projects_section(prs: list[dict], repos: dict[str, dict], cfg: dict) -> str:
    """Render contributed-to repository badge cloud."""
    projects = sorted({p["repo"] for p in prs})
    if not projects:
        return ""

    out = ["### Contributed to\n"]
    contrib_align = cfg.get("contributed_to_alignment", "left")
    contrib_align_attr = (
        f' align="{contrib_align}"'
        if contrib_align in ("center", "left", "right")
        else ""
    )
    out.append(f"<p{contrib_align_attr}>")
    badge_mode = cfg.get("descriptions", {}).get("star_badges", "tooltip")
    show_stars = cfg.get("show_stars", True)

    for repo in projects:
        repo_info = repos.get(repo, {})
        desc = repo_info.get("description") or repo
        title_attr = (
            f' title="{escape_attr(desc)}"' if badge_mode == "tooltip" and desc else ""
        )
        encoded_repo = urllib.parse.quote(repo)

        if show_stars:
            badge_url = f"https://img.shields.io/github/stars/{repo}?style=flat-square&logo=github&label={encoded_repo}&color=1f6feb&labelColor=0d1117"
            badge_alt = f"{repo} stars"
        else:
            badge_url = f"https://img.shields.io/badge/{encoded_repo}-1f6feb?style=flat-square&logo=github&logoColor=white&labelColor=0d1117"
            badge_alt = repo

        out.append(
            f'  <a href="https://github.com/{repo}"{title_attr}><img alt="{badge_alt}" src="{badge_url}" /></a>'
        )
    out.append("</p>")
    return "\n".join(out)


def render(prs: list[dict], repos: dict[str, dict], config: dict | None = None) -> str:
    """Generate the full unified markdown block."""
    cfg = config or get_default_config()
    blocks = []

    order = cfg.get("section_order", ["metrics", "merged", "in_review", "projects"])
    section_generators = {
        "metrics": lambda: (
            render_metrics_section(prs, repos, cfg)
            if cfg.get("show_metrics", True)
            else ""
        ),
        "merged": lambda: (
            render_merged_section(prs, repos, cfg)
            if cfg.get("show_merged", True)
            else ""
        ),
        "in_review": lambda: (
            render_in_review_section(prs, repos, cfg)
            if cfg.get("show_in_review", True)
            else ""
        ),
        "projects": lambda: (
            render_projects_section(prs, repos, cfg)
            if cfg.get("show_contributed_to", True)
            else ""
        ),
    }

    for section in order:
        if section in section_generators:
            content = section_generators[section]()
            if content.strip():
                blocks.append(content)

    return "\n\n".join(blocks)


def inject_content(
    original_text: str,
    prs: list[dict],
    repos: dict[str, dict],
    config: dict | None = None,
) -> str:
    """Inject content using the Hybrid Placement & Claimed Section Architecture."""
    cfg = config or get_default_config()
    updated_text = original_text

    section_renderers = {
        "metrics": lambda: (
            render_metrics_section(prs, repos, cfg)
            if cfg.get("show_metrics", True)
            else ""
        ),
        "merged": lambda: (
            render_merged_section(prs, repos, cfg)
            if cfg.get("show_merged", True)
            else ""
        ),
        "in_review": lambda: (
            render_in_review_section(prs, repos, cfg)
            if cfg.get("show_in_review", True)
            else ""
        ),
        "projects": lambda: (
            render_projects_section(prs, repos, cfg)
            if cfg.get("show_contributed_to", True)
            else ""
        ),
    }

    claimed_sections = set()

    # 1. Check and inject into modular sub-markers
    for sec_name, (start_m, end_m) in SUB_MARKERS.items():
        if start_m in updated_text and end_m in updated_text:
            claimed_sections.add(sec_name)
            sec_content = section_renderers[sec_name]()
            wrapped = (
                f"{start_m}\n{sec_content}\n{end_m}"
                if sec_content.strip()
                else f"{start_m}\n{end_m}"
            )
            pattern = re.escape(start_m) + r".*?" + re.escape(end_m)
            updated_text = re.sub(
                pattern, lambda _, w=wrapped: w, updated_text, flags=re.DOTALL
            )

    # 2. Check and inject remaining UNCLAIMED sections into global container
    if START_MARKER in updated_text and END_MARKER in updated_text:
        unclaimed_blocks = []
        order = cfg.get("section_order", ["metrics", "merged", "in_review", "projects"])
        for sec in order:
            if sec not in claimed_sections and sec in section_renderers:
                content = section_renderers[sec]()
                if content.strip():
                    unclaimed_blocks.append(content)

        global_content = "\n\n".join(unclaimed_blocks)
        wrapped_global = f"{START_MARKER}\n{global_content}\n{END_MARKER}"
        pattern = re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER)
        updated_text = re.sub(
            pattern, lambda _: wrapped_global, updated_text, flags=re.DOTALL
        )
    elif not claimed_sections:
        # Self-heal: No sub-markers and no global marker found -> append global block
        rendered_all = render(prs, repos, cfg)
        wrapped_all = f"{START_MARKER}\n{rendered_all}\n{END_MARKER}"
        sep = "\n\n" if updated_text.strip() else ""
        updated_text = f"{updated_text.rstrip()}{sep}{wrapped_all}\n"

    return updated_text


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="pr-showcase",
        description="Automated GitHub open-source pull request showcase generator.",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("PR_SHOWCASE_USERNAME")
        or os.environ.get("GITHUB_ACTOR")
        or "Aayushyaash",
        help="GitHub username whose upstream PRs are showcased.",
    )
    parser.add_argument(
        "--target-file",
        default=os.environ.get("PR_SHOWCASE_TARGET_FILE", "README.md"),
        help="Target markdown file to update with the showcase ledger.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub personal access token or secrets.GITHUB_TOKEN for API calls.",
    )
    parser.add_argument(
        "--max-featured",
        type=int,
        default=int(os.environ.get("PR_SHOWCASE_MAX_FEATURED", "5")),
        help="Maximum number of merged PRs to display before collapsing into drawer.",
    )
    parser.add_argument(
        "--show-stars",
        type=lambda v: str(v).lower() in ("true", "1", "yes"),
        default=os.environ.get("PR_SHOWCASE_SHOW_STARS", "true").lower()
        in ("true", "1", "yes"),
        help="Whether to show live GitHub star badges.",
    )
    parser.add_argument(
        "--badge-alignment",
        default=os.environ.get("PR_SHOWCASE_BADGE_ALIGNMENT", "center"),
        choices=["center", "left", "right"],
        help="Alignment of metric badges.",
    )
    parser.add_argument(
        "--contributed-to-alignment",
        default=os.environ.get("PR_SHOWCASE_CONTRIBUTED_TO_ALIGNMENT", "left"),
        choices=["center", "left", "right"],
        help="Alignment of contributed-to project star badges.",
    )
    parser.add_argument(
        "--coauthored-prs",
        default=os.environ.get("PR_SHOWCASE_COAUTHORED_PRS", ""),
        help="Comma, whitespace, or newline-separated list of PRs co-authored by the user (owner/repo#number).",
    )
    parser.add_argument(
        "--force-merged-prs",
        default=os.environ.get("PR_SHOWCASE_FORCE_MERGED_PRS", ""),
        help="Comma, whitespace, or newline-separated list of PRs to force-treat as merged (owner/repo#number).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rendered markdown to stdout without modifying target file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI and Action entrypoint."""
    # Ensure stdout handles UTF-8 on Windows
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except AttributeError:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)

    cfg = get_default_config()
    cfg["max_featured_merged"] = args.max_featured
    cfg["show_stars"] = args.show_stars
    cfg["badge_alignment"] = args.badge_alignment
    cfg["contributed_to_alignment"] = args.contributed_to_alignment

    token = resolve_token(args.token)

    # Resolve force-merged PR set
    force_merged_list = parse_pr_references(args.force_merged_prs)
    force_merged_set = {f"{o}/{r}#{n}".lower() for o, r, n in force_merged_list}
    for tok in re.split(r"[\s,]+", (args.force_merged_prs or "").strip()):
        if tok:
            force_merged_set.add(tok.lower())

    prs, repos = [], {}
    if token:
        try:
            prs, repos = fetch_contributions_graphql(
                args.username, token, force_merged_prs=force_merged_set
            )
        except (
            urllib.error.URLError,
            TimeoutError,
            json.JSONDecodeError,
            KeyError,
            OSError,
        ) as exc:
            print(
                f"[pr-showcase] GraphQL query failed ({exc}); falling back to REST API...",
                file=sys.stderr,
            )
            prs, repos = fetch_contributions_rest(
                args.username, token=token, force_merged_prs=force_merged_set
            )
    else:
        print(
            "[pr-showcase] No token found; using unauthenticated REST search API...",
            file=sys.stderr,
        )
        prs, repos = fetch_contributions_rest(
            args.username, token=None, force_merged_prs=force_merged_set
        )

    # Ingest declared co-authored PRs
    coauth_refs = parse_pr_references(args.coauthored_prs)
    if coauth_refs:
        if token:
            coauth_prs, coauth_repos = fetch_coauthored_prs_graphql(
                coauth_refs, token, force_merged_prs=force_merged_set
            )
        else:
            coauth_prs, coauth_repos = fetch_coauthored_prs_rest(
                coauth_refs, token=None, force_merged_prs=force_merged_set
            )

        existing_keys = {(p["repo"].lower(), p["number"]) for p in prs}
        for cp in coauth_prs:
            if (cp["repo"].lower(), cp["number"]) not in existing_keys:
                prs.append(cp)
                existing_keys.add((cp["repo"].lower(), cp["number"]))

        for r_name, r_info in coauth_repos.items():
            if r_name not in repos:
                repos[r_name] = r_info

    if not prs:
        print(
            f"[pr-showcase] No public upstream PRs found for '{args.username}'.",
            file=sys.stderr,
        )
        return 0

    rendered = render(prs, repos, cfg)

    if args.dry_run:
        print(f"{START_MARKER}\n{rendered}\n{END_MARKER}")
        return 0

    target_path = Path(args.target_file).resolve()
    if not target_path.exists():
        initial_text = "# Open-Source Contributions\n"
    else:
        initial_text = target_path.read_text(encoding="utf-8")

    updated = inject_content(initial_text, prs, repos, cfg)
    if updated != initial_text:
        target_path.write_text(updated, encoding="utf-8")
        merged_count = sum(1 for p in prs if p["state"] == "merged")
        print(
            f"[pr-showcase] Updated {args.target_file}: {len(prs)} PRs across {len(repos)} projects ({merged_count} merged)."
        )
    else:
        print(f"[pr-showcase] {args.target_file} is already up to date.")

    return 0


if __name__ == "__main__":
    sys.exit(main())

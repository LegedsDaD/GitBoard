#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


def _utc_today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def _pct(numerator: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return (numerator / denominator) * 100.0


def _round_int(value: float) -> int:
    return int(round(value))


def _format_percent(value: float) -> str:
    return f"{_clamp(value):.1f}%"


def _tier_from_core_power(core_power: float) -> str:
    # Thresholds from user spec.
    if core_power >= 90:
        return "Ω-TIER"
    if core_power >= 75:
        return "S-TIER"
    if core_power >= 60:
        return "A-TIER"
    if core_power >= 40:
        return "B-TIER"
    if core_power >= 20:
        return "C-TIER"
    return "D-TIER"


def _iso_date(d: dt.date) -> str:
    return d.isoformat()


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _ensure_markers(readme_text: str, start: str, end: str) -> str:
    if start in readme_text and end in readme_text:
        return readme_text
    if readme_text and not readme_text.endswith("\n"):
        readme_text += "\n"
    return readme_text + f"\n{start}\n{end}\n"


def _replace_between_markers(readme_text: str, start: str, end: str, replacement_block: str) -> str:
    readme_text = _ensure_markers(readme_text, start, end)
    # Replace only when markers appear on their own lines (common pattern in profile READMEs),
    # and prefer the *last* occurrence. Markers inside fenced code blocks are ignored.
    lines = readme_text.splitlines(keepends=True)

    def is_marker_line(idx: int, marker: str) -> bool:
        return lines[idx].strip() == marker

    start_idx = None
    end_idx = None
    in_fence = False
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].lstrip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        if end_idx is None and is_marker_line(i, end):
            end_idx = i
            continue
        if end_idx is not None and start_idx is None and is_marker_line(i, start):
            start_idx = i
            break

    if start_idx is None or end_idx is None or start_idx >= end_idx:
        # Fallback to a conservative regex replace.
        pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), flags=re.DOTALL)
        block = f"{start}\n{replacement_block}\n{end}"
        return pattern.sub(block, readme_text, count=1)

    before = "".join(lines[: start_idx + 1])
    after = "".join(lines[end_idx:])
    middle = replacement_block.rstrip("\n") + "\n"
    return before + middle + after


class GitHubClient:
    def __init__(self, token: str | None):
        self._token = token

    def _request(self, method: str, url: str, body: dict | None = None) -> dict:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "GitBoard",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            details = e.read().decode("utf-8", errors="replace")
            if e.code == 403 and "rate limit" in details.lower() and not self._token:
                raise RuntimeError(
                    "GitHub API rate limit exceeded for unauthenticated requests. "
                    "Set GITHUB_TOKEN (or GH_TOKEN) to increase the rate limit."
                ) from e
            raise RuntimeError(f"GitHub API error {e.code} for {url}: {details}") from e

    def rest_get(self, path: str, query: dict | None = None) -> dict:
        base = "https://api.github.com"
        url = base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self._request("GET", url)

    def graphql(self, query: str, variables: dict) -> dict:
        payload = {"query": query, "variables": variables}
        return self._request("POST", "https://api.github.com/graphql", payload)


@dataclass(frozen=True)
class ContributionStats:
    current_streak_days: int
    active_days_365: int
    total_days_365: int
    total_contributions_365: int


@dataclass(frozen=True)
class RepoStats:
    public_repos: int
    total_stars: int
    total_forks: int
    archived_count: int
    pinned_count: int


@dataclass(frozen=True)
class SocialStats:
    followers: int
    following: int


@dataclass(frozen=True)
class CollaborationStats:
    prs_opened: int
    prs_merged: int
    issues_closed: int


def _fetch_language_breakdown(client: GitHubClient, username: str, max_repos_pages: int = 5) -> dict[str, int]:
    """
    Returns {language_name: total_bytes} aggregated across the user's public, non-fork repositories.
    """
    query = """
    query($login: String!, $after: String) {
      user(login: $login) {
        repositories(first: 100, after: $after, ownerAffiliations: OWNER, isFork: false, orderBy: {field: PUSHED_AT, direction: DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
              edges { size node { name } }
            }
          }
        }
      }
    }
    """
    after = None
    page = 0
    totals: dict[str, int] = {}
    while page < max_repos_pages:
        try:
            data = client.graphql(query, {"login": username, "after": after})
        except RuntimeError:
            return {}

        user = (data.get("data") or {}).get("user") or {}
        repos = user.get("repositories") or {}
        for repo in repos.get("nodes") or []:
            langs = (repo.get("languages") or {}).get("edges") or []
            for edge in langs:
                name = ((edge.get("node") or {}).get("name") or "").strip()
                size = int(edge.get("size") or 0)
                if not name or size <= 0:
                    continue
                totals[name] = totals.get(name, 0) + size

        page_info = repos.get("pageInfo") or {}
        if not page_info.get("hasNextPage"):
            break
        after = page_info.get("endCursor")
        page += 1

    return totals


def _top_language_lines(lang_bytes: dict[str, int], limit: int = 4) -> list[str]:
    if not lang_bytes:
        return []
    total = sum(lang_bytes.values())
    if total <= 0:
        return []
    items = sorted(lang_bytes.items(), key=lambda kv: kv[1], reverse=True)

    lines: list[str] = []
    for name, size in items[:limit]:
        pct = int(round((size / total) * 100))
        lines.append(f"{name} - {pct}%")
    return lines


@dataclass(frozen=True)
class Scores:
    stars_score: float
    commit_activity_score: float
    repo_quality_score: float
    followers_score: float
    pr_score: float
    streak_score: float
    activity_consistency_score: float
    impact_score: float
    core_power: float
    tier: str


def _compute_current_streak(contrib_days: list[dict]) -> int:
    # contrib_days: [{date: 'YYYY-MM-DD', contributionCount: int}, ...] sorted asc.
    if not contrib_days:
        return 0
    today = _utc_today()
    contrib_map = {d["date"]: int(d["contributionCount"]) for d in contrib_days}
    streak = 0
    cursor = today
    while True:
        key = _iso_date(cursor)
        if contrib_map.get(key, 0) > 0:
            streak += 1
            cursor = cursor - dt.timedelta(days=1)
            continue
        break
    return streak


def _fetch_contributions_365(client: GitHubClient, username: str) -> ContributionStats:
    today = _utc_today()
    from_date = today - dt.timedelta(days=364)
    query = """
    query($login: String!, $from: DateTime!, $to: DateTime!) {
      user(login: $login) {
        contributionsCollection(from: $from, to: $to) {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays {
                date
                contributionCount
              }
            }
          }
        }
      }
    }
    """
    variables = {
        "login": username,
        "from": from_date.isoformat() + "T00:00:00Z",
        "to": today.isoformat() + "T23:59:59Z",
    }
    try:
        data = client.graphql(query, variables)
    except RuntimeError:
        # Allow rendering without contribution data when token is missing.
        return ContributionStats(0, 0, 365, 0)
    user = (data.get("data") or {}).get("user")
    if not user:
        raise RuntimeError(f"Could not load GitHub user '{username}' via GraphQL")

    cal = (
        user["contributionsCollection"]["contributionCalendar"]
        if user.get("contributionsCollection")
        else None
    )
    if not cal:
        return ContributionStats(0, 0, 365, 0)

    days: list[dict] = []
    for week in cal.get("weeks") or []:
        for day in week.get("contributionDays") or []:
            days.append({"date": day["date"], "contributionCount": int(day["contributionCount"])})
    days.sort(key=lambda d: d["date"])

    active_days = sum(1 for d in days if d["contributionCount"] > 0)
    current_streak = _compute_current_streak(days)
    total_contribs = int(cal.get("totalContributions") or 0)
    return ContributionStats(
        current_streak_days=current_streak,
        active_days_365=active_days,
        total_days_365=365,
        total_contributions_365=total_contribs,
    )


def _fetch_repo_stats(client: GitHubClient, username: str) -> RepoStats:
    user = client.rest_get(f"/users/{urllib.parse.quote(username)}")
    public_repos = int(user.get("public_repos") or 0)

    # Pinned items count via GraphQL (REST doesn't expose pinned).
    pin_query = """
    query($login: String!) {
      user(login: $login) {
        pinnedItems(first: 6) { totalCount }
      }
    }
    """
    pinned_count = 0
    try:
        pin_data = client.graphql(pin_query, {"login": username})
        pinned_count = int(
            ((((pin_data.get("data") or {}).get("user") or {}).get("pinnedItems") or {}).get("totalCount") or 0)
        )
    except RuntimeError:
        pinned_count = 0

    total_stars = 0
    total_forks = 0
    archived_count = 0

    # Public repos list (pagination).
    page = 1
    while True:
        repos = client.rest_get(
            f"/users/{urllib.parse.quote(username)}/repos",
            {"per_page": 100, "page": page, "type": "owner", "sort": "pushed"},
        )
        if not isinstance(repos, list) or not repos:
            break
        for r in repos:
            total_stars += int(r.get("stargazers_count") or 0)
            total_forks += int(r.get("forks_count") or 0)
            if r.get("archived"):
                archived_count += 1
        page += 1
        if page > 20:  # safety guard (2000 repos)
            break

    return RepoStats(
        public_repos=public_repos,
        total_stars=total_stars,
        total_forks=total_forks,
        archived_count=archived_count,
        pinned_count=pinned_count,
    )


def _fetch_social_stats(client: GitHubClient, username: str) -> SocialStats:
    user = client.rest_get(f"/users/{urllib.parse.quote(username)}")
    return SocialStats(
        followers=int(user.get("followers") or 0),
        following=int(user.get("following") or 0),
    )


def _fetch_collab_stats(client: GitHubClient, username: str) -> CollaborationStats:
    # Use search counts (fast and simple).
    # Note: GitHub search has rate limits; using token is strongly recommended.
    def search_count(query: str) -> int:
        data = client.rest_get("/search/issues", {"q": query, "per_page": 1})
        return int(data.get("total_count") or 0)

    try:
        prs_opened = search_count(f"author:{username} is:pr")
        prs_merged = search_count(f"author:{username} is:pr is:merged")
        issues_closed = search_count(f"author:{username} is:issue is:closed")
    except RuntimeError:
        prs_opened = 0
        prs_merged = 0
        issues_closed = 0
    return CollaborationStats(prs_opened=prs_opened, prs_merged=prs_merged, issues_closed=issues_closed)


def _score_stars(total_stars: int, max_star_threshold: int = 5000) -> float:
    return _clamp(_pct(total_stars, max_star_threshold))


def _score_followers(followers: int, follower_threshold: int = 2000) -> float:
    return _clamp(_pct(followers, follower_threshold))


def _score_commit_activity(total_contributions_365: int, max_contrib_threshold: int = 2000) -> float:
    return _clamp(_pct(total_contributions_365, max_contrib_threshold))


def _score_streak(current_streak_days: int) -> float:
    # Spec: T = (Current Streak / 365) * 100
    return _clamp(_pct(current_streak_days, 365))


def _score_activity_consistency(active_days: int, total_days: int) -> float:
    # Spec: A = (Active Days / Total Days) * 100
    return _clamp(_pct(active_days, total_days))


def _score_pr(prs_merged: int, prs_opened: int, merged_threshold: int = 200, opened_threshold: int = 400) -> float:
    # Spec: P = 0.7m + 0.3o (scores, not raw counts)
    m = _clamp(_pct(prs_merged, merged_threshold))
    o = _clamp(_pct(prs_opened, opened_threshold))
    return _clamp(0.7 * m + 0.3 * o)


def _score_repo_quality(repo: RepoStats, max_repo_threshold: int = 100) -> float:
    # Spec: R = (2r + 10f + 5a)
    # We keep the *structure* but normalize to 0-100 for Core Power combination.
    r = repo.public_repos
    f = repo.pinned_count  # "featured repos" proxy
    # "archived/popular projects factor" proxy: non-archived ratio * stars-per-repo signal (0-10 range)
    non_archived = max(0, repo.public_repos - repo.archived_count)
    non_archived_ratio = non_archived / max(1, repo.public_repos)
    stars_per_repo = repo.total_stars / max(1, repo.public_repos)
    popular_factor = min(10.0, stars_per_repo / 10.0)  # 10 stars/repo -> 1.0
    a = non_archived_ratio * popular_factor

    raw = (2 * r) + (10 * f) + (5 * a)
    # Normalize: assume "excellent" is ~100 repos + 6 pinned + a~10 => raw ~ 200 + 60 + 50 = 310
    return _clamp(_pct(raw, 310.0))


def _score_impact(total_stars: int, followers: int, forks: int, prs_merged: int, issues_closed: int) -> float:
    # Spec: Impact = 0.40S + 0.25F + 0.15K + 0.10M + 0.10I
    # These are influence scores, normalized to 0-100 with reasonable thresholds.
    s = _clamp(_pct(total_stars, 5000))
    f = _clamp(_pct(followers, 2000))
    k = _clamp(_pct(forks, 2000))
    m = _clamp(_pct(prs_merged, 200))
    i = _clamp(_pct(issues_closed, 500))
    return _clamp(0.40 * s + 0.25 * f + 0.15 * k + 0.10 * m + 0.10 * i)


def _compute_scores(
    repo: RepoStats,
    social: SocialStats,
    collab: CollaborationStats,
    contrib: ContributionStats,
) -> Scores:
    stars_score = _score_stars(repo.total_stars)
    followers_score = _score_followers(social.followers)
    commit_activity_score = _score_commit_activity(contrib.total_contributions_365)
    streak_score = _score_streak(contrib.current_streak_days)
    activity_consistency_score = _score_activity_consistency(contrib.active_days_365, contrib.total_days_365)
    pr_score = _score_pr(collab.prs_merged, collab.prs_opened)
    repo_quality_score = _score_repo_quality(repo)

    core_power = _clamp(
        0.25 * stars_score
        + 0.20 * commit_activity_score
        + 0.15 * repo_quality_score
        + 0.15 * followers_score
        + 0.10 * pr_score
        + 0.10 * streak_score
        + 0.05 * activity_consistency_score
    )

    impact_score = _score_impact(
        total_stars=repo.total_stars,
        followers=social.followers,
        forks=repo.total_forks,
        prs_merged=collab.prs_merged,
        issues_closed=collab.issues_closed,
    )

    return Scores(
        stars_score=stars_score,
        commit_activity_score=commit_activity_score,
        repo_quality_score=repo_quality_score,
        followers_score=followers_score,
        pr_score=pr_score,
        streak_score=streak_score,
        activity_consistency_score=activity_consistency_score,
        impact_score=impact_score,
        core_power=core_power,
        tier=_tier_from_core_power(core_power),
    )


def _pick_template_path(template_name: str, theme: str, prefer_txt: bool, templates_dir: str) -> str:
    # template_name can be:
    # - a direct filepath
    # - a folder name under templates/ (e.g. "Quantum Forge")
    # - a base prefix (e.g. "quantum_forge")
    if os.path.exists(template_name):
        return template_name

    norm_theme = theme.lower().strip()
    if norm_theme not in ("dark", "light"):
        raise ValueError("--theme must be 'dark' or 'light'")

    candidates: list[str] = []
    root = os.path.join(templates_dir, template_name)
    if os.path.isdir(root):
        # look for *_<theme>_txt.txt or *_<theme>_svg.svg
        for ext in (("_txt.txt" if prefer_txt else "_svg.svg"), ("_svg.svg" if prefer_txt else "_txt.txt")):
            candidates.append(os.path.join(root, f"{template_name.lower().replace(' ', '_')}_{norm_theme}{ext}"))

        # fallback: any file in that folder matching theme
        for filename in os.listdir(root):
            if norm_theme in filename.lower():
                if prefer_txt and filename.lower().endswith("_txt.txt"):
                    candidates.append(os.path.join(root, filename))
                if (not prefer_txt) and filename.lower().endswith("_svg.svg"):
                    candidates.append(os.path.join(root, filename))
                if filename.lower().endswith(".svg") or filename.lower().endswith(".svg.svg"):
                    candidates.append(os.path.join(root, filename))

    # base prefix attempt
    prefix = template_name.lower().replace(" ", "_")
    for dirpath, _, filenames in os.walk(templates_dir):
        for filename in filenames:
            lower = filename.lower()
            if prefix in lower and norm_theme in lower:
                if prefer_txt and lower.endswith("_txt.txt"):
                    candidates.append(os.path.join(dirpath, filename))
                if (not prefer_txt) and lower.endswith("_svg.svg"):
                    candidates.append(os.path.join(dirpath, filename))

    for c in candidates:
        if os.path.exists(c):
            return c

    raise FileNotFoundError(f"Could not find template '{template_name}' with theme '{theme}' under templates/")


def _render_svg(template_text: str, replacements: dict[str, str]) -> str:
    rendered = template_text
    for key, value in replacements.items():
        rendered = rendered.replace("{{" + key + "}}", value)
    return rendered


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Generate a GitBoard SVG dashboard from templates.")
    parser.add_argument("--username", default=os.getenv("GITHUB_ACTOR") or os.getenv("GITHUB_REPOSITORY_OWNER") or "", help="GitHub username")
    parser.add_argument("--template", default="Quantum Forge", help="Template folder name, base name, or path")
    parser.add_argument("--theme", default="dark", choices=["dark", "light"], help="Template theme variant")
    parser.add_argument(
        "--templates-dir",
        default=os.path.join(os.path.dirname(__file__), "templates"),
        help="Directory containing template folders (defaults to <script_dir>/templates)",
    )
    parser.add_argument("--prefer-txt", action="store_true", help="Prefer *_txt.txt templates over *_svg.svg")
    parser.add_argument("--output", default=os.path.join("assets", "my_dashboard.svg"), help="Output SVG path")
    parser.add_argument("--readme", default="README.md", help="README path to inject image link")
    parser.add_argument("--start-marker", default="<!-- Gitboard Start -->")
    parser.add_argument("--end-marker", default="<!-- Gitboard End -->")
    args = parser.parse_args(argv)

    username = args.username.strip()
    if not username:
        raise SystemExit("Missing --username (or set GITHUB_ACTOR/GITHUB_REPOSITORY_OWNER).")

    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    client = GitHubClient(token)

    social = _fetch_social_stats(client, username)
    repo = _fetch_repo_stats(client, username)
    collab = _fetch_collab_stats(client, username)
    contrib = _fetch_contributions_365(client, username)
    lang_bytes = _fetch_language_breakdown(client, username)
    lang_lines = _top_language_lines(lang_bytes, limit=4)
    scores = _compute_scores(repo, social, collab, contrib)

    template_path = _pick_template_path(
        args.template,
        args.theme,
        prefer_txt=args.prefer_txt,
        templates_dir=args.templates_dir,
    )
    template_text = _read_text(template_path)

    # Template placeholders. Some are labels-only in the SVG; we fill them anyway.
    replacements = {
        "USERNAME": username,
        "CORE_POWER": str(_round_int(scores.core_power)),
        "IMPACT": _format_percent(scores.impact_score),
        "TIER": scores.tier,
        "FOLLOWERS": str(social.followers),
        "FOLLOWING": "FOLLOWING",
        "STREAK": str(contrib.current_streak_days),
        "REPOS": str(repo.public_repos),
        "STARS": str(repo.total_stars),
        "LANG1_LINE": lang_lines[0] if len(lang_lines) > 0 else "",
        "LANG2_LINE": lang_lines[1] if len(lang_lines) > 1 else "",
        "LANG3_LINE": lang_lines[2] if len(lang_lines) > 2 else "",
        "LANG4_LINE": lang_lines[3] if len(lang_lines) > 3 else "",
    }

    svg_text = _render_svg(template_text, replacements)
    _write_text(args.output, svg_text)

    readme_path = args.readme
    readme_text = _read_text(readme_path) if os.path.exists(readme_path) else ""
    rel_svg_path = args.output.replace("\\", "/")
    dashboard_block = f"![GitBoard Dashboard]({rel_svg_path})"
    updated = _replace_between_markers(readme_text, args.start_marker, args.end_marker, dashboard_block)
    _write_text(readme_path, updated)

    print(f"Generated: {args.output}")
    print(f"Updated: {readme_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

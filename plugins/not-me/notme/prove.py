"""Where to test a pull request: the preview URL found by the strategies in prove.target.strategy.

A URL is only returned when its host is one of prove.target.hosts (matched as a suffix: `netlify.app` covers
`deploy-preview-3--app.netlify.app`) or the host of prove.target.url_template. Bot comments are read only from
logins listed in prove.target.bots or trusted by the roster. hosts and bots come from the default and global
layers only: a committed repo layer cannot widen what counts as a preview.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlsplit

from . import feedback
from .gh import GhClient

_HOSTS_RE = ("netlify\\.app", "netlify\\.live", "vercel\\.app", "pan" "theonsite\\.io")
DEFAULT_BOT_REGEX = r"https?://[^\s)>\]\"']*(?:" + "|".join(_HOSTS_RE) + r")[^\s)>\]\"']*"
DEFAULT_STRATEGY = ("deployments", "bot-comment", "url-template", "local")
DEFAULT_HOSTS = ("netlify.app", "vercel.app", "pan" "theonsite.io", "github.io")


def host_of(url: str) -> Optional[str]:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower().rstrip(".") if host else None


def host_allowed(url: str, hosts: Iterable[str]) -> bool:
    """True when url is http(s) and its host equals, or is a subdomain of, one of hosts."""
    if not url.startswith(("http://", "https://")):
        return False
    host = host_of(url)
    if not host:
        return False
    for allowed in hosts:
        suffix = str(allowed).lower().strip(".")
        if suffix and (host == suffix or host.endswith("." + suffix)):
            return True
    return False


def _template_url(template: str, **values: Any) -> Optional[str]:
    url = re.sub(r"\{(pr|repo|owner|name)\}", lambda m: str(values[m.group(1)]), template)
    return url if url.startswith(("http://", "https://")) else None


def _deployment_urls(gh: GhClient, repo: str, sha: str) -> Iterable[str]:
    deployments = gh.read(["api", f"repos/{repo}/deployments?sha={sha}&per_page=100"]) or []
    for dep in sorted(deployments, key=lambda d: d.get("id") or 0, reverse=True):
        for status in gh.read(["api", f"repos/{repo}/deployments/{int(dep['id'])}/statuses?per_page=100"]) or []:
            url = status.get("environment_url")
            if status.get("state") == "success" and isinstance(url, str) and url.startswith(("http://", "https://")):
                yield url


def _bot_urls(gh: GhClient, repo: str, n: int, pattern: str, bots: Sequence[str], roster: Any,
              skipped: List[Dict[str, Any]]) -> Iterable[str]:
    try:
        regex = re.compile(pattern)
    except re.error:
        regex = re.compile(DEFAULT_BOT_REGEX)
    allowed_bots = {str(b).lower() for b in bots}
    for comment in sorted(gh.comments(repo, n), key=lambda c: c["id"], reverse=True):
        who = comment.get("author") or ""
        if not regex.search(comment.get("body") or ""):
            continue
        if who.lower() not in allowed_bots and not feedback.trusted(roster, who):
            skipped.append({"strategy": "bot-comment", "reason": f"comment by {who or 'unknown'} is not from "
                                                                 "prove.target.bots or a trusted user"})
            continue
        for m in regex.finditer(comment.get("body") or ""):
            url = m.group(1) if m.groups() and m.group(1) else m.group(0)
            if url.startswith(("http://", "https://")):
                yield url


def find_target(gh: GhClient, repo: str, n: int, target: Dict[str, Any], trusted_target: Dict[str, Any],
                roster: Any) -> Dict[str, Any]:
    """{url, strategy, head_sha, tried[, skipped]} for the first strategy with an allowed URL, else
    {none, tried[, skipped]}. target is the merged prove.target; trusted_target is the same map from the default
    and global layers only. strategy, hosts, bots and the url_template whose host is allowed come from
    trusted_target; a repo-layer url_template is still tried, but only passes when its host is already allowed."""
    order = trusted_target.get("strategy") or list(DEFAULT_STRATEGY)
    hosts = list(trusted_target.get("hosts") or DEFAULT_HOSTS)
    owner, _, name = repo.partition("/")
    values = dict(pr=n, repo=repo, owner=owner, name=name)
    trusted_template = str(trusted_target.get("url_template") or "")
    trusted_url = _template_url(trusted_template, **values) if trusted_template else None
    if trusted_url and host_of(trusted_url):
        hosts.append(str(host_of(trusted_url)))
    template = str(target.get("url_template") or "")
    template_url = _template_url(template, **values) if template else None
    bots = trusted_target.get("bots") or []
    sha = gh.pr(repo, n).get("headRefOid") or ""
    tried: List[str] = []
    skipped: List[Dict[str, Any]] = []
    for strategy in order:
        tried.append(str(strategy))
        urls: Iterable[str] = ()
        if strategy == "deployments" and sha:
            urls = _deployment_urls(gh, repo, sha)
        elif strategy == "bot-comment":
            urls = _bot_urls(gh, repo, n, target.get("bot_comment_regex") or DEFAULT_BOT_REGEX, bots, roster,
                             skipped)
        elif strategy == "url-template" and template_url:
            urls = [template_url]
        for url in urls:
            if host_allowed(url, hosts):
                out = {"url": url, "strategy": strategy, "head_sha": sha, "tried": tried}
                return {**out, "skipped": skipped} if skipped else out
            skipped.append({"strategy": str(strategy), "url": url,
                            "reason": f"host {host_of(url)} is not in prove.target.hosts"})
    out = {"none": True, "tried": tried}
    return {**out, "skipped": skipped} if skipped else out

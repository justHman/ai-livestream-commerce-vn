"""
Shared YAML loader for GitHub Actions workflows.

PyYAML's default SafeLoader (YAML 1.1) treats the `on` key of a workflow
trigger block as boolean True, which breaks `data["on"]` lookup. This module
re-registers the bool resolver without `on/off` so the `on` key stays a
string, then exposes `load` / `safe_load` helpers.
"""

import re
from typing import Any, Dict, Optional

import yaml


class GitHubActionsLoader(yaml.SafeLoader):
    """SafeLoader with YAML 1.2-ish boolean handling (`on` stays a string)."""


# Remove the implicit bool resolver, then re-add it excluding on/off.
GitHubActionsLoader.yaml_implicit_resolvers = {
    first: [entry for entry in entries if entry[0] != "tag:yaml.org,2002:bool"]
    for first, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
}

GitHubActionsLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE|yes|Yes|YES|no|No|NO|y|Y|n|N)$"),
    list("tTfFyYnN"),
)


def safe_load(stream: str) -> Optional[Dict[str, Any]]:
    """Safely load YAML text using the GitHub Actions convention.

    Returns None on parse failure. Keys are plain Python objects; the `on`
    key stays the string `"on"`.
    """
    try:
        doc = yaml.load(stream, Loader=GitHubActionsLoader)
        if isinstance(doc, dict):
            return dict(doc)
        return doc
    except yaml.YAMLError:
        return None


def load_file(path) -> Optional[Dict[str, Any]]:
    """Load a YAML file using the GitHub Actions convention."""
    with open(path, "r", encoding="utf-8") as f:
        return safe_load(f.read())

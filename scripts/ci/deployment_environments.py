"""Canonical GitHub Actions Environment vocabulary (audit R1.3).

One canonical vocabulary across workflow `environment:` blocks, protected
GitHub Environments, and the OIDC trust subjects that must match them
exactly. Workflows MUST use these names verbatim; Terraform trust subjects
(infra/environments/global `var.github_environment`) MUST equal the same
strings for any enabled cloud deployment.

OIDC subject equality is exact (`token.actions.githubusercontent.com:sub`
== `repo:<owner>/<repo>:environment:<env>`). The actual Terraform-side
`github_environment` values are owned by Cluster B/E; this module documents
the exact subjects the workflow side expects so the tf side can align.
"""

GITHUB_ENVIRONMENT_NAMES = frozenset({"development", "staging", "production"})

# infra-apply / infra-teardown-nonprod use a dynamic `infra-${{ env }}` name
# whose `env` input is hard-allowlisted to dev|staging by each workflow.
INFRA_APPLY_ENVIRONMENT_NAMES = frozenset({"infra-dev", "infra-staging"})

SUPPORTED_ENVIRONMENT_NAMES = GITHUB_ENVIRONMENT_NAMES | INFRA_APPLY_ENVIRONMENT_NAMES

_GITHUB_REPO = "justHman/ai-livestream-commerce-vn"


def trust_subject(environment_name: str) -> str:
    """OIDC subject for a GitHub Environment (exact StringEquals match)."""
    return f"repo:{_GITHUB_REPO}:environment:{environment_name}"


def expected_trust_subjects() -> dict:
    """{environment_name: exact trust subject} the workflow side requires."""
    return {env: trust_subject(env) for env in sorted(GITHUB_ENVIRONMENT_NAMES)}

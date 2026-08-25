"""Static and Terraform-level checks for secret-safe DATABASE_URL wiring."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPUTE = ROOT / "infra/modules/compute/backend.tf"
DEV_MAIN = ROOT / "infra/environments/dev/main.tf"
DEV_VARS = ROOT / "infra/environments/dev/variables.tf"
PROD_MAIN = ROOT / "infra/environments/prod/main.tf"
PROD_VARS = ROOT / "infra/environments/prod/variables.tf"
SECRETS = ROOT / "infra/modules/secrets"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _matching_delimiter(source: str, opening: int, left: str, right: str) -> int:
    depth = 0
    quoted = False
    escaped = False
    for index in range(opening, len(source)):
        char = source[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            continue
        if char == '"':
            quoted = True
        elif char == left:
            depth += 1
        elif char == right:
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError(f"unbalanced {left}{right} block")


def _variable_block(source: str, name: str) -> str:
    start = source.index(f'variable "{name}"')
    opening = source.index("{", start)
    return source[start : _matching_delimiter(source, opening, "{", "}") + 1]


def _backend_task_definition() -> str:
    source = _read(COMPUTE)
    return source.split('resource "aws_ecs_task_definition" "backend"', 1)[1].split(
        'resource "aws_ecs_task_definition" "llm"', 1
    )[0]


def _database_url_validation_block(source: str) -> str:
    variable = _variable_block(source, "database_url_parameter_arn")
    start = variable.index("validation {")
    opening = variable.index("{", start)
    return variable[start : _matching_delimiter(variable, opening, "{", "}") + 1]


def _database_url_condition(source: str) -> str:
    validation = _database_url_validation_block(source)
    match = re.search(r"condition\s*=\s*(.*?)\n\s*error_message", validation, re.DOTALL)
    assert match, "DATABASE_URL validation must define condition before error_message"
    return match.group(1)


def _terraform_validation_module(source: str) -> str:
    return "\n".join(
        (
            _variable_block(source, "enable_database_url"),
            _variable_block(source, "database_url_parameter_arn"),
        )
    )


def _run_validation_plan(
    source: str, value: str, enabled: bool
) -> subprocess.CompletedProcess[str]:
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("terraform is required for HCL validation checks")

    with tempfile.TemporaryDirectory(prefix="database-url-validation-") as directory:
        module = Path(directory)
        (module / "main.tf").write_text(_terraform_validation_module(source), encoding="utf-8")
        init = subprocess.run(
            [terraform, "-chdir=" + str(module), "init", "-backend=false", "-input=false"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert init.returncode == 0, init.stdout + init.stderr
        return subprocess.run(
            [
                terraform,
                "-chdir=" + str(module),
                "plan",
                "-refresh=false",
                "-input=false",
                "-no-color",
                f"-var=enable_database_url={'true' if enabled else 'false'}",
                f"-var=database_url_parameter_arn={value}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )


def test_database_url_is_injected_as_ecs_secret() -> None:
    task = _backend_task_definition()
    assert re.search(
        r'name\s*=\s*"DATABASE_URL".*?valueFrom\s*=\s*var\.secrets_arns\s*\[\s*"backend/database_url"\s*\]',
        task,
        re.DOTALL,
    )


def test_database_url_is_not_in_backend_environment() -> None:
    task = _backend_task_definition()
    # environment may be a plain list or concat([...], [...]); either way the
    # captured block must never contain DATABASE_URL as a plaintext value.
    environment = re.search(
        r"environment\s*=\s*(?:concat\()?\[(.*?)\]\s*\)?\s*secrets\s*=",
        task,
        re.DOTALL,
    )
    assert environment and 'name = "DATABASE_URL"' not in environment.group(1)


def test_database_url_is_not_provisioned_by_secret_module() -> None:
    source = "\n".join(_read(path) for path in SECRETS.glob("*.tf"))
    assert "database_url" not in source.lower()


def test_database_url_validation_contract_is_structurally_gated() -> None:
    for source in (_read(DEV_VARS), _read(PROD_VARS)):
        condition = _database_url_condition(source)
        normalized = re.sub(r"\s+", " ", condition).strip()
        assert normalized.startswith(
            '(trimspace(var.database_url_parameter_arn) == "" && !var.enable_database_url) '
            "|| can(regex("
        )
        assert "trimspace(var.database_url_parameter_arn)" in condition
        assert "|| can(regex(" in normalized
        assert re.search(r'"\^arn:[^"]+\$"', condition)
        assert ":ssm:" in condition
        assert ":parameter/" in condition


@pytest.mark.parametrize(
    "value",
    [
        "postgresql://user:password@example/runtime",
        "CHANGE_ME",
        "password123",
        "database-url",
    ],
)
def test_terraform_rejects_invalid_database_url_values(value: str) -> None:
    for source in (_read(DEV_VARS), _read(PROD_VARS)):
        result = _run_validation_plan(source, value, enabled=True)
        assert result.returncode != 0, result.stdout + result.stderr


def test_terraform_accepts_valid_arn_and_disabled_empty_value() -> None:
    valid_arn = "arn:aws:ssm:ap-northeast-2:123456789012:parameter/runtime/database-url"
    for source in (_read(DEV_VARS), _read(PROD_VARS)):
        enabled = _run_validation_plan(source, valid_arn, enabled=True)
        disabled = _run_validation_plan(source, "", enabled=False)
        assert enabled.returncode == 0, enabled.stdout + enabled.stderr
        assert disabled.returncode == 0, disabled.stdout + disabled.stderr


def test_dev_database_url_is_opt_in() -> None:
    block = _variable_block(_read(DEV_VARS), "enable_database_url")
    assert re.search(r"default\s*=\s*false", block)


def test_prod_database_url_enablement_has_no_default() -> None:
    block = _variable_block(_read(PROD_VARS), "enable_database_url")
    assert "default" not in block


def test_database_url_arn_is_merged_into_backend_secret_map() -> None:
    source = _read(DEV_MAIN) + _read(PROD_MAIN)
    assert re.search(r'"backend/database_url"\s*=\s*var\.database_url_parameter_arn', source)


def _policy_statements() -> list[str]:
    # SSM execution policy lives in compute/iam.tf after the 1.61 split.
    source = _read(COMPUTE) + _read(ROOT / "infra/modules/compute/iam.tf")
    policy = source.split('resource "aws_iam_role_policy" "ecs_execution_ssm"', 1)[1].split(
        'resource "aws_iam_role" "ecs_task"', 1
    )[0]
    start = policy.index("Statement")
    opening = policy.index("[", start)
    closing = _matching_delimiter(policy, opening, "[", "]")
    body = policy[opening + 1 : closing]
    statements = []
    cursor = 0
    while cursor < len(body):
        opening = body.find("{", cursor)
        if opening == -1:
            break
        closing = _matching_delimiter(body, opening, "{", "}")
        statements.append(body[opening + 1 : closing])
        cursor = closing + 1
    return statements


def _ssm_policy_statements() -> list[tuple[str, list[str]]]:
    result = []
    for statement in _policy_statements():
        action = re.search(r"Action\s*=\s*\[(.*?)\]", statement, re.DOTALL)
        if not action:
            continue
        actions = re.findall(r'"([^"]+)"', action.group(1))
        if any(item == "*" or item.startswith("ssm:") for item in actions):
            result.append((statement, actions))
    return result


def test_every_ssm_policy_statement_uses_exact_arns_and_action() -> None:
    statements = _ssm_policy_statements()
    assert statements
    for statement, actions in statements:
        assert actions == ["ssm:GetParameters"]
        assert re.search(
            r"^\s*Resource\s*=\s*values\(var\.secrets_arns\)\s*$",
            statement,
            re.MULTILINE,
        )
        assert not re.search(r"parameter/[^\n]*\*", statement)
        assert not re.search(r"Resource\s*=\s*\"\*\"", statement)


def test_no_execution_policy_statement_grants_wildcard_action() -> None:
    for statement in _policy_statements():
        action = re.search(r"Action\s*=\s*\[(.*?)\]", statement, re.DOTALL)
        assert action and '"*"' not in action.group(1)

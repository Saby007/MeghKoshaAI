import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


def test_build_launcher_does_not_let_child_stdin_consume_remaining_script(tmp_path):
    powershell = shutil.which("pwsh")
    candidates = [Path(r"C:\Program Files\Git\bin\bash.exe"), Path(r"C:\Program Files\Git\usr\bin\bash.exe")]
    bash = next((str(path) for path in candidates if path.is_file()), shutil.which("bash"))
    if not powershell or not bash:
        pytest.skip("PowerShell and Bash are required for the exact build-launcher regression.")
    payload = tmp_path / "build-launcher.sh"
    environment = dict(os.environ, BUILDER_SOURCE=str(ROOT / "scripts" / "deploy-staged.ps1"),
                       BUILDER_PAYLOAD=str(payload))
    render = r"""
$tokens=$null; $errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($env:BUILDER_SOURCE,[ref]$tokens,[ref]$errors)
if($errors.Count) { throw ($errors | Out-String) }
$assignment=$ast.Find({
    param($node)
    $node -is [Management.Automation.Language.AssignmentStatementAst] -and
    $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
    $node.Left.VariablePath.UserPath -eq 'remote'
},$true)
if(-not $assignment) { throw 'Production build payload assignment was not found.' }
$encoded='eyJvZmZsaW5lIjp0cnVlfQ=='
$body=@'
set -euo pipefail
cat >/dev/null
printf '%s\n' 'MEGHKOSHA_BUILD_RESULT {"offline":true}'
'@
. ([scriptblock]::Create($assignment.Extent.Text))
[IO.File]::WriteAllText($env:BUILDER_PAYLOAD,$remote.Replace("`r`n","`n"),[Text.UTF8Encoding]::new($false))
"""
    rendered = subprocess.run([powershell, "-NoProfile", "-Command", render], env=environment,
                              capture_output=True, text=True, timeout=15)
    assert rendered.returncode == 0, rendered.stderr
    result = subprocess.run(
        [bash, "-c", 'exec /bin/sh "$1"', "offline-launcher", str(payload)],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'MEGHKOSHA_BUILD_RESULT {"offline":true}'


@pytest.mark.parametrize("existing_repository", [False, True])
def test_guest_failure_trap_reports_outside_redirected_build_log(tmp_path, existing_repository):
    candidates = [Path(r"C:\Program Files\Git\bin\bash.exe"), Path(r"C:\Program Files\Git\usr\bin\bash.exe")]
    bash = next((str(path) for path in candidates if path.is_file()), shutil.which("bash"))
    if not bash:
        pytest.skip("Bash is required for the full guest-recipe failure regression.")
    recipe = ROOT / "scripts" / "private-builder.sh"
    repository_files = [
        "/etc/apt/keyrings/microsoft.gpg",
        "/etc/apt/sources.list.d/azure-cli.list",
    ]
    if existing_repository:
        body = recipe.read_text()
        for index, path in enumerate(repository_files):
            fixture = tmp_path / f"repository-file-{index}"
            fixture.write_text("Existing public repository configuration.")
            body = body.replace(path, fixture.as_posix())
        recipe = tmp_path / "recipe.sh"
        recipe.write_text(body, encoding="utf-8")
    environment = dict(os.environ, OFFLINE_WORKSPACE=tmp_path.as_posix())
    stub = r"""
mktemp() { printf '%s\n' "$OFFLINE_WORKSPACE"; }
python3() {
    if [ "$#" -eq 1 ]; then
        printf '%s\n' 'REPOSITORY=offline' 'COMMIT=offline' 'REGISTRY=offline' 'CLIENTID=offline' 'NONCE=offline'
    fi
}
apt-get() { printf 'Synthetic package mirror failure\n' >&2; return 100; }
chmod() { printf 'Public repository permissions: %s\n' "$*"; command chmod "$@"; }
export -f mktemp python3 apt-get chmod
exec /bin/bash "$1"
"""
    result = subprocess.run(
        [bash, "-c", stub, "offline-recipe", str(recipe)],
        stdin=subprocess.DEVNULL, env=environment, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 100
    assert "Synthetic package mirror failure" in result.stderr
    assert 'MEGHKOSHA_BUILD_ERROR {"phase":"install-prerequisites","exitCode":100}' in result.stderr
    assert "MEGHKOSHA_BUILD_RESULT" not in result.stdout
    if existing_repository:
        for index in range(len(repository_files)):
            assert f"Public repository permissions: 0644 {(tmp_path / f'repository-file-{index}').as_posix()}" in result.stderr


def test_staged_rollout_has_offline_approval_and_attestation_regressions():
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell 7.4 is required.")
    result = subprocess.run(
        [shell, "-NoProfile", "-File", str(ROOT / "scripts" / "tests" / "test-staged-deploy.ps1")],
        capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stderr
    proof = json.loads(result.stdout.strip().splitlines()[-1])
    assert proof["liveNetworkCalls"] == 0
    assert proof["failedBootstrapBlocksRelease"]
    assert proof["staleBuildAttestationRejected"]
    assert proof["oneCommandCompletes"]
    assert proof["checkpointResumeVerified"]
    assert proof["failureCleanupVerified"]
    assert proof["numericQuotaValidationVerified"]
    assert proof["buildDiagnosticsVerified"]


def test_private_builder_shell_syntax():
    candidates = [
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files\Git\usr\bin\bash.exe"),
    ]
    shell = next((str(path) for path in candidates if path.is_file()), shutil.which("bash"))
    if not shell:
        pytest.skip("Bash syntax validation requires Bash.")
    result = subprocess.run(
        [shell, "-n", str(ROOT / "scripts" / "private-builder.sh")],
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_builder_recipe_keeps_auth_ephemeral_and_source_pinned():
    recipe = (ROOT / "scripts" / "private-builder.sh").read_text()
    assert "az login --identity --client-id" in recipe
    assert 'fetch --quiet --depth=1 origin "$COMMIT"' in recipe
    assert 'test "$(git -C "$work/source" rev-parse HEAD)" = "$COMMIT"' in recipe
    assert "--password" not in recipe
    assert "MEGHKOSHA_BUILD_RESULT" in recipe
    assert "is_private" in recipe
    assert "AZURE_CONFIG_DIR" in recipe and "DOCKER_CONFIG" in recipe
    assert "shutil.rmtree(path)" in recipe


def test_builder_infrastructure_has_no_inbound_or_vm_public_ip():
    recipe = (ROOT / "infra" / "modules" / "private-builder.bicep").read_text()
    assert "publicIPAddress" not in recipe
    assert "adminPassword" not in recipe
    assert "disablePasswordAuthentication: true" in recipe
    assert "TrustedLaunch" in recipe
    assert "AcrPush" not in recipe or "8311e382" in recipe
    core = (ROOT / "infra" / "modules" / "core.bicep").read_text()
    assert "defaultOutboundAccess: false" in core
    assert "name: 'deny-inbound'" in core
    assert "param enablePrivateBuilder bool = false" in core


def test_compiled_builder_keeps_nat_public_ip_off_the_vm():
    compiler = shutil.which("bicep") or str(Path.home() / ".azure" / "bin" / "bicep.exe")
    if not Path(compiler).is_file():
        pytest.skip("Standalone Bicep is required.")
    result = subprocess.run(
        [compiler, "build", str(ROOT / "infra" / "main.bicep"), "--stdout"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr.strip(), result.stderr
    template = json.loads(result.stdout)
    assert template["parameters"]["enablePrivateBuilder"]["defaultValue"] is False
    modules = template["resources"]
    builder = modules["privateBuilder"]["properties"]["template"]["resources"]
    values = list(builder.values()) if isinstance(builder, dict) else builder
    vm = next(item for item in values if item["type"] == "Microsoft.Compute/virtualMachines")
    nic = next(item for item in values if item["type"] == "Microsoft.Network/networkInterfaces")
    assert "publicIPAddress" not in json.dumps(nic)
    assert vm["properties"]["osProfile"]["linuxConfiguration"]["disablePasswordAuthentication"] is True
    assert vm["identity"]["type"] == "UserAssigned"
    assert vm["properties"]["securityProfile"]["uefiSettings"]["secureBootEnabled"] is True
    core = modules["core"]["properties"]["template"]["resources"]
    values = list(core.values()) if isinstance(core, dict) else core
    nsg = next(item for item in values if item["type"] == "Microsoft.Network/networkSecurityGroups")
    rule = nsg["properties"]["securityRules"][0]["properties"]
    assert rule["direction"] == "Inbound" and rule["access"] == "Deny"
    nat = next(item for item in values if item["type"] == "Microsoft.Network/natGateways")
    assert len(nat["properties"]["publicIpAddresses"]) == 1

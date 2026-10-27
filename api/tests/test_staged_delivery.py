import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


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

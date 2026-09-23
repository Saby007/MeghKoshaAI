import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _authorize_all_subscriptions_by_default():
    pass


def test_api_container_has_an_explicit_nonroot_source_only_contract():
    lines = (PROJECT_ROOT / "api" / "Dockerfile").read_text().splitlines()
    assert "USER 65532:65532" in lines
    assert "EXPOSE 8000" in lines
    assert "ENTRYPOINT []" in lines
    assert not any(line.startswith("COPY . ") for line in lines)
    assert "COPY --chown=65532:65532 brand.py ./" in lines
    assert any("--constraint constraints.txt" in line for line in lines)
    command = json.loads(next(line.removeprefix("CMD ") for line in lines if line.startswith("CMD ")))
    assert command[:4] == ["/usr/bin/python", "-m", "uvicorn", "main:app"]
    assert "--no-proxy-headers" in command
    assert command[command.index("--port") + 1] == "8000"
    assert "uvicorn[standard]" not in (PROJECT_ROOT / "api" / "requirements.txt").read_text()


def test_api_build_context_excludes_environment_state_and_test_data():
    patterns = (PROJECT_ROOT / "api" / ".dockerignore").read_text().splitlines()
    assert {".venv", ".env", ".env.*", ".azure", ".git", "tests", "__pycache__"} <= set(patterns)


def test_container_base_images_are_immutable_and_installer_is_patched():
    images = []
    for service in ("api", "web"):
        lines = (PROJECT_ROOT / service / "Dockerfile").read_text().splitlines()
        images.extend(line.split("=", 1)[1] for line in lines if line.startswith("ARG ") and "_IMAGE=" in line)
    assert len(images) == 4
    assert all(re.fullmatch(r"[a-z0-9/_.:-]+@sha256:[a-f0-9]{64}", image) for image in images)
    api_recipe = (PROJECT_ROOT / "api" / "Dockerfile").read_text()
    assert "pip==26.2.1" in api_recipe
    assert "--only-binary=:all: --target /opt/python" in api_recipe
    assert "cgr.dev/chainguard/python:latest@sha256:" in api_recipe
    assert "sys.version_info[:3] == (3, 14, 7)" in api_recipe
    runtime = api_recipe.split("AS runtime", 1)[1]
    assert "COPY --from=dependencies /opt/python /opt/python" in runtime
    assert "pip install" not in runtime and "RUN " not in runtime


def test_web_image_copies_only_built_assets_into_an_unprivileged_runtime():
    dockerfile = (PROJECT_ROOT / "web" / "Dockerfile").read_text()
    assert "RUN npm ci --no-audit --no-fund" in dockerfile
    assert "RUN npm run build" in dockerfile
    runtime = dockerfile.split("AS runtime", 1)[1]
    assert "USER 101:101" in runtime
    assert "EXPOSE 8080" in runtime
    assert "COPY --from=build --chown=101:101 /app/dist/" in runtime
    assert "node_modules" not in runtime and "npm run dev" not in runtime
    patterns = set((PROJECT_ROOT / "web" / ".dockerignore").read_text().splitlines())
    assert {"node_modules", ".env", ".env.*", "test-results", "browser"} <= patterns


def test_web_proxy_preserves_bearer_challenges_without_legacy_identity_trust():
    configuration = (PROJECT_ROOT / "web" / "nginx" / "default.conf.template").read_text()
    assert "proxy_pass https://${API_HOST};" in configuration
    assert "proxy_ssl_verify on;" in configuration
    assert "proxy_ssl_verify_depth 3;" in configuration
    assert "proxy_set_header Authorization $http_authorization;" in configuration
    assert "proxy_set_header X-MS-CLIENT-PRINCIPAL '';" in configuration
    assert "proxy_set_header X-Meghkosha-User-Token '';" in configuration
    assert "proxy_intercept_errors off;" in configuration
    assert "proxy_pass_header WWW-Authenticate;" in configuration
    assert "same-origin-allow-popups" in configuration
    assert "frame-src 'self' https://login.microsoftonline.com" in configuration
    assert "$http_authorization" not in configuration.splitlines()[0]
    assert "$request_uri" not in configuration.splitlines()[0]


def test_container_smoke_is_portable_and_uses_only_synthetic_identity():
    script = PROJECT_ROOT / "scripts" / "tests" / "container-smoke.py"
    result = subprocess.run([sys.executable, str(script), "--help"], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--api" in result.stdout and "--web" in result.stdout
    assert '"unavailableHttpsUpstream": "rejected"' in script.read_text()
    assert "invalidTlsUpstream" not in script.read_text()
    task = (script.parent / "acr-smoke.yaml").read_text()
    assert "disableWorkingDirectoryOverride: true" in task
    assert "/usr/bin/python /workspace/container-smoke.py" in task
    assert "nginx -t" in task
    assert "MEGHKOSHA_AI_ENABLED=false" in task
    assert "ignoreErrors" not in task
    assert task.count("docker image save --output") == 2
    assert task.count("--severity HIGH,CRITICAL --exit-code 1") == 2
    assert "--ignore-unfixed" not in task
    assert "aquasec/trivy:0.74.0@sha256:" in task
    assert task.count("when: [export-web-image]") == 2
    assert task.count("--format json --quiet") == 2
    assert "--cache-dir /workspace/trivy-api-cache" in task
    assert "--cache-dir /workspace/trivy-web-cache" in task
    assert "616dc9b8-b4aa-415f-8dcb-71bc462916c5" not in task


def test_runtime_test_image_keeps_production_dependencies_and_identity_isolated():
    recipe = (PROJECT_ROOT / "scripts" / "tests" / "runtime-tests.Dockerfile").read_text()
    runtime = recipe.split("FROM ${API_IMAGE}", 1)[1]
    assert "PYTHONPATH=/opt/python:/opt/test-packages:/app" in runtime
    assert "USER 65532:65532" in runtime
    assert "MEGHKOSHA_AI_ENABLED=false" in runtime
    assert "RUN " not in runtime
    assert "COPY --chown=65532:65532 tests /app/tests" in runtime
    command = json.loads(next(line.removeprefix("CMD ") for line in runtime.splitlines() if line.startswith("CMD ")))
    assert command[:3] == ["/usr/bin/python", "-m", "pytest"]
    assert "--ignore=tests/test_packaging.py" in command
    assert "--disable-socket" not in command
    assert "616dc9b8-b4aa-415f-8dcb-71bc462916c5" not in recipe


def run_input_validation(overrides=None, operation="Local"):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell 7.4 or later is required for deployment-input checks")
    environment = {name: value for name, value in os.environ.items()
                   if not name.startswith(("AZURE_", "APP_", "SERVICE_", "MEGHKOSHA_", "FOUNDRY_"))}
    environment.update(AZURE_ENV_NAME="app-local-validation")
    environment.update(overrides or {})
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(PROJECT_ROOT / "scripts" / "validate-deployment-inputs.ps1"),
         "-Operation", operation], env=environment, capture_output=True, text=True, timeout=20,
    )


@pytest.mark.parametrize("profile", ["core", "data", "ai"])
def test_each_profile_defaults_to_no_applications_processor_ai_runtime_or_export_exception(profile):
    result = run_input_validation({"APP_PROFILE": profile})
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    assert settings["profile"] == profile
    assert not settings["applicationImagesSupplied"]
    assert not settings["processorEnabled"]
    assert not settings["aiRuntimeEnabled"]
    assert not settings["chatRuntimeEnabled"]
    assert not settings["nativeExportNetworkException"]
    assert settings["cloudPreflightStillRequired"]


@pytest.mark.parametrize("operation", ["Provision", "Publish", "Deploy", "Down"])
def test_azure_operations_are_blocked_without_explicit_approval(operation):
    result = run_input_validation(operation=operation)
    assert result.returncode != 0
    assert "Azure changes are not approved" in result.stderr


@pytest.mark.parametrize("overrides, message", [
    ({"APP_PROFILE": "unknown"}, "APP_PROFILE must be"),
    ({"APP_PROVISIONED_PROFILE": "ai", "APP_PROFILE": "core"}, "Profile downgrade"),
    ({"APP_APPLICATIONS_DEPLOYED": "true"}, "Do not clear deployed"),
    ({"SERVICE_API_IMAGE_NAME": "single-image"}, "Provide both application images"),
    ({"APP_CONTAINER_SUBNET_PREFIX": "10.42.1.0/27"}, "must not overlap"),
    ({"APP_PRIVATE_ENDPOINT_SUBNET_PREFIX": "10.43.0.0/27"}, "within the VNet"),
    ({"APP_EXPORT_TRUSTED_SERVICES": "true"}, "not part of the core stage"),
    ({"APP_ENABLE_PROCESSOR": "true"}, "requires the data"),
    ({"APP_ENABLE_AI_RUNTIME": "true"}, "AI runtime requires"),
    ({"APP_ENABLE_CHAT_RUNTIME": "true"}, "Foundry chat requires"),
    ({"APP_MODEL_DEPLOYMENTS": "{}"}, "must be a JSON array"),
    ({"APP_PROFILE": "ai", "APP_MODEL_DEPLOYMENTS": '[{"name":"test"}]'}, "missing modelFormat"),
])
def test_invalid_or_unimplemented_configuration_fails_before_cloud_calls(overrides, message):
    result = run_input_validation(overrides)
    assert result.returncode != 0
    assert message in result.stderr


def test_approved_model_router_enables_chat_without_hosted_agent_narration():
    result = run_input_validation({
        "APP_PROFILE": "ai",
        "APP_MODEL_DEPLOYMENTS": json.dumps([{
            "name": "model-router",
            "modelFormat": "OpenAI",
            "modelName": "model-router",
            "modelVersion": "2025-11-18",
            "sku": "GlobalStandard",
            "capacity": 100,
        }]),
        "APP_ENABLE_CHAT_RUNTIME": "true",
        "APP_AI_VALIDATED": "true",
        "MODEL_ROUTER_DEPLOYMENT_NAME": "model-router",
    })

    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout)
    assert settings["chatRuntimeEnabled"] is True
    assert settings["aiRuntimeEnabled"] is False


def test_ai_deployment_helper_plans_previewed_ai_profile_with_processor():
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell 7.4 or later is required for deployment-helper checks")
    script = PROJECT_ROOT / "scripts" / "deploy-ai.ps1"
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(script),
         "-EnvironmentName", "fresh-ai", "-Location", "centralindia", "-PlanOnly"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["environment"] == "fresh-ai"
    assert plan["preview"] is True
    assert plan["enableProcessor"] is True
    assert plan["maxAttempts"] == 3
    assert plan["settings"] == {
        "AZURE_LOCATION": "centralindia",
        "APP_PROFILE": "ai",
        "APP_EXPORT_TRUSTED_SERVICES": "true",
        "APP_MODEL_DEPLOYMENTS": '[{"name":"model-router","modelFormat":"OpenAI","modelName":"model-router","modelVersion":"2025-11-18","sku":"GlobalStandard","capacity":100}]',
        "MODEL_ROUTER_DEPLOYMENT_NAME": "model-router",
        "APP_ENABLE_CHAT_RUNTIME": "true",
        "APP_AI_VALIDATED": "true",
        "APP_ENABLE_AI_RUNTIME": "false",
        "APP_RESTORE_AI_ACCOUNT": "false",
    }
    source = script.read_text(encoding="utf-8")
    assert "AccountProvisioningStateInvalid|Another operation is in progress" in source
    assert "resource not found: unable to find a resource with name 'ca-(api|web)-" in source
    assert "az cognitiveservices account list" in source
    assert "az cognitiveservices account show" in source
    assert "az resource list" not in source
    assert "azd env list --output json" in source
    assert "azd env select $EnvironmentName 2>$null" not in source
    assert "azd down" not in source
    assert "--purge" not in source


def test_ai_deployment_helper_plan_supports_explicit_opt_outs():
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell 7.4 or later is required for deployment-helper checks")
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(PROJECT_ROOT / "scripts" / "deploy-ai.ps1"),
         "-EnvironmentName", "fresh-ai", "-SkipPreview", "-SkipProcessor", "-MaxAttempts", "5", "-PlanOnly"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    plan = json.loads(result.stdout)
    assert plan["preview"] is False
    assert plan["enableProcessor"] is False
    assert plan["maxAttempts"] == 5


def test_ai_deployment_helper_environment_inventory_handles_empty_list():
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell 7.4 or later is required for deployment-helper checks")
    expression = """
Set-StrictMode -Version Latest
$EnvironmentName = 'fresh-ai'
function Test-Inventory([string] $Json) {
    $environments = @($Json | ConvertFrom-Json)
    @($environments | Where-Object {
        $_ -ne $null -and $_.PSObject.Properties['Name'] -ne $null -and $_.Name -eq $EnvironmentName
    }).Count -gt 0
}
@{
    empty = Test-Inventory '[]'
    present = Test-Inventory '[{"Name":"fresh-ai"}]'
    other = Test-Inventory '[{"Name":"other"}]'
} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", expression],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"empty": False, "present": True, "other": False}


def test_first_publish_allows_no_existing_digests_but_requires_approved_environment_registry():
    result = run_input_validation({"APP_ALLOW_AZURE_CHANGES": "true",
                                   "AZURE_CONTAINER_REGISTRY_ENDPOINT": "testapp.azurecr.io"}, "Publish")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["applicationImagesSupplied"] is False


@pytest.mark.parametrize("apply", [False, True])
def test_identity_bootstrap_is_offline_by_default_and_requires_explicit_apply_approval(apply):
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell is required for the bootstrap contract")
    environment = dict(os.environ, APP_ALLOW_AZURE_CHANGES="false")
    tenant = "11111111-1111-1111-1111-111111111111"
    subscription = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    command = [shell, "-NoProfile", "-NonInteractive", "-File", str(PROJECT_ROOT / "scripts" / "bootstrap-identity.ps1"),
               "-TenantId", tenant, "-SubscriptionId", subscription, "-EnvironmentName", "app-test",
               "-WebOrigin", "https://app.example.test",
               "-OboManagedIdentityResourceId", f"/subscriptions/{subscription}/resourceGroups/rg-app/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-obo"]
    if apply:
        command.append("-Apply")
    result = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=20)
    if apply:
        assert result.returncode != 0
        assert "Identity changes are not approved" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        plan = json.loads(result.stdout)
        assert plan["action"] == "Preview"
        assert plan["createsClientSecret"] is False
        assert plan["assignsSubscriptionRoles"] is False
        assert plan["administratorConsentRequested"] is False
        assert plan["callbacks"] == ["https://app.example.test/auth-callback.html"]


def test_identity_bootstrap_apply_is_idempotent_against_offline_graph_transport():
    shell = shutil.which("pwsh")
    if not shell:
        pytest.skip("PowerShell is required for the bootstrap contract")
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File",
                             str(PROJECT_ROOT / "scripts" / "tests" / "test-bootstrap.ps1")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["networkCalls"] == 0
    assert summary["idempotentCreates"]
    assert summary["rejectedConflictsBeforeWrites"] == 4
    assert summary["consentFailureRecoverable"]


def resource_map(template):
    resources = template["resources"]
    return resources if isinstance(resources, dict) else {str(index): resource for index, resource in enumerate(resources)}


def test_export_access_template_limits_roles_without_mutating_application_or_storage():
    compiler = shutil.which("bicep") or str(Path.home() / ".azure" / "bin" / "bicep.exe")
    if not Path(compiler).is_file():
        pytest.skip("Standalone Bicep is required for generated-template checks")
    result = subprocess.run([compiler, "build", str(PROJECT_ROOT / "infra" / "export-access.bicep"), "--stdout"],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert not result.stderr.strip(), result.stderr
    template = json.loads(result.stdout)
    resources = list(resource_map(template).values())
    assert {resource["type"] for resource in resources} == {
        "Microsoft.Authorization/roleDefinitions", "Microsoft.Authorization/roleAssignments", "Microsoft.Resources/deployments",
    }
    roles = [resource for resource in resources if resource["type"] == "Microsoft.Authorization/roleDefinitions"]
    assert len(roles) == 3
    expected = {
        "Export Configurator": {"Microsoft.Authorization/permissions/read", "Microsoft.CostManagement/exports/read",
                                "Microsoft.CostManagement/exports/write", "Microsoft.CostManagement/exports/action",
                                "Microsoft.CostManagement/exports/run/action"},
        "Export Executor": {"Microsoft.Authorization/permissions/read", "Microsoft.CostManagement/exports/read",
                             "Microsoft.CostManagement/exports/action", "Microsoft.CostManagement/exports/run/action"},
        "Export Storage Setup": {"Microsoft.Storage/storageAccounts/read", "Microsoft.Storage/storageAccounts/write",
                                  "Microsoft.Storage/storageAccounts/blobServices/containers/read",
                                  "Microsoft.Authorization/permissions/read", "Microsoft.Authorization/roleAssignments/read",
                                  "Microsoft.Authorization/roleAssignments/write"},
    }
    for label, actions in expected.items():
        role = next(resource["properties"] for resource in roles if label in resource["properties"]["roleName"])
        assert role["type"] == "CustomRole"
        assert len(role["permissions"]) == 1
        permissions = role["permissions"][0]
        assert set(permissions["actions"]) == actions
        assert permissions["notActions"] == permissions["dataActions"] == permissions["notDataActions"] == []
        assert len(role["assignableScopes"]) == 1
        if label == "Export Storage Setup":
            assert "Microsoft.Storage/storageAccounts" in role["assignableScopes"][0]
        else:
            assert role["assignableScopes"] == ["[subscription().id]"]
    assignments = [resource for resource in resources if resource["type"] == "Microsoft.Authorization/roleAssignments"]
    assert len(assignments) == 3
    assert template["parameters"]["enableApiCostManagementContributor"]["defaultValue"] is False
    compatibility = next(resource for resource in assignments if "condition" in resource)
    assert compatibility["condition"] == "[parameters('enableApiCostManagementContributor')]"
    assert compatibility["properties"]["principalId"] == "[parameters('apiPrincipalId')]"
    assert compatibility["properties"]["roleDefinitionId"] == "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '434105ed-43f6-45c7-a02f-909b2ba83430')]"
    assert "scope" not in compatibility
    module = next(resource for resource in resources if resource["type"] == "Microsoft.Resources/deployments")
    storage_assignments = list(resource_map(module["properties"]["template"]).values())
    assert len(storage_assignments) == 2
    assert template["parameters"]["enableApiStorageAccountContributor"]["defaultValue"] is False
    assert module["properties"]["parameters"]["enableApiStorageAccountContributor"]["value"] == "[parameters('enableApiStorageAccountContributor')]"
    storage_compatibility = next(resource for resource in storage_assignments if "condition" in resource)
    assert storage_compatibility["condition"] == "[parameters('enableApiStorageAccountContributor')]"
    assert storage_compatibility["properties"]["principalId"] == "[parameters('apiPrincipalId')]"
    assert storage_compatibility["properties"]["roleDefinitionId"] == "[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '17d1049b-9a84-46fb-8f53-869881c3d3ab')]"
    assert "Microsoft.Storage/storageAccounts" in storage_compatibility["scope"]
    assignment = next(resource for resource in storage_assignments if "condition" not in resource)
    assert assignment["type"] == "Microsoft.Authorization/roleAssignments"
    assert "Microsoft.Storage/storageAccounts" in assignment["scope"]
    assert assignment["properties"]["principalId"] == "[parameters('apiPrincipalId')]"
    assert all(resource["properties"]["principalType"] == "ServicePrincipal" for resource in assignments + storage_assignments)


@pytest.fixture(scope="module")
def compiled_profiles():
    compiler = shutil.which("bicep") or str(Path.home() / ".azure" / "bin" / "bicep.exe")
    if not Path(compiler).is_file():
        pytest.skip("Standalone Bicep is required for generated-template checks")
    profiles = {}
    for profile in ("core", "data", "ai"):
        environment = {name: value for name, value in os.environ.items()
                       if not name.startswith(("AZURE_", "APP_", "SERVICE_", "MEGHKOSHA_", "FOUNDRY_", "MODEL_ROUTER_"))}
        environment.update(AZURE_ENV_NAME="app-contract-test", APP_PROFILE=profile)
        result = subprocess.run([compiler, "build-params", str(PROJECT_ROOT / "infra" / "main.bicepparam"), "--stdout"],
                                env=environment, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert not result.stderr.strip(), result.stderr
        compiled = json.loads(result.stdout)
        profiles[profile] = (json.loads(compiled["parametersJson"]), json.loads(compiled["templateJson"]))
    return profiles


def test_compiled_stage_defaults_are_explicit_and_safe(compiled_profiles):
    for profile, (parameters, template) in compiled_profiles.items():
        values = {name: item["value"] for name, item in parameters["parameters"].items()}
        assert values["profile"] == profile
        assert values["location"] == "centralindia"
        assert values["foundryLocation"] == "eastus2"
        assert values["apiImage"] == values["webImage"] == ""
        assert values["apiClientId"] == values["webClientId"] == ""
        assert values["modelDeployments"] == []
        assert not any(values[name] for name in ("enableProcessor", "enableAiRuntime", "enableChatRuntime", "allowNativeExportTrustedServices"))
        modules = resource_map(template)
        assert "condition" not in modules["core"]
        assert modules["data"]["condition"] == "[variables('dataEnabled')]"
        assert modules["ai"]["condition"] == "[variables('aiEnabled')]"
        assert modules["apps"]["condition"] == "[variables('deployApplications')]"
        assert modules["processor"]["condition"] == "[variables('deployProcessor')]"
        assert template["variables"]["dataEnabled"] == "[contains(createArray('data', 'ai'), parameters('profile'))]"
        assert template["variables"]["aiEnabled"] == "[equals(parameters('profile'), 'ai')]"


def test_compiled_core_and_apps_enforce_identity_and_ingress_boundaries(compiled_profiles):
    _, template = compiled_profiles["core"]
    modules = resource_map(template)
    core = list(resource_map(modules["core"]["properties"]["template"]).values())
    core_types = {resource["type"] for resource in core}
    assert not any(resource_type.startswith(("Microsoft.Storage/", "Microsoft.CognitiveServices/")) for resource_type in core_types)
    registry = next(resource for resource in core if resource["type"] == "Microsoft.ContainerRegistry/registries")
    assert registry["properties"]["adminUserEnabled"] is False
    environment = next(resource for resource in core if resource["type"] == "Microsoft.App/managedEnvironments")
    assert environment["properties"]["appLogsConfiguration"] == {"destination": "azure-monitor"}
    assert "sharedKey" not in json.dumps(core)
    apps = list(resource_map(modules["apps"]["properties"]["template"]).values())
    api_environment = modules["apps"]["properties"]["parameters"]["apiEnvironment"]["value"]
    assert next(item for item in api_environment if item["name"] == "FOUNDRY_CHAT_ENABLED")["value"] == "[string(and(and(variables('aiEnabled'), parameters('enableChatRuntime')), not(empty(parameters('modelRouterDeploymentName')))))]"
    api = next(resource for resource in apps if "ca-api-" in resource["name"])
    web = next(resource for resource in apps if "ca-web-" in resource["name"])
    assert api["properties"]["configuration"]["ingress"]["external"] is False
    assert web["properties"]["configuration"]["ingress"]["external"] is True
    for app in (api, web):
        assert app["identity"]["type"] == "UserAssigned"
        assert app["properties"]["workloadProfileName"] == "Consumption"
        assert app["properties"]["configuration"]["ingress"]["allowInsecure"] is False
        assert len(app["properties"]["template"]["containers"][0]["probes"]) == 3


def test_compiled_data_ai_and_processor_do_not_add_queues_or_implicit_credentials(compiled_profiles):
    _, template = compiled_profiles["ai"]
    modules = resource_map(template)
    data = list(resource_map(modules["data"]["properties"]["template"]).values())
    storage = next(resource for resource in data if resource["type"] == "Microsoft.Storage/storageAccounts")
    assert storage["properties"]["isHnsEnabled"] is False
    assert storage["properties"]["allowSharedKeyAccess"] is False
    assert storage["properties"]["allowBlobPublicAccess"] is False
    assert storage["properties"]["networkAcls"]["defaultAction"] == "Deny"
    assert storage["properties"]["publicNetworkAccess"] == "[if(parameters('allowNativeExportTrustedServices'), 'Enabled', 'Disabled')]"
    ai = list(resource_map(modules["ai"]["properties"]["template"]).values())
    account = next(resource for resource in ai if resource["type"] == "Microsoft.CognitiveServices/accounts")
    assert account["location"] == "[parameters('foundryLocation')]"
    assert account["properties"]["disableLocalAuth"] is True
    assert account["properties"]["publicNetworkAccess"] == "Disabled"
    endpoint = next(resource for resource in ai if resource["type"] == "Microsoft.Network/privateEndpoints")
    assert endpoint["location"] == "[parameters('networkLocation')]"
    processor = list(resource_map(modules["processor"]["properties"]["template"]).values())
    job = next(resource for resource in processor if resource["type"] == "Microsoft.App/jobs")
    assert job["properties"]["configuration"]["triggerType"] == "Schedule"
    assert job["properties"]["configuration"]["scheduleTriggerConfig"]["parallelism"] == 1


def test_operational_model_router_template_only_targets_the_existing_account_child():
    compiler = shutil.which("bicep") or str(Path.home() / ".azure" / "bin" / "bicep.exe")
    if not Path(compiler).is_file():
        pytest.skip("Standalone Bicep is required for generated-template checks")
    result = subprocess.run(
        [compiler, "build", str(PROJECT_ROOT / "infra" / "model-router.bicep"), "--stdout"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    template = json.loads(result.stdout)
    resources = list(resource_map(template).values())
    assert len(resources) == 1
    deployment = resources[0]
    assert deployment["type"] == "Microsoft.CognitiveServices/accounts/deployments"
    assert deployment["name"] == "[format('{0}/{1}', parameters('accountName'), 'model-router')]"
    assert deployment["sku"] == {"name": "GlobalStandard", "capacity": 100}
    assert deployment["properties"]["model"] == {
        "format": "OpenAI", "name": "model-router", "version": "2025-11-18",
    }
    assert deployment["properties"]["versionUpgradeOption"] == "NoAutoUpgrade"
    all_text = json.dumps(template)
    for forbidden in ("Microsoft.ServiceBus/", "queueServices/queues", "Microsoft.DocumentDB/", "Microsoft.Synapse/", "Microsoft.Kusto/"):
        assert forbidden not in all_text
    assert "passwordCredentials" not in all_text
    assert "616dc9b8-b4aa-415f-8dcb-71bc462916c5" not in all_text
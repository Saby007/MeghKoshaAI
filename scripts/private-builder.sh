#!/usr/bin/env bash
set -euo pipefail
umask 077

work="$(mktemp -d /var/tmp/meghkosha-build.XXXXXXXX)"
export AZURE_CONFIG_DIR="$work/azure"
export DOCKER_CONFIG="$work/docker"
mkdir -p "$AZURE_CONFIG_DIR" "$DOCKER_CONFIG"
log="$work/build.log"
finish() {
    status=$?
    if [ "$status" -ne 0 ]; then
        printf 'Private build failed (exit %s).\n' "$status" >&2
        tail -n 20 "$log" >&2 || true
    fi
    python3 - "$work" <<'PY'
import pathlib, shutil, sys
path = pathlib.Path(sys.argv[1])
if path.parent != pathlib.Path("/var/tmp") or not path.name.startswith("meghkosha-build."):
    raise SystemExit("Unsafe build workspace cleanup refused.")
shutil.rmtree(path)
PY
    exit "$status"
}
trap finish EXIT

assignments="$(python3 - <<'PY'
import base64, json, os, re, shlex
config = json.loads(base64.b64decode(os.environ["MEGHKOSHA_BUILD_CONFIG"], validate=True))
patterns = {
    "repository": r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git",
    "commit": r"[a-f0-9]{40}",
    "registry": r"[a-z0-9]+\.azurecr\.io",
    "clientId": r"[a-f0-9-]{36}",
    "nonce": r"[a-f0-9]{32}",
}
for key, pattern in patterns.items():
    if not re.fullmatch(pattern, config[key]):
        raise SystemExit("Invalid private build configuration: " + key)
    print(key.upper() + "=" + shlex.quote(config[key]))
PY
)" || exit 1
eval "$assignments"

{
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y ca-certificates curl gnupg git docker.io python3
    install -d -m 0755 /etc/apt/keyrings
    curl --fail --silent --show-error https://packages.microsoft.com/keys/microsoft.asc |
        gpg --dearmor --yes -o /etc/apt/keyrings/microsoft.gpg
    . /etc/os-release
    printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/microsoft.gpg] https://packages.microsoft.com/repos/azure-cli/ %s main\n' \
        "$VERSION_CODENAME" > /etc/apt/sources.list.d/azure-cli.list
    apt-get update
    apt-get install -y azure-cli
    systemctl start docker
    python3 - "$REGISTRY" <<'PY'
import ipaddress, socket, sys
addresses = {item[4][0] for item in socket.getaddrinfo(sys.argv[1], 443)}
if not addresses or any(not ipaddress.ip_address(address).is_private for address in addresses):
    raise SystemExit("Private ACR DNS did not resolve exclusively to private addresses.")
PY
    az login --identity --client-id "$CLIENTID" --output none
    authenticated=false
    for attempt in 1 2 3 4 5; do
        if az acr login --name "${REGISTRY%%.*}"; then authenticated=true; break; fi
        sleep "$((attempt * 15))"
    done
    if [ "$authenticated" != true ]; then
        printf 'Managed-identity ACR login failed after bounded RBAC propagation retries.\n' >&2
        exit 1
    fi
    git init --quiet "$work/source"
    git -C "$work/source" remote add origin "$REPOSITORY"
    GIT_TERMINAL_PROMPT=0 git -C "$work/source" fetch --quiet --depth=1 origin "$COMMIT"
    git -C "$work/source" checkout --quiet --detach FETCH_HEAD
    test "$(git -C "$work/source" rev-parse HEAD)" = "$COMMIT"
    for service in api web; do
        docker build --platform linux/amd64 --label "org.opencontainers.image.revision=$COMMIT" \
            --file "$work/source/$service/Dockerfile" \
            --tag "$REGISTRY/meghkoshaai-$service:$COMMIT" "$work/source/$service"
        docker push "$REGISTRY/meghkoshaai-$service:$COMMIT"
    done
} >> "$log" 2>&1

export BUILD_COMMIT="$COMMIT" BUILD_REGISTRY="$REGISTRY" BUILD_NONCE="$NONCE"
export API_DIGEST="$(az acr repository show --name "${REGISTRY%%.*}" --image "meghkoshaai-api:$COMMIT" --query digest --output tsv)"
export WEB_DIGEST="$(az acr repository show --name "${REGISTRY%%.*}" --image "meghkoshaai-web:$COMMIT" --query digest --output tsv)"
python3 - <<'PY'
import json, os, re
for name in ("API_DIGEST", "WEB_DIGEST"):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", os.environ[name]):
        raise SystemExit("Registry did not return an immutable image digest.")
registry = os.environ["BUILD_REGISTRY"]
result = {
    "commit": os.environ["BUILD_COMMIT"], "nonce": os.environ["BUILD_NONCE"],
    "registry": registry,
    "apiImage": registry + "/meghkoshaai-api@" + os.environ["API_DIGEST"],
    "webImage": registry + "/meghkoshaai-web@" + os.environ["WEB_DIGEST"],
}
print("MEGHKOSHA_BUILD_RESULT " + json.dumps(result, separators=(",", ":")))
PY

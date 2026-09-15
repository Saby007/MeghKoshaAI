import asyncio
import json
from types import SimpleNamespace

import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceExistsError, ResourceModifiedError, ResourceNotFoundError

from services import finops_actions


class Downloader:
    def __init__(self, value, etag=None):
        self.value = value
        self.properties = SimpleNamespace(etag=etag)

    def readall(self):
        return self.value


class Container:
    def __init__(self):
        self.blobs = {}
        self.uploads = []
        self.etags = {}
        self.before_commit = None

    def upload_blob(self, name, value, overwrite, content_settings, **kwargs):
        if "/latest/" in name and self.before_commit:
            callback, self.before_commit = self.before_commit, None
            callback()
        if not overwrite and name in self.blobs:
            raise ResourceExistsError("exists")
        if overwrite:
            assert kwargs.get("match_condition") == MatchConditions.IfNotModified
            if kwargs.get("etag") != self.etags.get(name):
                raise ResourceModifiedError("etag mismatch")
        self.blobs[name] = value
        self.etags[name] = f'"{len(self.uploads) + 1}"'
        self.uploads.append((name, overwrite))

    def list_blobs(self, name_starts_with):
        return [SimpleNamespace(name=name) for name in self.blobs if name.startswith(name_starts_with)]

    def download_blob(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("missing")
        return Downloader(self.blobs[name], self.etags.get(name))


def test_action_state_writes_immutable_event_before_latest_and_lists(monkeypatch):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    state = finops_actions.build_state(
        scope_hash="scope-1",
        action_id="unattached_disks",
        status="completed",
        owner="owner@example.com",
        due_date="2026-08-31",
        realized_saving_month=125.5,
        note="Validated removal.",
        actor="tenant:user",
        updated_at="2026-08-17T12:00:00+00:00",
    )

    saved = asyncio.run(finops_actions.save_action(state, expected_version=0))

    assert "/events/" in container.uploads[0][0]
    assert container.uploads[0][1] is False
    assert container.uploads[1] == ("actions/scope-1/latest/unattached_disks.json", False)
    loaded = asyncio.run(finops_actions.list_actions("scope-1"))
    assert loaded == [saved]
    assert saved.version == 1
    assert loaded[0].completed_at == "2026-08-17T12:00:00+00:00"


def _state(note):
    return finops_actions.build_state(scope_hash="scope-1", action_id="unattached_disks", status="open",
        owner="owner", due_date=None, realized_saving_month=None, note=note, actor="tenant:user",
        updated_at="2026-09-07T10:00:00+00:00")


def test_stale_editor_cannot_overwrite_a_committed_action(monkeypatch):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    first = asyncio.run(finops_actions.save_action(_state("first"), expected_version=0))
    second = asyncio.run(finops_actions.save_action(_state("second"), expected_version=first.version))
    with pytest.raises(finops_actions.ActionConflictError):
        asyncio.run(finops_actions.save_action(_state("stale"), expected_version=first.version))
    assert asyncio.run(finops_actions.list_actions("scope-1")) == [second]
    history = asyncio.run(finops_actions.list_action_history("scope-1", "unattached_disks"))
    assert [item.note for item in history] == ["second", "first"]


def test_concurrent_commit_uses_etag_and_excludes_losing_audit_attempt(monkeypatch):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    first = asyncio.run(finops_actions.save_action(_state("first"), expected_version=0))
    container.before_commit = lambda: finops_actions._save(_state("winner"), first.version)
    with pytest.raises(finops_actions.ActionConflictError):
        asyncio.run(finops_actions.save_action(_state("loser"), expected_version=first.version))
    assert asyncio.run(finops_actions.list_actions("scope-1"))[0].note == "winner"
    assert [item.note for item in asyncio.run(finops_actions.list_action_history("scope-1", "unattached_disks"))] == ["winner", "first"]


def test_legacy_action_is_upgraded_with_preserved_baseline(monkeypatch):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    path = "actions/scope-1/latest/unattached_disks.json"
    legacy = _state("legacy").model_dump(by_alias=True)
    legacy.pop("version", None)
    container.blobs[path] = json.dumps(legacy).encode()
    container.etags[path] = '"legacy"'
    assert asyncio.run(finops_actions.list_actions("scope-1"))[0].version == 0
    saved = asyncio.run(finops_actions.save_action(_state("updated"), expected_version=0))
    assert saved.version == 1
    history = asyncio.run(finops_actions.list_action_history("scope-1", "unattached_disks"))
    assert [item.note for item in history] == ["updated", "legacy"]


def test_audit_failure_leaves_committed_state_unchanged(monkeypatch):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    first = asyncio.run(finops_actions.save_action(_state("first"), expected_version=0))
    original_upload = container.upload_blob

    def failed_audit(name, *args, **kwargs):
        if "/events/" in name:
            raise RuntimeError("Storage unavailable")
        return original_upload(name, *args, **kwargs)

    monkeypatch.setattr(container, "upload_blob", failed_audit)
    with pytest.raises(RuntimeError):
        asyncio.run(finops_actions.save_action(_state("not saved"), expected_version=1))
    assert asyncio.run(finops_actions.list_actions("scope-1")) == [first]


def test_completion_time_survives_note_edit_and_resets_on_reopen(monkeypatch):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    complete = _state("done").model_copy(update={"status": "completed", "completed_at": "2026-09-01T00:00:00Z"})
    first = asyncio.run(finops_actions.save_action(complete, expected_version=0))
    edit = complete.model_copy(update={"note": "evidence", "completed_at": "2026-09-07T00:00:00Z"})
    second = asyncio.run(finops_actions.save_action(edit, expected_version=1))
    assert second.completed_at == first.completed_at
    reopened = asyncio.run(finops_actions.save_action(_state("reopened"), expected_version=2))
    assert reopened.completed_at is None


def test_corrupt_action_is_not_silently_treated_as_empty(monkeypatch):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    path = "actions/scope-1/latest/unattached_disks.json"
    container.blobs[path] = b"{}"
    with pytest.raises(finops_actions.ActionIntegrityError):
        asyncio.run(finops_actions.list_actions("scope-1"))
    with pytest.raises(finops_actions.ActionIntegrityError):
        asyncio.run(finops_actions.save_action(_state("overwrite"), expected_version=0))
    assert container.uploads == []


def test_two_first_saves_cannot_both_create_the_same_action(monkeypatch):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    container.before_commit = lambda: finops_actions._save(_state("winner"), 0)
    with pytest.raises(finops_actions.ActionConflictError):
        asyncio.run(finops_actions.save_action(_state("loser"), expected_version=0))
    assert [state.note for state in asyncio.run(finops_actions.list_action_history("scope-1", "unattached_disks"))] == ["winner"]


@pytest.mark.parametrize("damage", ["missing", "modified"])
def test_damaged_committed_audit_head_blocks_further_saves(monkeypatch, damage):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    asyncio.run(finops_actions.save_action(_state("first"), expected_version=0))
    path = "actions/scope-1/latest/unattached_disks.json"
    original_head = container.blobs[path]
    event = json.loads(original_head)["eventBlob"]
    if damage == "missing":
        del container.blobs[event]
    else:
        container.blobs[event] = b"{}"
    with pytest.raises(finops_actions.ActionIntegrityError):
        asyncio.run(finops_actions.save_action(_state("update"), expected_version=1))
    assert container.blobs[path] == original_head


def test_ambiguous_commit_failure_is_recovered_by_read_not_blind_retry(monkeypatch):
    container = Container()
    monkeypatch.setattr(finops_actions, "get_container_client", lambda: container)
    upload = container.upload_blob

    def lost_response(name, *args, **kwargs):
        upload(name, *args, **kwargs)
        if "/latest/" in name:
            raise RuntimeError("Response lost after commit")

    monkeypatch.setattr(container, "upload_blob", lost_response)
    with pytest.raises(RuntimeError):
        asyncio.run(finops_actions.save_action(_state("committed"), expected_version=0))
    monkeypatch.setattr(container, "upload_blob", upload)
    assert asyncio.run(finops_actions.list_actions("scope-1"))[0].version == 1
    with pytest.raises(finops_actions.ActionConflictError):
        asyncio.run(finops_actions.save_action(_state("duplicate"), expected_version=0))
    assert len(asyncio.run(finops_actions.list_action_history("scope-1", "unattached_disks"))) == 1


def test_installed_blob_sdk_sends_conditional_headers_without_network():
    from azure.core.pipeline.transport import HttpResponse, HttpTransport
    from azure.storage.blob import ContainerClient, ContentSettings

    requests = []

    class Response(HttpResponse):
        def __init__(self, request):
            super().__init__(request, None)
            self.status_code = 201
            self.headers = {"etag": '"committed"', "x-ms-request-id": "test-request"}

        def body(self):
            return b""

    class Transport(HttpTransport):
        def open(self):
            pass

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def send(self, request, **kwargs):
            requests.append(request)
            return Response(request)

    with ContainerClient("https://example.blob.core.windows.net", "reports", credential=None, transport=Transport()) as container:
        settings = ContentSettings(content_type="application/json")
        container.upload_blob("actions/test/latest/disk.json", b"{}", overwrite=False, content_settings=settings)
        container.upload_blob("actions/test/latest/disk.json", b"{}", overwrite=True, content_settings=settings,
                              etag='"observed"', match_condition=MatchConditions.IfNotModified)
    assert len(requests) == 2
    assert requests[0].headers["If-None-Match"] == "*"
    assert requests[1].headers["If-Match"] == '"observed"'
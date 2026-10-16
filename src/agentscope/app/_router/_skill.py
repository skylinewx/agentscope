# -*- coding: utf-8 -*-
"""Skill router — the user's own library of installed skills.

The skill counterpart of :mod:`._mcp`: this is the user-level collection
an install lands in, distinct from ``/workspace/skill``, which manages
the skills present in one session's workspace.
"""

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from typing import AsyncIterator, Literal

from ..deps import (
    get_current_user_id,
    get_storage,
    get_resource_service,
    get_skill_hubs,
)
from ..hub import SkillHubBase
from .._service import ResourceService
from ..storage import SkillRecord, StorageBase
from ..resources import StoredSkill
from .._service._workspace import (
    UploadManifest,
    WorkspaceService,
)
from ._schema import SkillView, SkillDetail
from ._schema._hub_skill import SaveSkillFilesRequest, SkillFileView

skill_router = APIRouter(prefix="/skill", tags=["skill"])


def _package_error_message(exc: ValueError | OSError) -> str:
    """Keep filesystem details private while explaining invalid input."""
    if isinstance(exc, OSError):
        return (
            "The Skill package could not be read or saved. Check file paths."
        )
    return str(exc)


async def _owned_skill(
    storage: StorageBase, user_id: str, skill_id: str
) -> SkillRecord:
    """Resolve records only within the caller's library."""
    record = await storage.get_skill(user_id, skill_id)
    if record is None:
        raise HTTPException(404, "Skill is not in your library.")
    return record


async def _save_package(
    storage: StorageBase,
    user_id: str,
    package: StoredSkill,
    version: str | None,
    record: SkillRecord | None = None,
) -> SkillView:
    """Commit a validated user package while preserving ID and switches."""
    existing = await storage.get_skill_by_name(user_id, package.name)
    if existing and (record is None or existing.id != record.id):
        raise HTTPException(409, "A Skill with this name is already saved.")
    if record is None:
        record = SkillRecord(user_id=user_id, name=package.name)
    if (
        version
        and record.version == version
        and record.artifact_id != package.artifact_id
    ):
        raise HTTPException(409, "New Skill contents require a new version.")
    record.name = package.name
    record.description = package.description
    record.markdown = package.markdown
    next_version = version or (
        record.version
        if record.artifact_id == package.artifact_id and record.version
        else package.artifact_id[:12]
    )
    record.update_version(package.artifact_id, next_version)
    record.hub_id = record.card_id = None
    record.display_name = None
    try:
        await storage.upsert_skill(user_id, record)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return SkillView.from_record(record)


async def _folder_package(
    manifest: str, files: list[UploadFile], resources: ResourceService
) -> StoredSkill:
    """Use the same bounded folder importer as workspace uploads."""
    try:
        parsed = UploadManifest.model_validate_json(manifest)
        WorkspaceService.validate_manifest(parsed, len(files))
        return await resources.skill_store.capture_archive(
            WorkspaceService.tar_stream(parsed, files), "tar", "skill"
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(
            422, "Invalid Skill folder: " + _package_error_message(exc)
        ) from exc


async def _archive_chunks(archive: UploadFile) -> AsyncIterator[bytes]:
    """Stream one uploaded package without buffering it in full."""
    while chunk := await archive.read(65536):
        yield chunk


@skill_router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: SaveSkillFilesRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> SkillView:
    """Create a Skill from authored UTF-8 files without a workspace."""
    try:
        package = await resources.skill_store.save_files(body.files)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, _package_error_message(exc)) from exc
    return await _save_package(storage, user_id, package, body.version)


@skill_router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_library_skill(
    manifest: str = Form(...),
    files: list[UploadFile] = File(...),
    version: str | None = Query(default=None, min_length=1),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> SkillView:
    """Upload a complete folder directly into the user's Skill library."""
    package = await _folder_package(manifest, files, resources)
    return await _save_package(storage, user_id, package, version)


@skill_router.post("/archive", status_code=status.HTTP_201_CREATED)
async def create_skill_archive(
    archive: UploadFile = File(...),
    archive_format: Literal["zip", "tar", "tar.gz"] = Query(...),
    version: str | None = Query(default=None, min_length=1),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> SkillView:
    """Import a complete archive independently of hubs and workspaces."""
    try:
        package = await resources.skill_store.capture_archive(
            _archive_chunks(archive), archive_format, "skill"
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(422, _package_error_message(exc)) from exc
    return await _save_package(storage, user_id, package, version)


@skill_router.get("/{skill_id}/files")
async def list_skill_files(
    skill_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> list[SkillFileView]:
    """List the saved package, retaining binary files as upload-managed."""
    record = await _owned_skill(storage, user_id, skill_id)
    if not record.artifact_id:
        raise HTTPException(409, "Upload a complete Skill package first.")
    try:
        return [
            SkillFileView(**item)
            for item in await resources.skill_store.list_files(
                record.artifact_id
            )
        ]
    except (ValueError, OSError) as exc:
        raise HTTPException(
            409, "The saved Skill package is unavailable."
        ) from exc


@skill_router.get("/{skill_id}/file")
async def read_skill_file(
    skill_id: str,
    path: str = Query(...),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> dict[str, str]:
    """Read one owned UTF-8 file for the editor."""
    record = await _owned_skill(storage, user_id, skill_id)
    if not record.artifact_id:
        raise HTTPException(409, "Upload a complete Skill package first.")
    try:
        content = await resources.skill_store.read_file(
            record.artifact_id, path
        )
    except FileNotFoundError as exc:
        raise HTTPException(404, "Skill file is unavailable.") from exc
    except (ValueError, OSError) as exc:
        raise HTTPException(422, _package_error_message(exc)) from exc
    return {"content": content}


@skill_router.patch("/{skill_id}/files")
async def edit_skill_files(
    skill_id: str,
    body: SaveSkillFilesRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> SkillView:
    """Publish an immutable package version, preserving workspace copies."""
    record = await _owned_skill(storage, user_id, skill_id)
    if not record.artifact_id:
        raise HTTPException(409, "Upload a complete Skill package first.")
    try:
        package = await resources.skill_store.save_files(
            body.files, record.artifact_id
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(422, _package_error_message(exc)) from exc
    return await _save_package(storage, user_id, package, body.version, record)


@skill_router.post("/{skill_id}/upload")
async def replace_skill_folder(
    skill_id: str,
    manifest: str = Form(...),
    files: list[UploadFile] = File(...),
    version: str | None = Query(default=None, min_length=1),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> SkillView:
    """Explicitly replace a saved package using a complete folder."""
    record = await _owned_skill(storage, user_id, skill_id)
    package = await _folder_package(manifest, files, resources)
    return await _save_package(storage, user_id, package, version, record)


@skill_router.get("")
async def list_skills(
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> list[SkillView]:
    """Return every skill the user has installed, ordered by name."""
    records = await storage.list_skills(user_id)
    return sorted(
        (SkillView.from_record(r) for r in records),
        key=lambda view: view.name,
    )


@skill_router.get("/{skill_id}")
async def get_skill(
    skill_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> SkillDetail:
    """Return one installed skill, including its ``SKILL.md`` body."""
    record = await storage.get_skill(user_id, skill_id)
    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No installed skill with id {skill_id!r}.",
        )
    return SkillDetail(
        **SkillView.from_record(record).model_dump(), markdown=record.markdown
    )


@skill_router.post("/{skill_id}/upgrade")
async def upgrade_skill(
    skill_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
    hubs: dict[str, SkillHubBase] = Depends(get_skill_hubs),
) -> SkillView:
    """Upgrade the library package; workspace copies require explicit apply."""
    record = await storage.get_skill(user_id, skill_id)
    if record is None:
        raise HTTPException(404, "Skill is not in your library.")
    try:
        hub = hubs.get(record.hub_id or "")
        if hub is None or record.card_id is None:
            raise ValueError("This Skill has no available upgrade source.")
        card = await hub.get_skill(user_id, record.card_id)
        archive = await hub.download(user_id, record.card_id, card.version)
        package = await resources.skill_store.capture_archive(
            archive.stream, archive.format, record.name
        )
        record.update_version(
            package.artifact_id, card.version or package.artifact_id[:12]
        )
        record.markdown = package.markdown
        for name in (
            "display_name",
            "description",
            "tags",
            "author",
            "icon_url",
            "url",
        ):
            setattr(record, name, getattr(card, name))
        await storage.upsert_skill(user_id, record)
    except (ValueError, KeyError, OSError) as exc:
        raise HTTPException(409, str(exc)) from exc
    return SkillView.from_record(record)


@skill_router.post("/{skill_id}/package")
async def replace_skill_package(
    skill_id: str,
    archive: UploadFile = File(...),
    archive_format: Literal["zip", "tar", "tar.gz"] = Query(...),
    version: str | None = Query(default=None, min_length=1),
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> SkillView:
    """Publish a new saved package for a manually installed Skill."""
    record = await storage.get_skill(user_id, skill_id)
    if record is None:
        raise HTTPException(404, "Skill is not in your library.")
    try:
        package = await resources.skill_store.capture_archive(
            _archive_chunks(archive), archive_format, record.name
        )
    except (ValueError, OSError) as exc:
        raise HTTPException(422, _package_error_message(exc)) from exc
    return await _save_package(storage, user_id, package, version, record)


@skill_router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
    resources: ResourceService = Depends(get_resource_service),
) -> None:
    """Remove a skill from the user's library.

    Workspace files are retained, but new runtimes exclude the uninstalled
    record. Every user-owned resource can be removed explicitly.
    """
    if not await storage.delete_skill(user_id, skill_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No installed skill with id {skill_id!r}.",
        )

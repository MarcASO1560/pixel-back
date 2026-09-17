"""Authoritative image operations: merge against current state, never stale snapshots."""

import hashlib
import json
import math
import re
import zlib
from copy import deepcopy
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from sqlalchemy import delete, exists, func
from sqlalchemy.orm import load_only
from sqlmodel import Session, select

from app.crud import PROJECT_EDIT_ROLES, get_project_or_404, publish_project_event
from app.models import (
    ImageCanvasTransform,
    ImageHistoryEntry,
    ImageHistoryState,
    ImageOperationReceipt,
    ProjectResource,
    ProjectResourceDetail,
    ResourceType,
)
from app.time import utc_now

MAX_LAYERS = 128
MAX_ACTIONS = 256
MAX_PIXEL_CHANGES = 262_144
MAX_OPERATION_BYTES = 3 * 1024 * 1024
MAX_HISTORY_ENTRIES = 100
MAX_HISTORY_BYTES = 32 * 1024 * 1024
# Vercel limits both request and response payloads to 4.5 MB. Reserve room
# below that limit for the complete canonical acknowledgement, not just pixels.
MAX_CANONICAL_RESPONSE_BYTES = 4_000_000
LEGACY_LAYER_ID = "legacy-layer-1"
Color = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")] | None
Identifier = Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")]
# Existing imported v2 documents allow arbitrary nonblank Unicode layer IDs.
# Bound incoming packets by total bytes rather than rejecting their identities.
LayerIdentifier = Annotated[str, Field(min_length=1, pattern=r"\S")]
Dimension = Annotated[StrictInt, Field(ge=1, le=256)]
PixelIndex = Annotated[StrictInt, Field(ge=0, le=65_535)]
PixelChange = tuple[PixelIndex, Color] | tuple[PixelIndex, Color, Color]


class OperationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageLayer(OperationModel):
    id: LayerIdentifier
    name: str = Field(min_length=1, pattern=r"\S")
    visible: StrictBool
    locked: StrictBool
    opacity: float = Field(ge=0, le=1, allow_inf_nan=False)
    pixels: list[Color] = Field(min_length=1, max_length=65_536)


class ImageDocument(OperationModel):
    version: Literal[2]
    width: Dimension
    height: Dimension
    # A multilayer image may contain more unique colors than one layer's pixel
    # count. Packet byte limits bound replacements without making a correctly
    # accepted add-layer operation unreadable on the next request.
    palette: list[Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")]]
    layers: list[ImageLayer] = Field(min_length=1, max_length=MAX_LAYERS)

    @model_validator(mode="after")
    def validate_layers(self) -> "ImageDocument":
        if len({layer.id for layer in self.layers}) != len(self.layers):
            raise ValueError("Layer identifiers must be unique")
        if any(len(layer.pixels) != self.width * self.height for layer in self.layers):
            raise ValueError("Every layer must contain exactly width * height pixels")
        return self


class PixelsAction(OperationModel):
    type: Literal["pixels"]
    layer_id: LayerIdentifier
    changes: list[PixelChange] = Field(min_length=1, max_length=65_536)


class LayerAddAction(OperationModel):
    type: Literal["layer-add"]
    layer: ImageLayer
    after_id: LayerIdentifier | None


class LayerRemoveAction(OperationModel):
    type: Literal["layer-remove"]
    layer_id: LayerIdentifier
    expected_layer: ImageLayer | None = None


class LayerFields(OperationModel):
    name: str | None = Field(default=None, min_length=1, pattern=r"\S")
    visible: StrictBool | None = None
    locked: StrictBool | None = None
    opacity: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_fields(self) -> "LayerFields":
        if not self.model_fields_set:
            raise ValueError("At least one layer field is required")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Layer fields cannot be null")
        return self


class LayerUpdateAction(OperationModel):
    type: Literal["layer-update"]
    layer_id: LayerIdentifier
    fields: LayerFields
    expected: LayerFields | None = None


class LayerOrderAction(OperationModel):
    type: Literal["layer-order"]
    layer_ids: list[LayerIdentifier] = Field(min_length=1, max_length=MAX_LAYERS)
    expected_layer_ids: list[LayerIdentifier] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_LAYERS,
    )

    @model_validator(mode="after")
    def validate_order(self) -> "LayerOrderAction":
        if len(set(self.layer_ids)) != len(self.layer_ids):
            raise ValueError("Layer order cannot contain duplicate identifiers")
        if self.expected_layer_ids is not None and len(set(self.expected_layer_ids)) != len(
            self.expected_layer_ids
        ):
            raise ValueError("Expected layer order cannot contain duplicate identifiers")
        return self


class ReplaceAction(OperationModel):
    type: Literal["replace"]
    document: ImageDocument


class ImportAction(ReplaceAction):
    type: Literal["import"]


ResizeAnchor = Literal[
    "top-left",
    "top",
    "top-right",
    "left",
    "center",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
]


class ResizeAction(OperationModel):
    type: Literal["resize"]
    width: Dimension
    height: Dimension
    anchor: ResizeAnchor


class UndoAction(OperationModel):
    type: Literal["undo"]


class RedoAction(OperationModel):
    type: Literal["redo"]


ImageAction = Annotated[
    PixelsAction
    | LayerAddAction
    | LayerRemoveAction
    | LayerUpdateAction
    | LayerOrderAction
    | ReplaceAction
    | ImportAction
    | ResizeAction
    | UndoAction
    | RedoAction,
    Field(discriminator="type"),
]


class ImageOperationRequest(OperationModel):
    operation_id: Identifier
    base_revision: Annotated[StrictInt, Field(ge=0)]
    width: Dimension
    height: Dimension
    history_group_id: Identifier | None = None
    coordinate_after_operation_id: Identifier | None = None
    actions: list[ImageAction] = Field(min_length=1, max_length=MAX_ACTIONS)

    @model_validator(mode="after")
    def validate_packet(self) -> "ImageOperationRequest":
        if (
            any(
                isinstance(action, (ReplaceAction, ResizeAction, UndoAction, RedoAction))
                for action in self.actions
            )
            and len(self.actions) != 1
        ):
            raise ValueError(
                "Replace, resize, undo and redo must be the only action in their packet"
            )
        pixel_changes = 0
        for action in self.actions:
            if isinstance(action, PixelsAction):
                pixel_changes += len(action.changes)
                if any(change[0] >= self.width * self.height for change in action.changes):
                    raise ValueError("Pixel index is outside the packet dimensions")
            elif isinstance(action, LayerAddAction):
                if len(action.layer.pixels) != self.width * self.height:
                    raise ValueError("Added layer must contain exactly width * height pixels")
            elif isinstance(action, LayerRemoveAction) and action.expected_layer is not None:
                if len(action.expected_layer.pixels) != self.width * self.height:
                    raise ValueError("Expected layer must contain exactly width * height pixels")
        if pixel_changes > MAX_PIXEL_CHANGES:
            raise ValueError("Too many pixel changes in one packet")
        if len(self.model_dump_json().encode("utf-8")) > MAX_OPERATION_BYTES:
            raise ValueError("Image operation must be at most 3 MiB")
        return self


class SharedImageHistory(OperationModel):
    can_undo: bool
    can_redo: bool


class CanvasTransform(OperationModel):
    revision: int
    from_width: int
    from_height: int
    to_width: int
    to_height: int
    offset_x: int
    offset_y: int
    operation_id: str
    user_id: UUID | None


class ImageOperationState(OperationModel):
    resource: ProjectResourceDetail
    history: SharedImageHistory
    transforms: list[CanvasTransform]


class ImageOperationResponse(ImageOperationState):
    operation_id: str
    applied_revision: int


def conflict(code: str, resource: ProjectResource, message: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={"code": code, "current_revision": resource.revision, "message": message},
    )


def normalize_color(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(
        r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", value.strip()
    ):
        return value.strip().upper()
    return None


def legacy_dimension(value: Any) -> int:
    try:
        numeric = float(value)
        return min(256, max(1, math.floor(numeric + 0.5))) if math.isfinite(numeric) else 32
    except (TypeError, ValueError):
        return 32


def canonical_document(resource: ProjectResource) -> dict[str, Any]:
    data = resource.data
    payload = data.get("pixel_art", data)
    if not isinstance(payload, dict):
        raise conflict("image_document_invalid", resource, "Stored image data is not an object")
    version = payload.get("version")
    if version == 2:
        try:
            return ImageDocument.model_validate(payload).model_dump(mode="json")
        except ValueError as error:
            raise conflict(
                "image_document_invalid", resource, "Stored image cannot be edited safely"
            ) from error
    if version not in (None, 1):
        raise conflict("image_document_invalid", resource, "Stored image version is unsupported")
    width = legacy_dimension(
        payload.get("width") if payload.get("width") is not None else payload.get("size", 32)
    )
    height = legacy_dimension(
        payload.get("height") if payload.get("height") is not None else payload.get("size", 32)
    )
    pixels = payload.get("pixels", [])
    pixels = pixels if isinstance(pixels, list) else []
    return {
        "version": 2,
        "width": width,
        "height": height,
        "palette": [],
        "layers": [
            {
                "id": LEGACY_LAYER_ID,
                "name": "Layer 1",
                "visible": True,
                "locked": False,
                "opacity": 1,
                "pixels": [
                    normalize_color(pixels[index]) if index < len(pixels) else None
                    for index in range(width * height)
                ],
            }
        ],
    }


def resize_transform(document: dict[str, Any], action: ResizeAction) -> dict[str, int]:
    horizontal = {"left": 0, "right": 1}
    vertical = {"top": 0, "bottom": 1}
    words = action.anchor.split("-")
    column = next((horizontal[word] for word in words if word in horizontal), 0.5)
    row = next((vertical[word] for word in words if word in vertical), 0.5)
    return {
        "from_width": document["width"],
        "from_height": document["height"],
        "to_width": action.width,
        "to_height": action.height,
        "offset_x": math.floor((action.width - document["width"]) * column + 0.5),
        "offset_y": math.floor((action.height - document["height"]) * row + 0.5),
    }


def inverse_transform(transform: dict[str, int]) -> dict[str, int]:
    return {
        "from_width": transform["to_width"],
        "from_height": transform["to_height"],
        "to_width": transform["from_width"],
        "to_height": transform["from_height"],
        "offset_x": -transform["offset_x"],
        "offset_y": -transform["offset_y"],
    }


def map_pixel_index(index: int, transforms: list[dict[str, int]]) -> int | None:
    for transform in transforms:
        x = index % transform["from_width"] + transform["offset_x"]
        y = index // transform["from_width"] + transform["offset_y"]
        # Do not compose transforms before checking bounds: a pixel cropped
        # during an intermediate resize must not resurrect on a later expand.
        if not (0 <= x < transform["to_width"] and 0 <= y < transform["to_height"]):
            return None
        index = y * transform["to_width"] + x
    return index


def map_pixel_array(pixels: list[Any], transforms: list[dict[str, int]]) -> list[Any]:
    if not transforms:
        return list(pixels)
    final = transforms[-1]
    mapped: list[Any] = [None] * (final["to_width"] * final["to_height"])
    for index, color in enumerate(pixels):
        target = map_pixel_index(index, transforms)
        if target is not None:
            mapped[target] = color
    return mapped


def transform_packet(
    packet: ImageOperationRequest,
    document: dict[str, Any],
    transforms: list[dict[str, int]],
    resource: ProjectResource,
) -> ImageOperationRequest:
    width, height = packet.width, packet.height
    for transform in transforms:
        if (width, height) != (transform["from_width"], transform["from_height"]):
            raise conflict(
                "image_coordinate_frame_unknown",
                resource,
                "Pending edits have an unknown coordinate frame; keep a local copy",
            )
        width, height = transform["to_width"], transform["to_height"]
    if (width, height) != (document["width"], document["height"]):
        raise conflict(
            "image_coordinate_frame_unknown",
            resource,
            "Pending edits predate retained canvas lineage; keep a local copy",
        )
    actions: list[ImageAction] = []
    for action in packet.actions:
        if isinstance(action, PixelsAction):
            changes = []
            for change in action.changes:
                target = map_pixel_index(change[0], transforms)
                if target is not None:
                    changes.append((target, *change[1:]))
            action = action.model_copy(update={"changes": changes})
        elif isinstance(action, LayerAddAction):
            layer = action.layer.model_copy(
                update={"pixels": map_pixel_array(action.layer.pixels, transforms)}
            )
            action = action.model_copy(update={"layer": layer})
        elif isinstance(action, LayerRemoveAction) and action.expected_layer is not None:
            expected = action.expected_layer.model_copy(
                update={"pixels": map_pixel_array(action.expected_layer.pixels, transforms)}
            )
            action = action.model_copy(update={"expected_layer": expected})
        actions.append(action)
    return packet.model_copy(update={"width": width, "height": height, "actions": actions})


def resize_document(document: dict[str, Any], transform: dict[str, int]) -> dict[str, Any]:
    resized = deepcopy(document)
    resized["width"], resized["height"] = transform["to_width"], transform["to_height"]
    for layer in resized["layers"]:
        layer["pixels"] = map_pixel_array(layer["pixels"], [transform])
    return normalize_document_palette(resized)


def normalize_document_palette(document: dict[str, Any]) -> dict[str, Any]:
    colors: dict[str, None] = {}
    for layer in document["layers"]:
        layer["pixels"] = [normalize_color(pixel) for pixel in layer["pixels"]]
        for color in layer["pixels"]:
            if color is not None:
                colors[color] = None
    document["palette"] = list(colors)
    return document


def compress_document(document: dict[str, Any]) -> bytes:
    return zlib.compress(
        json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def decompress_document(document: bytes) -> dict[str, Any]:
    return json.loads(zlib.decompress(document))


def history_entries(session: Session, resource_id: UUID) -> list[ImageHistoryEntry]:
    return list(
        session.exec(
            select(ImageHistoryEntry)
            .options(
                load_only(
                    ImageHistoryEntry.id,
                    ImageHistoryEntry.resource_id,
                    ImageHistoryEntry.user_id,
                    ImageHistoryEntry.applied_revision,
                    ImageHistoryEntry.latest_edit_revision,
                    ImageHistoryEntry.history_group_id,
                    ImageHistoryEntry.action_kind,
                    ImageHistoryEntry.transforms,
                    ImageHistoryEntry.active,
                    ImageHistoryEntry.undone_at_revision,
                )
            )
            .where(
                ImageHistoryEntry.resource_id == resource_id,
            )
            .order_by(ImageHistoryEntry.applied_revision)
        ).all()
    )


def history_flags(entries: list[ImageHistoryEntry]) -> SharedImageHistory:
    return SharedImageHistory(
        can_undo=any(entry.active for entry in entries),
        can_redo=any(not entry.active for entry in entries),
    )


def read_history_flags(session: Session, resource_id: UUID) -> SharedImageHistory:
    # Polling tabs need two indexed existence checks, never compressed blobs.
    return SharedImageHistory(
        can_undo=bool(
            session.exec(
                select(
                    exists().where(
                        ImageHistoryEntry.resource_id == resource_id,
                        ImageHistoryEntry.active.is_(True),
                    )
                )
            ).one()
        ),
        can_redo=bool(
            session.exec(
                select(
                    exists().where(
                        ImageHistoryEntry.resource_id == resource_id,
                        ImageHistoryEntry.active.is_(False),
                    )
                )
            ).one()
        ),
    )


def canvas_transforms(
    session: Session, resource_id: UUID, since_revision: int, until_revision: int
) -> list[CanvasTransform]:
    return [
        CanvasTransform.model_validate(row, from_attributes=True)
        for row in session.exec(
            select(ImageCanvasTransform)
            .where(
                ImageCanvasTransform.resource_id == resource_id,
                ImageCanvasTransform.revision > since_revision,
                ImageCanvasTransform.revision <= until_revision,
            )
            .order_by(ImageCanvasTransform.revision, ImageCanvasTransform.position),
        ).all()
    ]


def require_response_fits(response: ImageOperationState, resource: ProjectResource) -> None:
    if len(response.model_dump_json().encode("utf-8")) > MAX_CANONICAL_RESPONSE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "code": "image_document_too_large",
                "current_revision": resource.revision,
                "message": (
                    "This document exceeds the hosting size limit. Pending edits have not "
                    "been accepted; keep them locally and export a local JSON copy."
                ),
            },
        )


def get_image_operation_state(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    resource_id: str,
    since_revision: int = 0,
) -> ImageOperationState:
    project = get_project_or_404(session=session, user_id=user_id, project_id=project_id)
    try:
        parsed_resource_id = UUID(resource_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Resource not found") from None
    try:
        # SHARE gives a coherent resource/history/lineage snapshot while
        # allowing concurrent readers. Every writer takes the same row lock.
        resource = session.exec(
            select(ProjectResource)
            .where(
                ProjectResource.id == parsed_resource_id,
                ProjectResource.project_id == project.id,
            )
            .with_for_update(read=True)
            .execution_options(populate_existing=True)
        ).first()
        if resource is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        if resource.type not in (ResourceType.pixel_art, ResourceType.tileset):
            raise HTTPException(status_code=422, detail="Image state requires an image or tileset")
        if since_revision > resource.revision:
            raise conflict(
                "resource_revision_conflict",
                resource,
                "Requested history revision is ahead of the server",
            )
        response = ImageOperationState(
            resource=ProjectResourceDetail.model_validate(resource),
            history=read_history_flags(session, resource.id),
            transforms=canvas_transforms(session, resource.id, since_revision, resource.revision),
        )
        require_response_fits(response, resource)
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise


def apply_actions(
    document: dict[str, Any],
    packet: ImageOperationRequest,
    resource: ProjectResource,
) -> dict[str, Any]:
    document = deepcopy(document)

    def find_layer(layer_id: str) -> dict[str, Any] | None:
        for layer in document["layers"]:
            if layer["id"] == layer_id:
                return layer
        return None

    for action in packet.actions:
        if isinstance(action, ReplaceAction):
            if not isinstance(action, ImportAction) and packet.base_revision != resource.revision:
                raise conflict(
                    "resource_revision_conflict",
                    resource,
                    "The image changed remotely; replacement requires an up-to-date revision",
                )
            document = action.document.model_dump(mode="json")
        elif isinstance(action, PixelsAction):
            layer = find_layer(action.layer_id)
            if layer is None:
                continue
            pixels = layer["pixels"]
            for change in action.changes:
                if len(change) == 3 and normalize_color(pixels[change[0]]) != normalize_color(
                    change[2]
                ):
                    continue
                pixels[change[0]] = normalize_color(change[1])
        elif isinstance(action, LayerAddAction):
            existing = find_layer(action.layer.id)
            if existing is not None:
                if existing == action.layer.model_dump(mode="json"):
                    continue
                raise conflict(
                    "image_layer_exists", resource, "Added layer identifier already exists"
                )
            if len(document["layers"]) >= MAX_LAYERS:
                raise conflict("image_layer_limit", resource, "The image already has 128 layers")
            index = 0
            if action.after_id is not None:
                after = find_layer(action.after_id)
                index = (
                    document["layers"].index(after) + 1
                    if after is not None
                    else len(document["layers"])
                )
            document["layers"].insert(index, action.layer.model_dump(mode="json"))
        elif isinstance(action, LayerRemoveAction):
            if action.expected_layer is not None:
                current = next(
                    (layer for layer in document["layers"] if layer["id"] == action.layer_id), None
                )
                expected = action.expected_layer.model_dump(mode="json")
                expected["pixels"] = [normalize_color(pixel) for pixel in expected["pixels"]]
                if current is None:
                    continue
                normalized_current = {
                    **current,
                    "pixels": [normalize_color(pixel) for pixel in current["pixels"]],
                }
                if normalized_current != expected:
                    continue
            layer = find_layer(action.layer_id)
            if layer is None or len(document["layers"]) == 1:
                continue
            document["layers"].remove(layer)
        elif isinstance(action, LayerUpdateAction):
            layer = find_layer(action.layer_id)
            if layer is None:
                continue
            expected = action.expected.model_dump(exclude_unset=True) if action.expected else {}
            for field, value in action.fields.model_dump(exclude_unset=True).items():
                if field not in expected or layer[field] == expected[field]:
                    layer[field] = value
        elif isinstance(action, LayerOrderAction):
            if (
                action.expected_layer_ids is not None
                and [layer["id"] for layer in document["layers"]] != action.expected_layer_ids
            ):
                continue
            known = [
                layer
                for layer_id in action.layer_ids
                if (layer := find_layer(layer_id)) is not None
            ]
            ordered = iter(known)
            requested_ids = {layer["id"] for layer in known}
            # Keep concurrently added, unknown layers in their existing slots.
            document["layers"] = [
                next(ordered) if layer["id"] in requested_ids else layer
                for layer in document["layers"]
            ]
    return normalize_document_palette(document)


def submit_image_operation(
    *,
    session: Session,
    user_id: UUID,
    project_id: str,
    resource_id: str,
    packet: ImageOperationRequest,
) -> ImageOperationResponse:
    # Authorization is rechecked even for acknowledged packets: revocation must
    # not leak current document data through the deduplication endpoint.
    project = get_project_or_404(
        session=session,
        user_id=user_id,
        project_id=project_id,
        required_roles=PROJECT_EDIT_ROLES,
    )
    try:
        parsed_resource_id = UUID(resource_id)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found"
        ) from None

    try:
        resource = session.exec(
            select(ProjectResource)
            .where(
                ProjectResource.id == parsed_resource_id,
                ProjectResource.project_id == project.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True),
        ).first()
        if resource is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
        if resource.type not in (ResourceType.pixel_art, ResourceType.tileset):
            raise HTTPException(
                status_code=422, detail="Image operations require an image or tileset"
            )
        hash_payload = packet.model_dump(mode="json")
        # Preserve the exact preimage used by previously deployed clients.
        for optional in ("history_group_id", "coordinate_after_operation_id"):
            if hash_payload[optional] is None:
                hash_payload.pop(optional)
        request_hash = hashlib.sha256(
            json.dumps(
                hash_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        receipt = session.exec(
            select(ImageOperationReceipt).where(
                ImageOperationReceipt.resource_id == resource.id,
                ImageOperationReceipt.user_id == user_id,
                ImageOperationReceipt.operation_id == packet.operation_id,
            )
        ).first()
        if receipt is not None:
            if receipt.request_hash != request_hash:
                raise conflict(
                    "image_operation_id_reused",
                    resource,
                    "Operation identifier was reused with different changes",
                )
            # A verified retry may span the 0019 -> shared-history upgrade.
            # Recover only the accepted packet's END frame from its exact
            # hash-matched payload; never invent its unknowable earlier origin
            # transform, reapply the operation, or create retroactive history.
            if receipt.action_kind is None:
                accepted = packet.actions[0]
                if isinstance(accepted, ReplaceAction):
                    receipt.action_kind = accepted.type
                    receipt.coordinate_width = accepted.document.width
                    receipt.coordinate_height = accepted.document.height
                elif isinstance(accepted, ResizeAction):
                    receipt.action_kind = "resize"
                    receipt.coordinate_width = accepted.width
                    receipt.coordinate_height = accepted.height
                else:
                    receipt.action_kind = "edit"
                    receipt.coordinate_width = packet.width
                    receipt.coordinate_height = packet.height
                session.add(receipt)
            response = ImageOperationResponse(
                operation_id=packet.operation_id,
                applied_revision=receipt.applied_revision,
                resource=ProjectResourceDetail.model_validate(resource),
                history=read_history_flags(session, resource.id),
                transforms=canvas_transforms(
                    session, resource.id, packet.base_revision, resource.revision
                ),
            )
            require_response_fits(response, resource)
            session.commit()  # Release the row lock even for a duplicate acknowledgement.
            return response
        if packet.base_revision > resource.revision:
            raise conflict(
                "resource_revision_conflict", resource, "Packet revision is ahead of the server"
            )
        before = normalize_document_palette(canonical_document(resource))
        entries = history_entries(session, resource.id)
        state = session.get(ImageHistoryState, resource.id)
        action = packet.actions[0]
        target: ImageHistoryEntry | None = None
        geometry: list[dict[str, int]] = []
        kind = "edit"
        flags = history_flags(entries)
        if isinstance(action, (UndoAction, RedoAction)):
            kind = action.type
            eligible = [
                entry for entry in entries if entry.active == isinstance(action, UndoAction)
            ]
            if eligible:
                target = max(
                    eligible,
                    key=lambda entry: (
                        entry.applied_revision
                        if isinstance(action, UndoAction)
                        else entry.undone_at_revision or 0
                    ),
                )
                document = decompress_document(
                    target.before_document
                    if isinstance(action, UndoAction)
                    else target.after_document
                )
                geometry = (
                    [inverse_transform(transform) for transform in reversed(target.transforms)]
                    if isinstance(action, UndoAction)
                    else deepcopy(target.transforms)
                )
                flags = SharedImageHistory(
                    can_undo=any(entry.active and entry.id != target.id for entry in entries)
                    if isinstance(action, UndoAction)
                    else True,
                    can_redo=True
                    if isinstance(action, UndoAction)
                    else any(not entry.active and entry.id != target.id for entry in entries),
                )
            else:
                document = deepcopy(before)
        elif isinstance(action, ResizeAction):
            kind = "resize"
            transform = resize_transform(before, action)
            document = resize_document(before, transform)
            # Identity events prove an accepted frame after a lost ACK, but
            # do not turn no-op resizes into real shared history entries.
            geometry = [transform]
        elif isinstance(action, ReplaceAction):
            kind = "import" if isinstance(action, ImportAction) else "replace"
            document = apply_actions(before, packet, resource)
            geometry = [
                {
                    "from_width": before["width"],
                    "from_height": before["height"],
                    "to_width": document["width"],
                    "to_height": document["height"],
                    "offset_x": 0,
                    "offset_y": 0,
                }
            ]
        else:
            source_revision = packet.base_revision
            if packet.coordinate_after_operation_id is not None:
                dependency = session.exec(
                    select(ImageOperationReceipt).where(
                        ImageOperationReceipt.resource_id == resource.id,
                        ImageOperationReceipt.user_id == user_id,
                        ImageOperationReceipt.operation_id == packet.coordinate_after_operation_id,
                    )
                ).first()
                if (
                    dependency is None
                    or dependency.action_kind not in ("resize", "replace", "import")
                    or (dependency.coordinate_width, dependency.coordinate_height)
                    != (packet.width, packet.height)
                ):
                    raise conflict(
                        "image_coordinate_dependency_invalid",
                        resource,
                        "The preceding canvas operation has not been acknowledged "
                        "in this coordinate frame",
                    )
                source_revision = dependency.applied_revision
            lineage = [
                transform.model_dump(
                    include={
                        "from_width",
                        "from_height",
                        "to_width",
                        "to_height",
                        "offset_x",
                        "offset_y",
                    }
                )
                for transform in canvas_transforms(
                    session, resource.id, source_revision, resource.revision
                )
            ]
            mapped = transform_packet(packet, before, lineage, resource)
            document = apply_actions(before, mapped, resource)
        changed = document != before
        if changed and kind not in ("undo", "redo"):
            flags = SharedImageHistory(can_undo=True, can_redo=False)
        next_data = {**resource.data, "pixel_art": document}
        applied_revision = resource.revision + 1
        next_updated_at = utc_now()
        # Verify the actual complete response BEFORE accepting or journalling
        # the operation. Committing a document that the platform cannot return
        # would strand retries behind a receipt with an undeliverable response.
        response = ImageOperationResponse(
            operation_id=packet.operation_id,
            applied_revision=applied_revision,
            resource=ProjectResourceDetail.model_validate(
                resource,
                update={
                    "data": next_data,
                    "revision": applied_revision,
                    "updated_at": next_updated_at,
                },
            ),
            history=flags,
            transforms=canvas_transforms(
                session, resource.id, packet.base_revision, resource.revision
            )
            + [
                CanvasTransform(
                    revision=applied_revision,
                    operation_id=packet.operation_id,
                    user_id=user_id,
                    **transform,
                )
                for transform in geometry
            ],
        )
        require_response_fits(response, resource)
        if state is None:
            state = ImageHistoryState(resource_id=resource.id)
            session.add(state)
        if kind in ("undo", "redo"):
            if target is not None:
                target.active = kind == "redo"
                target.undone_at_revision = applied_revision if kind == "undo" else None
                session.add(target)
            state.last_action_revision = applied_revision
            state.last_entry_id = None
        elif changed:
            # A real edit starts a new branch; no-op packets never clear redo.
            for entry in entries:
                if not entry.active:
                    session.delete(entry)
            latest = next((entry for entry in reversed(entries) if entry.active), None)
            can_group = (
                packet.history_group_id is not None
                and kind == "edit"
                and latest is not None
                and latest.action_kind == "edit"
                and latest.user_id == user_id
                and latest.history_group_id == packet.history_group_id
                and state.last_entry_id == latest.id
                and state.last_action_revision == latest.latest_edit_revision
            )
            if can_group:
                latest.after_document = compress_document(document)
                latest.latest_edit_revision = applied_revision
                entry = latest
            else:
                entry = ImageHistoryEntry(
                    resource_id=resource.id,
                    user_id=user_id,
                    applied_revision=applied_revision,
                    latest_edit_revision=applied_revision,
                    history_group_id=packet.history_group_id,
                    action_kind=kind,
                    before_document=compress_document(before),
                    after_document=compress_document(document),
                    transforms=geometry,
                )
                session.add(entry)
                # The singleton points at this entry via a real FK. Insert the
                # entry first even when both objects are new in this session.
                session.flush()
            state.last_action_revision = applied_revision
            state.last_entry_id = entry.id
            session.flush()
            retained = list(
                session.exec(
                    select(
                        ImageHistoryEntry.id,
                        func.length(ImageHistoryEntry.before_document),
                        func.length(ImageHistoryEntry.after_document),
                    )
                    .where(ImageHistoryEntry.resource_id == resource.id)
                    .order_by(
                        ImageHistoryEntry.applied_revision,
                    )
                ).all()
            )
            total_bytes = sum(
                before_bytes + after_bytes for _, before_bytes, after_bytes in retained
            )
            while len(retained) > 1 and (
                len(retained) > MAX_HISTORY_ENTRIES or total_bytes > MAX_HISTORY_BYTES
            ):
                evicted_id, before_bytes, after_bytes = retained.pop(0)
                total_bytes -= before_bytes + after_bytes
                session.execute(delete(ImageHistoryEntry).where(ImageHistoryEntry.id == evicted_id))
        session.add(state)
        for position, transform in enumerate(geometry):
            session.add(
                ImageCanvasTransform(
                    resource_id=resource.id,
                    user_id=user_id,
                    operation_id=packet.operation_id,
                    revision=applied_revision,
                    position=position,
                    **transform,
                )
            )
        resource.data = next_data
        resource.revision = applied_revision
        resource.updated_at = next_updated_at
        session.add(resource)
        session.add(
            ImageOperationReceipt(
                resource_id=resource.id,
                user_id=user_id,
                operation_id=packet.operation_id,
                request_hash=request_hash,
                applied_revision=applied_revision,
                coordinate_width=document["width"],
                coordinate_height=document["height"],
                action_kind=kind,
            )
        )
        session.commit()
        # Return the acceptance-time canonical snapshot checked above. A later
        # concurrent update must not change this packet's acknowledgement or
        # bypass its serialized response-size check after the lock is released.
    except Exception:
        session.rollback()
        raise
    publish_project_event(
        session=session,
        project_id=project.id,
        event="project.updated",
        actor_id=user_id,
        extra={
            "resource_id": str(resource.id),
            "revision": response.applied_revision,
            "operation_id": packet.operation_id,
        },
    )
    return response

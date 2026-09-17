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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from sqlalchemy import delete, exists, func
from sqlalchemy.orm import aliased, load_only
from sqlmodel import Session, select

from app.crud import (
    PROJECT_EDIT_ROLES,
    get_project_or_404,
    list_project_user_ids,
    publish_project_event,
)
from app.image_pixel_codec import (
    MAX_CANVAS_PIXELS,
    MAX_DOCUMENT_PIXELS,
    MAX_IMAGE_DIMENSION,
    canvas_pixel_count,
    compact_document,
    decode_document,
    decode_pixels,
)
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
Dimension = Annotated[StrictInt, Field(ge=1, le=MAX_IMAGE_DIMENSION)]
PixelIndex = Annotated[StrictInt, Field(ge=0, lt=MAX_CANVAS_PIXELS)]
PixelChange = tuple[PixelIndex, Color] | tuple[PixelIndex, Color, Color]


class OperationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageLayer(OperationModel):
    id: LayerIdentifier
    name: str = Field(min_length=1, pattern=r"\S")
    visible: StrictBool
    locked: StrictBool
    opacity: float = Field(ge=0, le=1, allow_inf_nan=False)
    pixels: list[Color] = Field(min_length=1, max_length=MAX_CANVAS_PIXELS)

    @field_validator("pixels", mode="before")
    @classmethod
    def decode_compact_pixels(cls, value: Any) -> Any:
        return decode_pixels(value) if isinstance(value, dict) else value


class ImageDocument(OperationModel):
    version: Literal[2]
    width: Dimension
    height: Dimension
    # A multilayer image may contain more unique colors than one layer's pixel
    # count. Packet byte limits bound replacements without making a correctly
    # accepted add-layer operation unreadable on the next request.
    palette: list[Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")]]
    layers: list[ImageLayer] = Field(min_length=1, max_length=MAX_LAYERS)

    @model_validator(mode="before")
    @classmethod
    def decode_compact_document(cls, value: Any) -> Any:
        return decode_document(value)

    @model_validator(mode="after")
    def validate_layers(self) -> "ImageDocument":
        canvas_pixel_count(self.width, self.height)
        if len(self.layers) * self.width * self.height > MAX_DOCUMENT_PIXELS:
            raise ValueError("Image layers exceed the supported document pixel budget")
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

    @model_validator(mode="after")
    def validate_canvas_budget(self) -> "ResizeAction":
        canvas_pixel_count(self.width, self.height)
        return self


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

    @model_validator(mode="before")
    @classmethod
    def decode_layer_snapshots(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        count = canvas_pixel_count(value.get("width"), value.get("height"))
        actions = value.get("actions")
        if not isinstance(actions, list):
            return value
        if len(actions) > MAX_ACTIONS:
            raise ValueError("Too many actions in one image operation")
        # Budget the entire packet before inflating any snapshot. Per-layer
        # validation alone permits hundreds of tiny compressed blank buffers
        # to expand into gigabytes before the document's layer limit runs.
        snapshot_pixels = 0
        for action in actions:
            if not isinstance(action, dict):
                continue
            for field in ("layer", "expected_layer"):
                if isinstance(action.get(field), dict):
                    snapshot_pixels += count
            document = action.get("document")
            if isinstance(document, dict):
                document_count = canvas_pixel_count(document.get("width"), document.get("height"))
                layers = document.get("layers")
                if isinstance(layers, list):
                    snapshot_pixels += document_count * len(layers)
            if snapshot_pixels > MAX_DOCUMENT_PIXELS:
                raise pixel_budget_error()
        copied = []
        for action in actions:
            if not isinstance(action, dict):
                copied.append(action)
                continue
            action = dict(action)
            for field in ("layer", "expected_layer"):
                layer = action.get(field)
                if isinstance(layer, dict) and isinstance(layer.get("pixels"), dict):
                    action[field] = {**layer, "pixels": decode_pixels(layer["pixels"], count)}
            copied.append(action)
        return {**value, "actions": copied}

    @model_validator(mode="after")
    def validate_packet(self) -> "ImageOperationRequest":
        canvas_pixel_count(self.width, self.height)
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
        packed = self.model_dump(mode="json")
        for action in packed["actions"]:
            if "document" in action:
                action["document"] = compact_document(action["document"])
            for field in ("layer", "expected_layer"):
                if field in action and action[field] is not None:
                    document = {"version": 2, "layers": [action[field]]}
                    action[field] = compact_document(document)["layers"][0]
        if (
            len(json.dumps(packed, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            > MAX_OPERATION_BYTES
        ):
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


def pixel_budget_error(resource: ProjectResource | None = None) -> HTTPException:
    detail: dict[str, Any] = {
        "code": "image_document_pixel_limit",
        "message": "Image layers exceed the supported document pixel budget",
    }
    if resource is not None:
        detail["current_revision"] = resource.revision
    return HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail=detail)


def normalize_color(value: Any) -> str | None:
    if isinstance(value, str) and re.fullmatch(
        r"#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?", value.strip()
    ):
        return value.strip().upper()
    return None


def legacy_dimension(value: Any) -> int:
    try:
        numeric = float(value)
        return (
            min(MAX_IMAGE_DIMENSION, max(1, math.floor(numeric + 0.5)))
            if math.isfinite(numeric)
            else 32
        )
    except (TypeError, ValueError):
        return 32


def canonical_document(resource: ProjectResource) -> dict[str, Any]:
    data = resource.data
    payload = data.get("pixel_art", data)
    if not isinstance(payload, dict):
        raise conflict("image_document_invalid", resource, "Stored image data is not an object")
    version = payload.get("version")
    if version == 2:
        layers = payload.get("layers")
        try:
            count = canvas_pixel_count(payload.get("width"), payload.get("height"))
        except ValueError as error:
            raise pixel_budget_error(resource) from error
        if isinstance(layers, list) and count * len(layers) > MAX_DOCUMENT_PIXELS:
            raise pixel_budget_error(resource)
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
    try:
        canvas_pixel_count(width, height)
    except ValueError as error:
        raise pixel_budget_error(resource) from error
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
    count = canvas_pixel_count(transform["to_width"], transform["to_height"])
    if count * len(document["layers"]) > MAX_DOCUMENT_PIXELS:
        raise ValueError("Image layers exceed the supported document pixel budget")
    resized = {
        **document,
        "width": transform["to_width"],
        "height": transform["to_height"],
        "layers": [
            {**layer, "pixels": map_pixel_array(layer["pixels"], [transform])}
            for layer in document["layers"]
        ],
    }
    return normalize_document_palette(resized)


def normalize_document_palette(document: dict[str, Any]) -> dict[str, Any]:
    colors: dict[str, None] = {}
    normalized: dict[Any, str | None] = {None: None}
    for layer in document["layers"]:
        original_pixels = layer["pixels"]
        pixels = original_pixels
        for index, pixel in enumerate(original_pixels):
            if isinstance(pixel, (str, type(None))):
                if pixel not in normalized:
                    normalized[pixel] = normalize_color(pixel)
                color = normalized[pixel]
            else:
                color = normalize_color(pixel)
            if color != pixel:
                if pixels is original_pixels:
                    pixels = list(original_pixels)
                pixels[index] = color
            if color is not None:
                colors[color] = None
        layer["pixels"] = pixels
    document["palette"] = list(colors)
    return document


def canonical_history_source(
    stored: Any, canonical: dict[str, Any], original_pixels: list[list[Any]]
) -> dict[str, Any]:
    """Reuse a validated snapshot only if normalization changed no semantics.

    canonical_document has already validated and decoded every stored pixel.
    Normalization preserves each original list only when every color is already
    canonical. Header and layer-field comparisons additionally reject stale
    palettes and any model coercion that would alter the historical snapshot.
    """
    if not isinstance(stored, dict) or stored.get("version") != 2:
        return canonical
    if {key: value for key, value in stored.items() if key != "layers"} != {
        key: value for key, value in canonical.items() if key != "layers"
    }:
        return canonical
    layers = stored.get("layers")
    if (
        not isinstance(layers, list)
        or len(layers) != len(canonical["layers"])
        or len(layers) != len(original_pixels)
    ):
        return canonical
    for stored_layer, layer, pixels in zip(
        layers, canonical["layers"], original_pixels, strict=True
    ):
        if layer["pixels"] is not pixels:
            return canonical
        if {key: value for key, value in stored_layer.items() if key != "pixels"} != {
            key: value for key, value in layer.items() if key != "pixels"
        }:
            return canonical
    return stored


def compress_document(document: dict[str, Any]) -> bytes:
    return zlib.compress(
        json.dumps(compact_document(document), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def decompress_document(document: bytes) -> dict[str, Any]:
    return decode_document(json.loads(zlib.decompress(document)))


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
    # Both indexed existence checks share one round trip, never compressed blobs.
    can_undo, can_redo = session.exec(
        select(
            exists().where(
                ImageHistoryEntry.resource_id == resource_id,
                ImageHistoryEntry.active.is_(True),
            ),
            exists().where(
                ImageHistoryEntry.resource_id == resource_id,
                ImageHistoryEntry.active.is_(False),
            ),
        )
    ).one()
    return SharedImageHistory(
        can_undo=bool(can_undo),
        can_redo=bool(can_redo),
    )


def read_history_target(
    session: Session, resource_id: UUID, *, undo: bool
) -> tuple[ImageHistoryEntry | None, SharedImageHistory]:
    # The resource row is already locked. Select only the latest eligible entry
    # and the exact snapshot this command restores; never transfer every blob
    # or make a separate lazy-load round trip for the selected snapshot.
    candidate = aliased(ImageHistoryEntry)
    remaining = aliased(ImageHistoryEntry)
    ordering = (
        [candidate.applied_revision.desc()]
        if undo
        else [
            func.coalesce(candidate.undone_at_revision, 0).desc(),
            candidate.applied_revision.asc(),
        ]
    )
    selected = session.exec(
        select(
            candidate,
            exists().where(
                remaining.resource_id == resource_id,
                remaining.active.is_(undo),
                remaining.id != candidate.id,
            ),
        )
        .options(
            load_only(
                candidate.id,
                candidate.transforms,
                candidate.active,
                candidate.undone_at_revision,
                candidate.before_document if undo else candidate.after_document,
            )
        )
        .where(candidate.resource_id == resource_id, candidate.active.is_(undo))
        .order_by(*ordering)
        .limit(1)
    ).first()
    if selected is None:
        # No-op commands still report any entries on the opposite stack.
        return None, read_history_flags(session, resource_id)
    target, has_remaining = selected
    return target, SharedImageHistory(
        can_undo=bool(has_remaining) if undo else True,
        can_redo=True if undo else bool(has_remaining),
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
    document = {**document, "layers": [dict(layer) for layer in document["layers"]]}
    owned_pixels: set[int] = set()

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
            owned_pixels = {id(layer) for layer in document["layers"]}
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
                color = normalize_color(change[1])
                if pixels[change[0]] == color:
                    continue
                if id(layer) not in owned_pixels:
                    pixels = list(pixels)
                    layer["pixels"] = pixels
                    owned_pixels.add(id(layer))
                pixels[change[0]] = color
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
            if (len(document["layers"]) + 1) * document["width"] * document[
                "height"
            ] > MAX_DOCUMENT_PIXELS:
                raise pixel_budget_error(resource)
            index = 0
            if action.after_id is not None:
                after = find_layer(action.after_id)
                index = (
                    document["layers"].index(after) + 1
                    if after is not None
                    else len(document["layers"])
                )
            added = action.layer.model_dump(mode="json")
            document["layers"].insert(index, added)
            owned_pixels.add(id(added))
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
        before = canonical_document(resource)
        original_pixels = [layer["pixels"] for layer in before["layers"]]
        before = normalize_document_palette(before)
        before_history_document = canonical_history_source(
            resource.data.get("pixel_art", resource.data), before, original_pixels
        )
        state = session.get(ImageHistoryState, resource.id)
        action = packet.actions[0]
        entries: list[ImageHistoryEntry] = []
        target: ImageHistoryEntry | None = None
        geometry: list[dict[str, int]] = []
        kind = "edit"
        if isinstance(action, (UndoAction, RedoAction)):
            target, flags = read_history_target(
                session, resource.id, undo=isinstance(action, UndoAction)
            )
        else:
            entries = history_entries(session, resource.id)
            flags = history_flags(entries)
        if isinstance(action, (UndoAction, RedoAction)):
            kind = action.type
            if target is not None:
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
            else:
                document = deepcopy(before)
        elif isinstance(action, ResizeAction):
            kind = "resize"
            if action.width * action.height * len(before["layers"]) > MAX_DOCUMENT_PIXELS:
                raise pixel_budget_error(resource)
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
        packed_document = compact_document(document)
        next_data = {**resource.data, "pixel_art": packed_document}
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
                latest.after_document = compress_document(packed_document)
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
                    before_document=compress_document(before_history_document),
                    after_document=compress_document(packed_document),
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
        # Capture notification identifiers/recipients while the project fence
        # and loaded ORM rows are still valid. commit() expires rows; reading
        # resource.id afterwards would otherwise reload its entire image JSON.
        accepted_project_id = project.id
        accepted_resource_id = resource.id
        event_user_ids = list_project_user_ids(session=session, project_id=accepted_project_id)
        session.commit()
        # Return the acceptance-time canonical snapshot checked above. A later
        # concurrent update must not change this packet's acknowledgement or
        # bypass its serialized response-size check after the lock is released.
    except Exception:
        session.rollback()
        raise
    publish_project_event(
        session=session,
        project_id=accepted_project_id,
        event="project.updated",
        actor_id=user_id,
        user_ids=event_user_ids,
        extra={
            "resource_id": str(accepted_resource_id),
            "revision": response.applied_revision,
            "operation_id": packet.operation_id,
        },
    )
    return response

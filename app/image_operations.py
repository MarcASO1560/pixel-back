"""Authoritative image operations: merge against current state, never stale snapshots."""

import hashlib
import json
import math
import re
from copy import deepcopy
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator
from sqlmodel import Session, select

from app.crud import PROJECT_EDIT_ROLES, get_project_or_404, publish_project_event
from app.models import (
    ImageOperationReceipt,
    ProjectResource,
    ProjectResourceDetail,
    ResourceType,
)
from app.time import utc_now

MAX_LAYERS = 128
MAX_ACTIONS = 256
MAX_PIXEL_CHANGES = 262_144
MAX_OPERATION_BYTES = 8 * 1024 * 1024
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


ImageAction = Annotated[
    PixelsAction
    | LayerAddAction
    | LayerRemoveAction
    | LayerUpdateAction
    | LayerOrderAction
    | ReplaceAction,
    Field(discriminator="type"),
]


class ImageOperationRequest(OperationModel):
    operation_id: Identifier
    base_revision: Annotated[StrictInt, Field(ge=0)]
    width: Dimension
    height: Dimension
    actions: list[ImageAction] = Field(min_length=1, max_length=MAX_ACTIONS)

    @model_validator(mode="after")
    def validate_packet(self) -> "ImageOperationRequest":
        if (
            any(isinstance(action, ReplaceAction) for action in self.actions)
            and len(self.actions) != 1
        ):
            raise ValueError("Replace must be the only action in its packet")
        pixel_changes = 0
        for action in self.actions:
            if isinstance(action, PixelsAction):
                pixel_changes += len(action.changes)
                if any(change[0] >= self.width * self.height for change in action.changes):
                    raise ValueError("Pixel index is outside the packet dimensions")
            elif isinstance(action, LayerAddAction):
                if len(action.layer.pixels) != self.width * self.height:
                    raise ValueError("Added layer must contain exactly width * height pixels")
        if pixel_changes > MAX_PIXEL_CHANGES:
            raise ValueError("Too many pixel changes in one packet")
        if len(self.model_dump_json().encode("utf-8")) > MAX_OPERATION_BYTES:
            raise ValueError("Image operation must be at most 8 MiB")
        return self


class ImageOperationResponse(OperationModel):
    operation_id: str
    applied_revision: int
    resource: ProjectResourceDetail


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


def apply_actions(
    document: dict[str, Any],
    packet: ImageOperationRequest,
    resource: ProjectResource,
) -> dict[str, Any]:
    document = deepcopy(document)

    def find_layer(layer_id: str) -> dict[str, Any]:
        for layer in document["layers"]:
            if layer["id"] == layer_id:
                return layer
        raise conflict("image_layer_missing", resource, "An edited layer was removed remotely")

    for action in packet.actions:
        if isinstance(action, ReplaceAction):
            if packet.base_revision != resource.revision:
                raise conflict(
                    "resource_revision_conflict",
                    resource,
                    "The image changed remotely; replacement requires an up-to-date revision",
                )
            document = action.document.model_dump(mode="json")
        elif isinstance(action, PixelsAction):
            pixels = find_layer(action.layer_id)["pixels"]
            for change in action.changes:
                if len(change) == 3 and normalize_color(pixels[change[0]]) != normalize_color(
                    change[2]
                ):
                    continue
                pixels[change[0]] = normalize_color(change[1])
        elif isinstance(action, LayerAddAction):
            if any(layer["id"] == action.layer.id for layer in document["layers"]):
                raise conflict(
                    "image_layer_exists", resource, "Added layer identifier already exists"
                )
            if len(document["layers"]) >= MAX_LAYERS:
                raise conflict("image_layer_limit", resource, "The image already has 128 layers")
            index = 0
            if action.after_id is not None:
                after = find_layer(action.after_id)
                index = document["layers"].index(after) + 1
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
            if len(document["layers"]) == 1:
                raise conflict(
                    "image_last_layer", resource, "The last image layer cannot be removed"
                )
            document["layers"].remove(layer)
        elif isinstance(action, LayerUpdateAction):
            layer = find_layer(action.layer_id)
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
            ordered = iter([find_layer(layer_id) for layer_id in action.layer_ids])
            requested_ids = set(action.layer_ids)
            # Keep concurrently added, unknown layers in their existing slots.
            document["layers"] = [
                next(ordered) if layer["id"] in requested_ids else layer
                for layer in document["layers"]
            ]
    colors: dict[str, None] = {}
    for layer in document["layers"]:
        layer["pixels"] = [normalize_color(pixel) for pixel in layer["pixels"]]
        for color in layer["pixels"]:
            if color is not None:
                colors[color] = None
    document["palette"] = list(colors)
    return document


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
        request_hash = hashlib.sha256(
            json.dumps(
                packet.model_dump(mode="json"),
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
            response = ImageOperationResponse(
                operation_id=packet.operation_id,
                applied_revision=receipt.applied_revision,
                resource=ProjectResourceDetail.model_validate(resource),
            )
            session.commit()  # Release the row lock even for a duplicate acknowledgement.
            return response
        if packet.base_revision > resource.revision:
            raise conflict(
                "resource_revision_conflict", resource, "Packet revision is ahead of the server"
            )
        document = canonical_document(resource)
        if (document["width"], document["height"]) != (packet.width, packet.height):
            raise conflict("image_dimensions_conflict", resource, "The image was resized remotely")
        document = apply_actions(document, packet, resource)
        resource.data = {**resource.data, "pixel_art": document}
        resource.revision += 1
        applied_revision = resource.revision
        resource.updated_at = utc_now()
        session.add(resource)
        session.add(
            ImageOperationReceipt(
                resource_id=resource.id,
                user_id=user_id,
                operation_id=packet.operation_id,
                request_hash=request_hash,
                applied_revision=applied_revision,
            )
        )
        session.commit()
        session.refresh(resource)
        response = ImageOperationResponse(
            operation_id=packet.operation_id,
            applied_revision=applied_revision,
            resource=ProjectResourceDetail.model_validate(resource),
        )
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

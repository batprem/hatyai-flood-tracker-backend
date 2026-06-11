"""Load and cache the committed Hat Yai flood evacuation shelter dataset.

The dataset is the QA-validated GeoJSON file committed under ``backend/data``
(HFT-70): 8 shelters designated during the November 2025 (B.E. 2568) Hat Yai
flood, with OpenStreetMap coordinates and per-shelter designation sources. The
public API serves a normalized Pydantic shape, never the raw GeoJSON.
"""

import json
import re
from functools import lru_cache
from pathlib import Path

from app.schemas.common import Coordinates
from app.schemas.shelters import (
    Shelter,
    ShelterDatasetProvenance,
    SheltersResponse,
    ShelterType,
)

# Public reference returned in API responses so clients and auditors can tie a
# shelter list back to the exact committed dataset file that produced it.
SHELTER_DATASET_REF = "shelters_hatyai.geojson"

_SHELTER_FILE = Path(__file__).parent.parent.parent / "data" / SHELTER_DATASET_REF
_OSM_ELEMENT_PATTERN = re.compile(r"OpenStreetMap (node|way|relation)/(\d+)")


def _shelter_id(coordinate_source: str) -> str:
    """Derive a stable shelter identifier from its OpenStreetMap element.

    Args:
        coordinate_source: Dataset value such as ``OpenStreetMap way/858854620``.

    Returns:
        A slug such as ``osm-way-858854620``.

    Raises:
        ValueError: when the coordinate source does not reference an
            OpenStreetMap element, which indicates a malformed dataset.
    """
    match = _OSM_ELEMENT_PATTERN.fullmatch(coordinate_source)
    if match is None:
        raise ValueError(
            f"Unrecognized coordinate_source in {SHELTER_DATASET_REF}: {coordinate_source!r}"
        )
    return f"osm-{match.group(1)}-{match.group(2)}"


@lru_cache(maxsize=1)
def get_shelters_response() -> SheltersResponse:
    """Load and return the normalized shelter list from the committed GeoJSON.

    The result is cached for the process lifetime because the dataset is a
    static committed asset; no per-request file IO occurs after the first
    call.

    Returns:
        The normalized shelter list with dataset-level provenance metadata.
    """
    with _SHELTER_FILE.open(encoding="utf-8") as shelter_file:
        data = json.load(shelter_file)

    shelters: list[Shelter] = []
    for feature in data["features"]:
        properties = feature["properties"]
        longitude, latitude = feature["geometry"]["coordinates"]
        shelters.append(
            Shelter(
                id=_shelter_id(properties["coordinate_source"]),
                name_th=properties["name_th"],
                name_en=properties["name_en"],
                type=ShelterType(properties["type"]),
                location=Coordinates(latitude=latitude, longitude=longitude),
                municipality_th=properties["municipality_th"],
                capacity=properties["capacity"],
                source=properties["source"],
                source_url=properties["source_url"],
                coordinate_source=properties["coordinate_source"],
                coordinate_source_url=properties["coordinate_source_url"],
            )
        )

    provenance = ShelterDatasetProvenance(
        license=data["license"],
        retrieved_date=data["retrieved_date"],
        dataset_ref=SHELTER_DATASET_REF,
        accuracy_note=data["accuracy_note"],
    )
    return SheltersResponse(
        shelters=shelters,
        shelter_count=len(shelters),
        provenance=provenance,
    )

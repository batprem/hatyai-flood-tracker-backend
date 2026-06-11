"""Pydantic schemas for the public evacuation shelters API."""

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import Coordinates


class ShelterType(StrEnum):
    """Model the facility category of a flood evacuation shelter."""

    SCHOOL = "school"
    UNIVERSITY = "university"
    TEMPLE = "temple"
    COMMUNITY_CENTER = "community_center"
    OTHER = "other"


class Shelter(BaseModel):
    """Model a normalized flood evacuation shelter for public display."""

    id: str = Field(
        description=(
            "Stable shelter identifier derived from the OpenStreetMap element "
            "that provides the coordinates, e.g. 'osm-way-858854620'."
        )
    )
    name_th: str = Field(description="Thai display name of the shelter facility.")
    name_en: str = Field(description="English display name of the shelter facility.")
    type: ShelterType = Field(description="Facility category of the shelter.")
    location: Coordinates = Field(
        description="Shelter location in WGS84 (EPSG:4326) latitude/longitude."
    )
    municipality_th: str = Field(
        description="Thai name of the municipality or campus that announced the shelter."
    )
    capacity: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Approximate capacity in persons where publicly announced; "
            "null when no figure was published."
        ),
    )
    source: str = Field(
        description="Public announcement that designated this facility as a shelter."
    )
    source_url: str = Field(description="URL of the shelter designation source.")
    coordinate_source: str = Field(
        description="OpenStreetMap element the coordinates were taken from."
    )
    coordinate_source_url: str = Field(description="URL of the OpenStreetMap element.")


class ShelterDatasetProvenance(BaseModel):
    """Describe license, retrieval, and accuracy provenance for the shelter dataset."""

    license: str = Field(
        description="License covering coordinates (ODbL) and shelter designations."
    )
    retrieved_date: date = Field(
        description="Date the dataset coordinates and designations were retrieved."
    )
    dataset_ref: str = Field(description="Committed dataset file that produced this response.")
    accuracy_note: str = Field(
        description="Coordinate accuracy and shelter activation caveats for the dataset."
    )


class SheltersResponse(BaseModel):
    """Model the response for the GET /api/shelters endpoint."""

    shelters: list[Shelter] = Field(description="Normalized list of evacuation shelters.")
    shelter_count: int = Field(description="Total number of shelters returned.")
    provenance: ShelterDatasetProvenance = Field(
        description="Dataset-level license, retrieval, and accuracy metadata."
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "shelters": [
                    {
                        "id": "osm-way-858854620",
                        "name_th": "ศูนย์กีฬาและนันทนาการ มหาวิทยาลัยสงขลานครินทร์ (ม.อ. หาดใหญ่)",
                        "name_en": (
                            "Prince of Songkla University Sports and Recreation Center "
                            "(PSU Sport Complex)"
                        ),
                        "type": "university",
                        "location": {"latitude": 7.0107159, "longitude": 100.5010529},
                        "municipality_th": "เทศบาลเมืองคอหงส์ / มหาวิทยาลัยสงขลานครินทร์",
                        "capacity": 3000,
                        "source": (
                            "Prime Minister's Office shelter announcement, "
                            "Nov 2025 Hat Yai flood (via Thairath); list compiled by Kapook"
                        ),
                        "source_url": "https://www.thairath.co.th/scoop/theissue/2897350",
                        "coordinate_source": "OpenStreetMap way/858854620",
                        "coordinate_source_url": "https://www.openstreetmap.org/way/858854620",
                    }
                ],
                "shelter_count": 1,
                "provenance": {
                    "license": (
                        "Coordinates (c) OpenStreetMap contributors, "
                        "Open Database License (ODbL) 1.0"
                    ),
                    "retrieved_date": "2026-06-12",
                    "dataset_ref": "shelters_hatyai.geojson",
                    "accuracy_note": (
                        "Coordinates are OpenStreetMap facility locations, "
                        "not surveyed shelter entrances."
                    ),
                },
            }
        }
    )

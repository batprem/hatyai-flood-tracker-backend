"""Tests for the GET /api/shelters endpoint."""

import unittest

from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas.shelters import ShelterType
from app.services.shelters import SHELTER_DATASET_REF

EXPECTED_SHELTER_COUNT = 8

EXPECTED_SHELTER_FIELDS = {
    "id",
    "name_th",
    "name_en",
    "type",
    "location",
    "municipality_th",
    "capacity",
    "source",
    "source_url",
    "coordinate_source",
    "coordinate_source_url",
}

EXPECTED_PROVENANCE_FIELDS = {
    "license",
    "retrieved_date",
    "dataset_ref",
    "accuracy_note",
}


class SheltersApiTests(unittest.TestCase):
    def _get_payload(self) -> dict:
        app = create_app()
        with TestClient(app) as http:
            response = http.get("/api/shelters")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def test_response_shape_is_normalized_not_raw_geojson(self) -> None:
        payload = self._get_payload()
        self.assertEqual(set(payload), {"shelters", "shelter_count", "provenance"})
        # No raw GeoJSON passthrough keys at any level of the response.
        self.assertNotIn("features", payload)
        for shelter in payload["shelters"]:
            self.assertEqual(set(shelter), EXPECTED_SHELTER_FIELDS)
            self.assertNotIn("geometry", shelter)
            self.assertNotIn("properties", shelter)

    def test_all_eight_shelters_present_with_unique_ids(self) -> None:
        payload = self._get_payload()
        self.assertEqual(len(payload["shelters"]), EXPECTED_SHELTER_COUNT)
        self.assertEqual(payload["shelter_count"], EXPECTED_SHELTER_COUNT)
        ids = [shelter["id"] for shelter in payload["shelters"]]
        self.assertEqual(len(set(ids)), EXPECTED_SHELTER_COUNT)
        for shelter_id in ids:
            self.assertRegex(shelter_id, r"^osm-(node|way|relation)-\d+$")

    def test_shelter_types_are_valid_enum_values(self) -> None:
        payload = self._get_payload()
        valid_types = {shelter_type.value for shelter_type in ShelterType}
        for shelter in payload["shelters"]:
            self.assertIn(shelter["type"], valid_types)

    def test_locations_are_within_hat_yai_area(self) -> None:
        payload = self._get_payload()
        for shelter in payload["shelters"]:
            location = shelter["location"]
            self.assertGreaterEqual(location["latitude"], 6.9)
            self.assertLessEqual(location["latitude"], 7.1)
            self.assertGreaterEqual(location["longitude"], 100.4)
            self.assertLessEqual(location["longitude"], 100.6)

    def test_known_shelter_fields_are_normalized(self) -> None:
        payload = self._get_payload()
        by_id = {shelter["id"]: shelter for shelter in payload["shelters"]}
        psu = by_id["osm-way-858854620"]
        self.assertEqual(psu["type"], "university")
        self.assertEqual(psu["capacity"], 3000)
        self.assertIn("Prince of Songkla University", psu["name_en"])
        self.assertTrue(psu["name_th"])
        self.assertTrue(psu["source"])
        self.assertTrue(psu["source_url"].startswith("https://"))
        self.assertTrue(psu["coordinate_source_url"].startswith("https://"))

    def test_provenance_block_is_populated(self) -> None:
        payload = self._get_payload()
        provenance = payload["provenance"]
        self.assertEqual(set(provenance), EXPECTED_PROVENANCE_FIELDS)
        self.assertEqual(provenance["dataset_ref"], SHELTER_DATASET_REF)
        self.assertIn("OpenStreetMap", provenance["license"])
        self.assertIn("Open Database License", provenance["license"])
        self.assertRegex(provenance["retrieved_date"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(provenance["accuracy_note"])


if __name__ == "__main__":
    unittest.main()

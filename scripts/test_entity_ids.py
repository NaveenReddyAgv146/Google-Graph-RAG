"""Test entity IDs generation for Google Slides entities."""

import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

from common.taxonomy import generate_entity_id

entities = [
    ("SUBSYSTEM", "Google Slides Pipeline"),
    ("API", "Google Drive Webhooks"),
    ("API", "Google Slides API"),
    ("SUBSYSTEM", "PDF Ingestion Pipeline"),
    ("STANDARD", "Unified Graph Schema"),
]

for cat, name in entities:
    eid = generate_entity_id(cat, name)
    print(f"{name} ({cat}) -> {eid}")

"""Check existing entities and technologies in Spanner."""
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/naveen/.config/gcloud/application_default_credentials.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "xenon-broker-513704-s9"

from common.config import settings
from google.cloud import spanner

db = spanner.Client(project=settings.project_id).instance(settings.spanner_instance_id).database(settings.spanner_database_id)
with db.snapshot(multi_use=True) as s:
    ents = list(s.execute_sql("SELECT EntityId, Name, Category FROM Entities"))
    techs = list(s.execute_sql("SELECT TechId, Name, Category FROM Technologies"))
    eres = list(s.execute_sql("SELECT SourceEntityId, TargetEntityId FROM EntityReferencesEntity"))

with open("/home/naveen/enterprise-graphrag/scripts/entities_report.txt", "w") as f:
    f.write(f"ENTITIES ({len(ents)}):\n")
    for e in ents:
        f.write(f"  {e[0]}: {e[1]} ({e[2]})\n")
    f.write(f"\nTECHNOLOGIES ({len(techs)}):\n")
    for t in techs:
        f.write(f"  {t[0]}: {t[1]} ({t[2]})\n")
    f.write(f"\nENTITY_REFERENCES_ENTITY ({len(eres)}):\n")
    for ere in eres:
        f.write(f"  {ere[0]} -> {ere[1]}\n")
print("Done writing report.")

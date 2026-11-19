"""Check GCS staging and canonical existence."""
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
sys.path.insert(0, _repo_root)
sys.path.insert(0, _common_src)

os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/home/naveen/.config/gcloud/application_default_credentials.json"
os.environ["GOOGLE_CLOUD_PROJECT"] = "xenon-broker-513704-s9"

from common.config import settings
from google.cloud import storage

client = storage.Client(project=settings.project_id)
bucket = client.bucket(settings.gcs_mumbai_bucket)
doc_id = "doc_slide_1oOHTB9O5s8nqMhgYEgp8inq2wqZSY1S9ysYgTn07-BQ"
can = bucket.blob(f"metadata/{doc_id}.meta.json").exists()
stg = bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp").exists()
print(f"Canonical exists: {can}")
print(f"Staging exists:   {stg}")
if stg:
    print("Deleting leftover staging blob from previous aborted attempt...")
    bucket.blob(f"metadata/staging/{doc_id}.meta.json.tmp").delete()
    print("Deleted staging blob successfully.")

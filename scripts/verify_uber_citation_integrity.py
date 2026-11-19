"""Citation integrity and claim-level provenance verification for Uber - 2008 (1).pdf (1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_)."""

import hashlib
import json
import os
import sys

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_common_src = os.path.join(_repo_root, "common", "src")
_ingestion_src = os.path.join(_repo_root, "ingestion-copilot")

sys.path.insert(0, _repo_root)
sys.path.insert(0, _ingestion_src)
sys.path.insert(0, _common_src)

from common.config import settings
from google.cloud import spanner
from app.enrichment_orchestrator import generate_evidence_link_id

doc_id = "1s0DCTjCu0GIFW1Kmj-Yt1FnQSAxsiHN_"
card_id = "card_0836a0e400d5b36bfb7a16204a1a964604704563f4b3843d"

spanner_client = spanner.Client(project=settings.project_id)
db = spanner_client.instance(settings.spanner_instance_id).database(settings.spanner_database_id)

# Fetch all text chunks from Spanner
with db.snapshot(multi_use=True) as s:
    rows = list(s.execute_sql(
        "SELECT ChunkId, PageNumber, TextContent FROM EvidenceChunks WHERE DocumentId = @d AND Modality = 'TEXT' ORDER BY PageNumber, ChunkId",
        params={"d": doc_id},
        param_types={"d": spanner.param_types.STRING}
    ))

chunks_by_id = {r[0]: (r[1], r[2]) for r in rows}

print(f"Total TEXT chunks fetched from Spanner: {len(chunks_by_id)}")

# Define the SolutionCard claims from recalculate_uber_audit.py
claims = [
    {
        "section": "Business problem",
        "claim": "Traditional cabs in 2008 use aging, inefficient technology like radio dispatch with no 2-way communication, low fuel efficiency, and manual hailing with no GPS coordination, leading to high dead-time and taxi-monopoly medallion lock-in.",
        "citations": [
            {
                "chunk_id": "chunk-165f0bd0d002a0894536ae7f4d776dd7c7c402a8d5f75fbb",
                "page": 2,
                "quote": "Most use aging & inefficient technology\n\n   —  Radio dispatch, no 2-way communication\n\n   —  Most common car, Ford Crown Victoria = 14mpg\n\n *  Hailing is done by hand or phone\n\n   — No GPS coordination between client/driver\n\n   —  Significant fareseeking or “dead-time”",
            },
            {
                "chunk_id": "chunk-2a4aa82038e62e9e55ebd7540a9a55fcf5c17ac11cb4a53a",
                "page": 3,
                "quote": "Taxi-monopolies reduce quality of service\n   — Medallions are expensive, and drivers underpaid\n\n\n\n\n\n   — Medallions cost ~$500k, drivers make 31k\n   — No incentive/accountability for drivers/clients",
            }
        ]
    },
    {
        "section": "Proposed solution",
        "claim": "A fast and efficient on-demand car service utilizing modern consumer web and device technology to automate dispatch, optimize fleets, and reduce wait times (the 'NetJets of car services').",
        "citations": [
            {
                "chunk_id": "chunk-992b8af588fb82f31b227ca0927b6ebb3d801f46069ea6a6",
                "page": 4,
                "quote": "A fast & efficient on-demand car service\n\n *  Market: Professionals in American cities\n\n * Convenience of a cab in NYC + experience of a\n   professional chauffeur. But in SF and NYC\n\n *  Latest consumer web & device technology\n  — automate dispatch to reduce wait-time\n\n *  Optimized fleets and incented drivers\n\n * The “NetJets of car services”",
            }
        ]
    },
    {
        "section": "Implemented solution",
        "claim": "1-Click request from Geo-aware devices, SMS booking ('pickup @work in 5'), mobile applications for iPhone, Blackberry, and Symbian, a web platform (UberCab.com) integrated with Google Maps, and a pre-paid, cashless billing system.",
        "citations": [
            {
                "chunk_id": "chunk-5ad2bc14e3124353e231112e4572a6de533773cef9c235da",
                "page": 8,
                "quote": "1-Click request from Geo-aware devices\n\n * SMS from any phone: “pickup @work in 5”",
            },
            {
                "chunk_id": "chunk-1e05081527a9128320b8bdd265376cc2c2bf547506e1a296",
                "page": 15,
                "quote": "Applications for iphone, blackberry, symbian",
            },
            {
                "chunk_id": "chunk-c3bfbc01346353aa942367ece60c5575d1eee725bdb0c525",
                "page": 9,
                "quote": "UberCab.com\n\n\n * Book Trips, show Fleet status, trip history\n\n *  Pre-specify locations with labels + coordinates\n   —  To enable easy texting of pickup locations\n\n * Google Maps integration",
            },
            {
                "chunk_id": "chunk-be54730f59a71324628abbf28eac871599b0abd17af7ec62",
                "page": 7,
                "quote": "Pre-paid, cashless billing system",
            }
        ]
    },
    {
        "section": "Projected outcomes",
        "claim": "Projected Outcomes: Best-case scenario projects becoming market leader with $1B+ in yearly revenue; realistic success scenario projects capturing 5% of top 5 US cities generating $20-30M+ per year in profit; worst-case scenario projects remaining a 10-car niche service in SF.",
        "citations": [
            {
                "chunk_id": "chunk-6d660e6633fa4738d44735ffc6dbecec9e6fd97ecba7864e",
                "page": 20,
                "quote": "Best-Case Scenario\n\n   — Becomes market leader, $1B+ in yearly revenue\n\n *  Realistic Success Scenario\n\n   —  Gets 5% of the top 5 US Cities\n\n   —  Generates 20-30M+ per year profit\n\n * Worst-Case Scenario\n\n   —  Remains a 10 car, 100 client service in SF\n\n   —  Time-saver for San-Francisco based executives",
            }
        ]
    }
]

print("\n=== CLAIM-LEVEL PROVENANCE AUDIT ===")
all_sections_pass = True
for c in claims:
    sec = c["section"]
    print(f"\n--- Section: {sec} ---")
    print(f"Claim: {c['claim']}")
    sec_pass = True
    for cit in c["citations"]:
        cid = cit["chunk_id"]
        quote = cit["quote"]
        if cid not in chunks_by_id:
            print(f"  FAIL: Chunk {cid} not found in Spanner EvidenceChunks!")
            sec_pass = False
            continue
        page, chunk_text = chunks_by_id[cid]
        quote_clean = " ".join(quote.split()).lower()
        chunk_clean = " ".join(chunk_text.split()).lower()
        found = quote_clean in chunk_clean
        print(f"  Citation: Chunk={cid} (Page {page})")
        print(f"    Verbatim Quote: {quote!r}")
        print(f"    In Chunk Text: {found}")
        if not found:
            sec_pass = False
    print(f"Section Verdict: {'PASS' if sec_pass else 'FAIL'}")
    if not sec_pass:
        all_sections_pass = False

print(f"\nAll 4 Sections Pass: {all_sections_pass}")

# Verify EvidenceLinks design vs Sidecar Provenance
# In Spanner: EvidenceLinks schema stores primary grounding link per card:
primary_chunk_id = "chunk-992b8af588fb82f31b227ca0927b6ebb3d801f46069ea6a6"
primary_quote = "A fast & efficient on-demand car service\n\n *  Market: Professionals in American cities\n\n * Convenience of a cab in NYC + experience of a\n   professional chauffeur. But in SF and NYC\n\n *  Latest consumer web & device technology\n  — automate dispatch to reduce wait-time"
ev_id = generate_evidence_link_id(card_id, primary_chunk_id, primary_quote)

print(f"\n=== RELATIONAL EVIDENCE LINK ===")
print(f"EvidenceId:   {ev_id}")
print(f"CardId:       {card_id}")
print(f"DocumentId:   {doc_id}")
print(f"ChunkId:      {primary_chunk_id}")
print(f"Primary Quote:{primary_quote!r}")
in_primary = " ".join(primary_quote.split()).lower() in " ".join(chunks_by_id[primary_chunk_id][1].split()).lower()
print(f"Grounding verified in Spanner: {in_primary}")

# Verify Sidecar Manifest Hash and OkfUri consistency
from scripts.recalculate_uber_audit import (
    metadata_result,
    orchestrator,
    eligible_chunks,
    sidecar_bytes,
    sidecar_sha256,
    sidecar_uri,
    canonical_okf_uri,
    projected_total,
    writer_res,
)

print(f"\n=== SIDECAR HASH & OKF_URI CONSISTENCY ===")
computed_sha = hashlib.sha256(sidecar_bytes).hexdigest()
print(f"Computed Sidecar SHA256: {computed_sha}")
print(f"Reported Sidecar SHA256: {sidecar_sha256}")
print(f"SHA Match:               {computed_sha == sidecar_sha256}")
print(f"Canonical GCS URI:       {sidecar_uri}")
print(f"Projected OkfUri:        {canonical_okf_uri}")
okf_hash = canonical_okf_uri.split("#sha256=")[-1]
print(f"OkfUri Hash Matches:     {okf_hash == computed_sha}")

print(f"\n=== MUTATION PROJECTION CONFIRMATION ===")
print(f"Orchestrator Projected Mutations: {projected_total} (expected 43)")
print(f"Writer Buffered Mutations:       {writer_res.total_mutations} (expected 43)")
print(f"Mutation Projection Exact Match: {projected_total == 43 and writer_res.total_mutations == 43}")


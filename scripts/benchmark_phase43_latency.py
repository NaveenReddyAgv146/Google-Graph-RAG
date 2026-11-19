"""Benchmark Phase 4.3 latency for EvidenceSelector, CitationValidator, and Synthesizer."""

import time
import numpy as np

from app.synthesis.citation_validator import CitationValidator
from app.synthesis.evidence_selector import EvidenceSelector
from app.synthesis.models import (
    AuthoritativeCardEvidence,
    AuthoritativeEvidenceChunk,
    ClaimType,
    EvidencePack,
    EvidenceReference,
    GroundedClaim,
    SynthesisResult,
)
from app.synthesis.synthesizer import GroundedAnswerSynthesizer


def run_benchmark():
    chunks = [
        AuthoritativeEvidenceChunk(
            chunk_id=f"chk_{i}",
            document_id="doc_uber",
            card_id="card_uber",
            customer_name="Uber",
            page_number=i + 1,
            text_content=f"Chunk {i} describes mobile request dispatch and GPS coordination with 14mpg Crown Victoria fleet.",
        )
        for i in range(10)
    ]
    card = AuthoritativeCardEvidence(
        card_id="card_uber",
        document_id="doc_uber",
        project_name="Uber Dispatch",
        customer_name="Uber",
        evidence_chunks=chunks,
    )
    pack = EvidencePack(
        query="How did Uber improve dispatch?",
        intent="SOLUTION_DISCOVERY",
        cards=[card],
        chunks_by_id={c.chunk_id: c for c in chunks},
        total_cards=1,
        total_chunks=10,
    )

    # 1. Benchmark CitationValidator
    validator = CitationValidator()
    synth_res = SynthesisResult(
        summary="Uber deployed mobile request dispatch and automated fleet tracking.",
        claims=[
            GroundedClaim(
                statement=f"Uber deployed feature {i} to automate dispatch.",
                claim_type=ClaimType.VERIFIED_FACT,
                customer_name="Uber",
                evidence_refs=[
                    EvidenceReference(
                        card_id="card_uber",
                        chunk_id=f"chk_{i}",
                        document_id="doc_uber",
                        customer_name="Uber",
                        page_number=i + 1,
                        exact_source_quote="mobile request dispatch",
                    )
                ],
            )
            for i in range(5)
        ],
    )

    val_latencies = []
    for _ in range(100):
        t0 = time.perf_counter()
        _ = validator.validate_synthesis(synth_res, pack)
        val_latencies.append((time.perf_counter() - t0) * 1000.0)

    # 2. Benchmark EvidenceSelector
    selector = EvidenceSelector()
    raw_subtasks = {
        "task_1": {
            "status": "success",
            "solutions": [
                {
                    "card_id": "card_uber",
                    "project_name": "Uber Dispatch",
                    "verification_status": "APPROVED",
                    "document_id": "doc_uber",
                    "citations": [{"chunk_id": "chk_1", "source_chunk_text": "mobile request dispatch"}],
                }
            ],
        }
    }
    sel_latencies = []
    for _ in range(100):
        t0 = time.perf_counter()
        _ = selector.select_evidence("test", "SOLUTION_DISCOVERY", raw_subtasks)
        sel_latencies.append((time.perf_counter() - t0) * 1000.0)

    # 3. Benchmark Deterministic Fallback Synthesis
    synth = GroundedAnswerSynthesizer()
    synth_latencies = []
    for _ in range(100):
        t0 = time.perf_counter()
        _ = synth._build_deterministic_fallback_synthesis("test", pack, 0.0, "test")
        synth_latencies.append((time.perf_counter() - t0) * 1000.0)

    print("=== Phase 4.3 Benchmark Results (100 runs) ===")
    print(f"EvidenceSelector:   p50={np.percentile(sel_latencies, 50):.3f}ms, p95={np.percentile(sel_latencies, 95):.3f}ms")
    print(f"CitationValidator:  p50={np.percentile(val_latencies, 50):.3f}ms, p95={np.percentile(val_latencies, 95):.3f}ms")
    print(f"DeterministicSynth: p50={np.percentile(synth_latencies, 50):.3f}ms, p95={np.percentile(synth_latencies, 95):.3f}ms")
    total_local = np.array(sel_latencies) + np.array(val_latencies) + np.array(synth_latencies)
    print(f"Total Local Core:   p50={np.percentile(total_local, 50):.3f}ms, p95={np.percentile(total_local, 95):.3f}ms")


if __name__ == "__main__":
    run_benchmark()


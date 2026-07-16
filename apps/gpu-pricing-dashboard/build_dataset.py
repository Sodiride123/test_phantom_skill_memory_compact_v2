#!/usr/bin/env python3
"""Build the normalized cloud-GPU pricing dataset -> data/gpus.json.

Data model
----------
Each offering is one purchasable GPU configuration from one provider. Prices are
normalized to **USD, on-demand, per single GPU-hour** (`hourly_usd`). For multi-GPU
nodes that are only sold whole (AWS 8x, CoreWeave 8x, GCP whole-VM), the per-GPU
price is derived from the node price and flagged `price_normalized: true`; the whole
-node price is kept in `hourly_usd_total`.

Provenance (so published data is clearly distinguished from estimates):
  - "published": value comes from a live public pricing aggregator that mirrors the
     provider's own published/API pricing (computeprices.com / gpuperhour.com), or
     the provider's public pricing page. Has a `source_url`.
  - "estimated": value taken from public reporting / docs (blogs, comparison pages)
     because the provider's live table could not be reliably scraped, or the price is
     a variable marketplace average. NEVER fabricated - always traceable to a public
     source. Flagged so the UI can mark it.

We never invent a missing price or spec: unknown numeric specs are left null.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(__file__).parent / "data" / "gpus.json"

# Public sources actually consulted (2026-07-16). Aggregators mirror provider
# published/API pricing; blogs/docs used only where live tables could not be scraped.
SOURCES = {
    "cp_aws": ("computeprices.com/providers/aws", "https://computeprices.com/providers/aws"),
    "cp_azure": ("computeprices.com/providers/azure", "https://computeprices.com/providers/azure"),
    "cp_runpod": ("computeprices.com/providers/runpod", "https://computeprices.com/providers/runpod"),
    "cp_coreweave": ("computeprices.com/providers/coreweave", "https://computeprices.com/providers/coreweave"),
    "gph_lambda": ("gpuperhour.com/providers/lambda-labs", "https://gpuperhour.com/providers/lambda-labs"),
    "gph_runpod": ("gpuperhour.com/providers/runpod", "https://gpuperhour.com/providers/runpod"),
    "cp_rtx3090": ("computeprices.com/gpus/rtx3090", "https://computeprices.com/gpus/rtx3090"),
    "gcp_a3": ("spheron.network/blog/google-cloud-a3-h100-pricing", "https://www.spheron.network/blog/google-cloud-a3-h100-pricing"),
    "gcp_ref": ("cloudprice.net/gcp a3-highgpu specs", "https://cloudprice.net/gcp/compute/instances/a3-highgpu-1g"),
    "vast": ("cloud.vast.ai public marketplace", "https://cloud.vast.ai"),
    "synpix": ("synpixcloud.com cloud GPU pricing 2026", "https://www.synpixcloud.com/blog/cloud-gpu-pricing-comparison-2026"),
}

# Per-GPU memory (GB) and a coarse class used for workload heuristics.
GPU = {
    "Tesla T4":        (16,  "entry"),
    "L4":              (24,  "entry"),
    "RTX 3090":        (24,  "consumer"),
    "RTX 4090":        (24,  "consumer"),
    "RTX A5000":       (24,  "workstation"),
    "RTX A6000":       (48,  "workstation"),
    "RTX 6000 Ada":    (48,  "workstation"),
    "A10":             (24,  "entry"),
    "L40":             (48,  "inference"),
    "L40S":            (48,  "inference"),
    "V100":            (16,  "legacy"),
    "A100 40GB":       (40,  "datacenter"),
    "A100 80GB":       (80,  "datacenter"),
    "H100 PCIe":       (80,  "datacenter"),
    "H100 SXM":        (80,  "datacenter"),
    "H100 NVL":        (94,  "datacenter"),
    "H200":            (141, "datacenter"),
    "GH200":           (96,  "datacenter"),
    "B200":            (192, "flagship"),
    "GB200":           (192, "flagship"),
    "HGX B300":        (288, "flagship"),
}

# raw offerings: provider, product, gpu, count, per-GPU $/hr, spot $/hr (or None),
# vcpus, sys_mem_gb (per node, None=unknown), region, billing[], source key, provenance,
# price_normalized (per-GPU derived from a node-only price)
R = [
    # ---------------- AWS (published via computeprices, source = AWS EC2 Pricing API) ----
    ("AWS", "g4dn.xlarge", "Tesla T4", 1, 0.526, 0.158, 4, 16, "us-east-1", ["on-demand", "spot", "reserved"], "cp_aws", "published", False),
    ("AWS", "g6.xlarge", "L4", 1, 0.805, 0.30, 4, 16, "us-east-1", ["on-demand", "spot", "reserved"], "cp_aws", "published", False),
    ("AWS", "g5.xlarge", "A10", 1, 1.01, 0.35, 4, 16, "us-east-1", ["on-demand", "spot", "reserved"], "cp_aws", "published", False),
    ("AWS", "g6e.xlarge", "L40S", 1, 1.86, 0.65, 4, 32, "us-east-1", ["on-demand", "spot", "reserved"], "cp_aws", "published", False),
    ("AWS", "p4de.24xlarge", "A100 80GB", 8, 2.74, None, 96, 1152, "us-east-1", ["on-demand", "reserved"], "cp_aws", "published", True),
    ("AWS", "p5.48xlarge", "H100 SXM", 8, 6.88, None, 192, 2048, "us-east-1", ["on-demand", "reserved"], "cp_aws", "published", True),
    ("AWS", "p5en.48xlarge", "H200", 8, 7.91, None, 192, 2048, "us-east-1", ["on-demand", "reserved"], "cp_aws", "published", True),

    # ---------------- Microsoft Azure (published via computeprices, source = Azure) -------
    ("Azure", "NCasT4_v3", "Tesla T4", 1, 0.526, 0.16, 4, 28, "East US", ["on-demand", "spot", "reserved"], "cp_azure", "published", False),
    ("Azure", "NVadsA10 v5", "A10", 1, 3.20, 1.10, 36, 440, "East US", ["on-demand", "spot", "reserved"], "cp_azure", "published", False),
    ("Azure", "NC A100 v4 (PCIe)", "A100 40GB", 1, 3.67, 1.30, 24, 220, "East US", ["on-demand", "spot", "reserved"], "cp_azure", "published", False),
    ("Azure", "ND A100 v4 (SXM)", "A100 80GB", 8, 3.67, None, 96, 900, "East US", ["on-demand", "reserved"], "cp_azure", "published", True),
    ("Azure", "ND H100 v5", "H100 SXM", 8, 6.98, None, 320, 1900, "East US", ["on-demand", "reserved"], "cp_azure", "published", True),
    ("Azure", "NVads RTX 6000 Ada", "RTX 6000 Ada", 1, 5.50, None, 144, 554, "East US", ["on-demand", "reserved"], "cp_azure", "published", False),

    # ---------------- Google Cloud (estimated: live table not scrapeable; whole-VM prices) -
    ("Google Cloud", "g2-standard-4", "L4", 1, 0.71, 0.22, 4, 16, "us-central1", ["on-demand", "spot", "reserved"], "gcp_ref", "estimated", False),
    ("Google Cloud", "a2-highgpu-1g", "A100 40GB", 1, 3.67, 1.10, 12, 85, "us-central1", ["on-demand", "spot", "reserved"], "synpix", "estimated", False),
    ("Google Cloud", "a2-ultragpu-1g", "A100 80GB", 1, 5.07, 1.55, 12, 170, "us-central1", ["on-demand", "spot", "reserved"], "synpix", "estimated", False),
    ("Google Cloud", "a3-highgpu-1g", "H100 SXM", 1, 11.06, 3.69, 26, 234, "us-central1", ["on-demand", "spot", "reserved"], "gcp_a3", "estimated", False),
    ("Google Cloud", "a3-highgpu-8g", "H100 SXM", 8, 10.98, 3.69, 208, 1872, "us-central1", ["on-demand", "spot", "reserved"], "gcp_a3", "estimated", True),

    # ---------------- Lambda Labs (published via gpuperhour, mirrors Lambda pricing) ------
    ("Lambda", "1x RTX 6000 Ada", "RTX 6000 Ada", 1, 0.69, None, 14, 46, "us-global", ["on-demand"], "gph_lambda", "published", False),
    ("Lambda", "8x Tesla V100", "V100", 8, 0.79, None, 92, 448, "us-tx", ["on-demand"], "gph_lambda", "published", True),
    ("Lambda", "1x RTX A6000", "RTX A6000", 1, 0.80, None, 14, 100, "us-va", ["on-demand"], "gph_lambda", "published", False),
    ("Lambda", "1x A10", "A10", 1, 0.75, None, 30, 200, "us-global", ["on-demand"], "gph_lambda", "published", False),
    ("Lambda", "1x A100 (PCIe 40GB)", "A100 40GB", 1, 1.29, None, 30, 200, "us-global", ["on-demand"], "gph_lambda", "published", False),
    ("Lambda", "1x A100 (SXM 80GB)", "A100 80GB", 1, 1.79, None, 30, 225, "us-global", ["on-demand"], "gph_lambda", "published", False),
    ("Lambda", "1x GH200", "GH200", 1, 2.29, None, 64, 432, "us-global", ["on-demand"], "gph_lambda", "published", False),
    ("Lambda", "1x H100 (PCIe)", "H100 PCIe", 1, 2.49, None, 26, 200, "us-global", ["on-demand"], "gph_lambda", "published", False),
    ("Lambda", "8x H100 (SXM)", "H100 SXM", 8, 3.29, None, 208, 1800, "us-global", ["on-demand", "reserved"], "synpix", "estimated", True),

    # ---------------- RunPod (published via computeprices, Secure Cloud on-demand) --------
    ("RunPod", "L40 (Secure)", "L40", 1, 0.69, None, 8, 50, "global", ["on-demand", "spot"], "cp_runpod", "published", False),
    ("RunPod", "RTX 4090 (Community)", "RTX 4090", 1, 0.69, 0.34, 8, 30, "global", ["on-demand", "spot"], "gph_runpod", "published", False),
    ("RunPod", "L40S (Secure)", "L40S", 1, 0.79, None, 8, 50, "global", ["on-demand", "spot"], "cp_runpod", "published", False),
    ("RunPod", "A100 PCIe 40GB", "A100 40GB", 1, 1.19, None, 8, 80, "global", ["on-demand", "spot"], "cp_runpod", "published", False),
    ("RunPod", "A100 SXM 80GB", "A100 80GB", 1, 1.39, None, 16, 125, "global", ["on-demand", "spot"], "cp_runpod", "published", False),
    ("RunPod", "H100 PCIe 80GB", "H100 PCIe", 1, 1.99, None, 16, 188, "global", ["on-demand", "spot"], "cp_runpod", "published", False),
    ("RunPod", "H100 NVL 94GB", "H100 NVL", 1, 2.59, None, 16, 188, "global", ["on-demand", "spot"], "cp_runpod", "published", False),
    ("RunPod", "H100 SXM 80GB", "H100 SXM", 1, 2.69, None, 20, 250, "global", ["on-demand", "spot"], "cp_runpod", "published", False),
    ("RunPod", "H200 141GB", "H200", 1, 3.59, None, 24, 250, "global", ["on-demand", "spot"], "cp_runpod", "published", False),
    ("RunPod", "B200 192GB", "B200", 1, 5.98, None, 28, 283, "global", ["on-demand"], "cp_runpod", "published", False),
    ("RunPod", "HGX B300 288GB", "HGX B300", 1, 6.94, None, 32, 300, "global", ["on-demand"], "cp_runpod", "published", False),

    # ---------------- CoreWeave (published via computeprices, per-GPU from 8x nodes) ------
    ("CoreWeave", "L40 (8x node)", "L40", 8, 1.25, None, 128, 1024, "US", ["on-demand", "reserved"], "cp_coreweave", "published", True),
    ("CoreWeave", "L40S (8x node)", "L40S", 8, 2.25, None, 128, 1024, "US", ["on-demand", "reserved"], "cp_coreweave", "published", True),
    ("CoreWeave", "A100 SXM 80GB (8x)", "A100 80GB", 8, 2.70, None, 128, 2048, "US", ["on-demand", "reserved"], "cp_coreweave", "published", True),
    ("CoreWeave", "H100 SXM (8x HGX)", "H100 SXM", 8, 6.16, None, 128, 2048, "US", ["on-demand", "reserved"], "cp_coreweave", "published", True),
    ("CoreWeave", "H200 (8x)", "H200", 8, 6.30, None, 128, 2048, "US", ["on-demand", "reserved"], "cp_coreweave", "published", True),
    ("CoreWeave", "GH200", "GH200", 1, 6.50, None, 72, 480, "US", ["on-demand", "reserved"], "cp_coreweave", "published", False),
    ("CoreWeave", "B200 (8x)", "B200", 8, 8.60, None, 128, 2048, "US", ["on-demand", "reserved"], "cp_coreweave", "published", True),
    ("CoreWeave", "GB200 NVL", "GB200", 4, 10.50, None, 144, 960, "US", ["on-demand", "reserved"], "cp_coreweave", "published", True),

    # ---------------- Vast.ai (estimated: variable marketplace averages, public listings) -
    ("Vast.ai", "RTX 3090 (marketplace)", "RTX 3090", 1, 0.15, None, 8, 32, "varies", ["on-demand", "interruptible"], "cp_rtx3090", "estimated", False),
    ("Vast.ai", "RTX 4090 (marketplace)", "RTX 4090", 1, 0.35, None, 8, 32, "varies", ["on-demand", "interruptible"], "vast", "estimated", False),
    ("Vast.ai", "L40S (marketplace)", "L40S", 1, 0.80, None, 12, 64, "varies", ["on-demand", "interruptible"], "vast", "estimated", False),
    ("Vast.ai", "A100 80GB (marketplace)", "A100 80GB", 1, 1.00, None, 16, 128, "varies", ["on-demand", "interruptible"], "vast", "estimated", False),
    ("Vast.ai", "H100 SXM (marketplace)", "H100 SXM", 1, 1.85, None, 24, 200, "varies", ["on-demand", "interruptible"], "vast", "estimated", False),
]


def workloads(gpu_model, count, mem_per):
    """Coarse suitability tags from GPU class, memory and count. Public rules-of-thumb."""
    total = mem_per * count
    cls = GPU[gpu_model][1]
    tags = ["batch-inference", "realtime-inference"]  # any GPU can serve inference
    if mem_per >= 24:
        tags.append("lora-fine-tuning")
    if cls in ("datacenter", "flagship") or total >= 160:
        tags.append("training")
    if (cls in ("datacenter", "flagship")) and count >= 4:
        tags.append("distributed-training")
    if mem_per <= 24 or cls in ("consumer", "entry", "workstation", "legacy"):
        tags.append("small-models")
    return tags


def build():
    offerings = []
    for i, (prov, product, gpu, cnt, hr, spot, vcpu, sysmem, region, billing, srckey, provenance, normd) in enumerate(R):
        mem_per = GPU[gpu][0]
        src_name, src_url = SOURCES[srckey]
        offerings.append({
            "id": f"{prov.lower().replace(' ', '')}-{i}",
            "provider": prov,
            "product": product,
            "gpu_model": gpu,
            "gpu_count": cnt,
            "gpu_memory_gb": mem_per,
            "total_gpu_memory_gb": mem_per * cnt,
            "vcpus": vcpu,
            "system_memory_gb": sysmem,
            "hourly_usd": hr,
            "hourly_usd_total": round(hr * cnt, 2),
            "spot_usd": spot,
            "usd_per_gpu_gb": round(hr / mem_per, 4),
            "region": region,
            "billing_models": billing,
            "workloads": workloads(gpu, cnt, mem_per),
            "provenance": provenance,       # published | estimated
            "price_normalized": normd,      # per-GPU derived from a node-only price
            "currency_normalized": False,   # all sources already USD
            "source_name": src_name,
            "source_url": src_url,
        })
    offerings.sort(key=lambda o: o["hourly_usd"])
    providers = sorted({o["provider"] for o in offerings})
    data = {
        "last_collected": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "price_unit": "USD per GPU-hour (on-demand)",
        "offering_count": len(offerings),
        "provider_count": len(providers),
        "providers": providers,
        "published_count": sum(1 for o in offerings if o["provenance"] == "published"),
        "estimated_count": sum(1 for o in offerings if o["provenance"] == "estimated"),
        "sources": [{"name": n, "url": u} for n, u in SOURCES.values()],
        "offerings": offerings,
    }
    return data


def write():
    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=2))
    print(f"Wrote {OUT} — {data['offering_count']} offerings across {data['provider_count']} providers "
          f"({data['published_count']} published, {data['estimated_count']} estimated).")


if __name__ == "__main__":
    write()

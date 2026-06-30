"""
quality_assessment.py — Ollama-powered qualitative judgment of discovery candidates

Optional feature. Entirely gated on OLLAMA_URL being set in the environment.
With no OLLAMA_URL, get_assessment() is never called by the API layer at all —
the Discovery tab behaves exactly as it does without this module existing.

The LLM is given the deterministic metrics from quality_metrics.py ALONGSIDE
the raw content sample — it synthesizes and judges, it never recomputes counts
or regex matches itself. Models are unreliable at that; they're good at reading
a sample and recognizing whether it looks deliberately maintained vs. dumped,
and at spotting contextual patterns (e.g. a list padded with near-identical
domains) that single-entry regex can't catch.

Any failure (unreachable Ollama, timeout, malformed JSON response) falls back
to a safe "review" recommendation with the deterministic metrics still shown —
this must never block or crash the existing approve/dismiss flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Literal

import aiohttp
from pydantic import BaseModel, Field, field_validator

from .quality_metrics import DeterministicMetrics

logger = logging.getLogger(__name__)

OLLAMA_TIMEOUT = aiohttp.ClientTimeout(total=60)
MAX_SAMPLE_CHARS = 2000   # keep the prompt small and fast — a representative sample, not the whole file

# Some models (e.g. reasoning models like deepseek-r1) wrap output in <think>...</think>
# before the actual answer. Strip it defensively regardless of which model is configured.
THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


class QualityAssessment(BaseModel):
    overall_score:   int = Field(ge=0, le=100)
    recommendation:  Literal["approve", "review", "reject"] = "review"
    red_flags:       list[str] = []
    reasoning:       str = ""
    source:          Literal["ollama", "fallback"] = "ollama"

    @field_validator("overall_score", mode="before")
    @classmethod
    def clamp_score(cls, v):
        try:
            return max(0, min(int(v), 100))
        except (TypeError, ValueError):
            return 50


def _fallback(reason: str, metrics: DeterministicMetrics) -> QualityAssessment:
    return QualityAssessment(
        overall_score=metrics.format_score,
        recommendation="review",
        red_flags=metrics.red_flags,
        reasoning=f"LLM assessment unavailable ({reason}). Showing deterministic metrics only.",
        source="fallback",
    )


def _build_prompt(content_sample: str, metrics: DeterministicMetrics, label: str) -> str:
    protocol_summary = ", ".join(f"{k}: {v}" for k, v in metrics.protocol_counts.items()) or "none"
    freshness_line = (
        f"Last commit: {metrics.freshness_days} days ago"
        if metrics.freshness_days is not None else "Freshness: not applicable for this source type"
    )

    return f"""You are assessing the quality of a candidate BitTorrent tracker list for a tracker management tool.

Source label: {label}

Pre-computed metrics (already calculated, do not recompute):
- Format validity: {metrics.format_score}% of lines are well-formed tracker URLs
- Protocol distribution: {protocol_summary}
- Diversity score: {metrics.diversity_score}/100
- Overlap with existing known trackers: {metrics.overlap_pct}%
- {freshness_line}
- Already-detected red flags: {', '.join(metrics.red_flags) if metrics.red_flags else 'none'}

Raw content sample (may be truncated):
{content_sample[:MAX_SAMPLE_CHARS]}

Judge the OVERALL quality and trustworthiness of this source. Consider:
1. Does this look like a deliberately curated, maintained tracker list, or a low-effort scrape/dump?
2. Are there any suspicious patterns in the sample beyond what's already flagged - for example,
   near-identical or sequentially-named domains, hostnames that look randomly generated, or entries
   that seem designed to look legitimate but aren't?
3. Given everything above, should this source be approved, reviewed manually, or rejected?

Return ONLY a JSON object with this exact shape, no other text, no markdown formatting, no code fences:
{{"overall_score": <int 0-100>, "recommendation": "approve" or "review" or "reject", "red_flags": ["<any NEW flags you found, not already listed above>"], "reasoning": "<one to two sentences>"}}"""


def _parse_response(raw: str, metrics: DeterministicMetrics) -> QualityAssessment:
    cleaned = THINK_BLOCK.sub("", raw).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return _fallback("could not parse JSON response", metrics)
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return _fallback("could not parse JSON response", metrics)

    try:
        llm_flags = data.get("red_flags", []) or []
        merged_flags = list(dict.fromkeys(metrics.red_flags + llm_flags))
        return QualityAssessment(
            overall_score=data.get("overall_score", 50),
            recommendation=data.get("recommendation", "review"),
            red_flags=merged_flags,
            reasoning=data.get("reasoning", ""),
            source="ollama",
        )
    except Exception as exc:
        logger.warning("Ollama response did not match expected shape: %s", exc)
        return _fallback("response did not match expected shape", metrics)


async def get_assessment(
    ollama_url: str,
    model: str,
    content_sample: str,
    metrics: DeterministicMetrics,
    label: str = "",
) -> QualityAssessment:
    """
    Calls Ollama's /api/generate with the deterministic metrics + a content
    sample, and returns a structured QualityAssessment. Never raises —
    any failure produces a safe fallback assessment instead.
    """
    if not ollama_url or not model:
        return _fallback("Ollama not configured", metrics)

    prompt = _build_prompt(content_sample, metrics, label)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{ollama_url.rstrip('/')}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=OLLAMA_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.warning("Ollama returned HTTP %d: %s", resp.status, body[:300])
                    return _fallback(f"HTTP {resp.status}", metrics)
                data = await resp.json()
    except asyncio.TimeoutError:
        return _fallback("timed out", metrics)
    except Exception as exc:
        logger.warning("Ollama request failed: %s", exc)
        return _fallback(str(exc), metrics)

    raw_response = data.get("response", "")
    if not raw_response:
        return _fallback("empty response", metrics)

    return _parse_response(raw_response, metrics)

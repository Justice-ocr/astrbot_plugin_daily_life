"""根据已保存成果验收目标；投入时长与成果达成分别记录。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .presence import record_event


def review_candidates(world: dict[str, Any]) -> list[dict[str, Any]]:
    outcomes = (world.get("kernel") or {}).get("action_outcomes", [])
    current_outcomes = {
        item.get("action_id"): item
        for item in outcomes
        if isinstance(item, dict) and item.get("action_id")
    }
    candidates = []
    for goal in world.get("goals", []):
        if goal.get("status") != "active":
            continue
        completed = {
            step["id"] for step in goal["steps"] if step.get("status") == "completed"
        }
        for step in goal["steps"]:
            if (
                step.get("status") == "completed"
                or not set(step.get("depends_on", [])) <= completed
            ):
                continue
            ids = {item["action_id"] for item in step.get("evidence", [])}
            saved_sources = [
                item
                for item in step.get("review_evidence", [])
                if isinstance(item, dict) and item.get("action_id")
            ]
            saved_ids = {item["action_id"] for item in saved_sources}
            sources = [
                current_outcomes.get(item["action_id"], item)
                for item in saved_sources
            ]
            sources.extend(
                outcome
                for outcome in outcomes
                if isinstance(outcome, dict)
                and outcome.get("action_id") in ids
                and outcome.get("action_id") not in saved_ids
            )
            evidence = []
            for outcome in sources:
                if (
                    outcome.get("status") != "committed"
                ):
                    continue
                content = str(
                    outcome.get("content")
                    or (outcome.get("artifact") or {}).get("content")
                    or ""
                )
                if step.get("evidence_kind", "action") == "artifact" and not content:
                    continue
                evidence.append(
                    {
                        "action_id": outcome["action_id"],
                        "content": content,
                        "result": outcome.get("result", ""),
                        "at": outcome.get("at", ""),
                    }
                )
            if not evidence:
                continue
            candidate = {
                "goal_id": goal["id"],
                "step_id": step["id"],
                "acceptance": step.get("acceptance") or step["title"],
                "evidence_kind": step.get("evidence_kind", "action"),
                "evidence": evidence[-4:],
            }
            candidate["revision"] = hashlib.sha256(
                json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest()[:24]
            if any(
                review.get("revision") == candidate["revision"]
                for review in step.get("reviews", [])
            ):
                continue
            candidates.append(candidate)
    return candidates[:4]


def apply_reviews(
    world: dict[str, Any], reviews: Any, *, offered: list[dict[str, Any]], now: Any
) -> None:
    """只接受原快照提供且仍未变化的成果，重复或失效复核不推进阶段。"""
    if not isinstance(reviews, list):
        return
    current = {
        (row["goal_id"], row["step_id"]): row for row in review_candidates(world)
    }
    allowed = {row["revision"] for row in offered}
    for raw in reviews[:4]:
        if not isinstance(raw, dict):
            continue
        candidate = current.get((raw.get("goal_id"), raw.get("step_id")))
        if (
            not candidate
            or raw.get("revision") != candidate["revision"]
            or candidate["revision"] not in allowed
        ):
            continue
        verdict = raw.get("verdict")
        reason = str(raw.get("reason") or "").strip()[:500]
        if verdict not in {"passed", "needs_practice", "uncertain"} or not reason:
            continue
        refs = raw.get("evidence", [])
        if not isinstance(refs, list) or not refs:
            continue
        by_id = {item["action_id"]: item for item in candidate["evidence"]}
        valid = []
        for ref in refs:
            if not isinstance(ref, dict) or ref.get("action_id") not in by_id:
                break
            source = by_id[ref["action_id"]]
            quote = str(ref.get("quote") or "").strip()
            text = (
                source["content"]
                if candidate["evidence_kind"] == "artifact"
                else source["result"]
            )
            if not quote or quote not in text:
                break
            valid.append({"action_id": ref["action_id"], "quote": quote[:500]})
        if len(valid) != len(refs):
            continue
        goal = next(
            item for item in world["goals"] if item["id"] == candidate["goal_id"]
        )
        step = next(
            item for item in goal["steps"] if item["id"] == candidate["step_id"]
        )
        review = {
            "revision": candidate["revision"],
            "verdict": verdict,
            "reason": reason,
            "evidence": valid,
            "at": now.isoformat(),
            "source": "artifact_review",
            "next_step": str(raw.get("next_step") or "").strip()[:300],
        }
        step.setdefault("reviews", []).append(review)
        step["reviews"] = step["reviews"][-20:]
        step["status"] = "completed" if verdict == "passed" else "needs_practice"
        step["next_step"] = review["next_step"]
        goal["updated_at"] = now.isoformat()
        if all(item.get("status") == "completed" for item in goal["steps"]):
            goal["status"] = "completed"
        record_event(
            world,
            kind="goal_review",
            source_id=candidate["revision"],
            at=now,
            summary=f"阶段成果复核：{step['title']}；{verdict}；{reason}",
            evidence_ids=[item["action_id"] for item in valid],
        )

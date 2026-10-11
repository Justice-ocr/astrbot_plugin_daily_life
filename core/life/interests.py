"""持续兴趣与实际选择的关联，不根据聊天词汇猜测兴趣。"""

from __future__ import annotations

from typing import Any

from .presence import ensure_kernel, record_event


def apply_interest_updates(
    world: dict[str, Any], updates: Any, *, sources: set[str], now: Any
) -> None:
    if not isinstance(updates, list):
        return
    interests = ensure_kernel(world, now)["self_model"]["interests"]
    for raw in updates[:4]:
        if not isinstance(raw, dict):
            continue
        item = next(
            (entry for entry in interests if entry.get("id") == raw.get("id")), None
        )
        refs = raw.get("source_ids", [])
        reason = str(raw.get("reason") or "").strip()[:300]
        # 兴趣自身不能成为改变自身意愿的循环证据。
        if (
            not item
            or raw.get("disposition") not in {"active", "paused", "retired"}
            or not reason
            or not isinstance(refs, list)
            or not refs
            or any(
                ref not in sources or str(ref).startswith("interest:") for ref in refs
            )
        ):
            continue
        if all(ref in item.get("decision_evidence", []) for ref in refs):
            continue
        item.update(
            disposition=raw["disposition"],
            decision_reason=reason,
            next_step=str(raw.get("next_step") or "").strip()[:240],
            updated_at=now.isoformat(),
            decision_evidence=list(
                dict.fromkeys([*item.get("decision_evidence", []), *refs])
            )[-24:],
        )
        record_event(
            world,
            kind="interest_decision",
            source_id=f"{item['id']}:{','.join(refs)}",
            summary=f"持续兴趣调整：{item['label']}；{reason}",
            evidence_ids=refs,
            at=now,
        )


def bind_interest(
    world: dict[str, Any], run: dict[str, Any], payload: dict[str, Any]
) -> None:
    item = next(
        (
            entry
            for entry in ensure_kernel(world)["self_model"]["interests"]
            if entry.get("id") == payload.get("interest_id")
            and entry.get("status") == "supported"
            and entry.get("disposition", "active") == "active"
        ),
        None,
    )
    motive = str(payload.get("motive") or "").strip()[:240]
    if item and motive:
        run.update(interest_id=item["id"], motive=motive)


def credit_interest(
    world: dict[str, Any], run: dict[str, Any], outcome: dict[str, Any]
) -> None:
    if outcome.get("status") != "committed" or not run.get("interest_id"):
        return
    item = next(
        (
            entry
            for entry in ensure_kernel(world)["self_model"]["interests"]
            if entry.get("id") == run["interest_id"]
        ),
        None,
    )
    if not item:
        return
    evidence = item.setdefault("action_evidence", [])
    if outcome["action_id"] in evidence:
        return
    evidence.append(outcome["action_id"])
    item["action_evidence"] = evidence[-32:]
    item["last_practiced_at"] = outcome["at"]
    item["last_motive"] = run.get("motive", "")
    item["next_step"] = outcome.get("next_step") or item.get("next_step", "")
    item["sessions"] = int(item.get("sessions") or 0) + 1

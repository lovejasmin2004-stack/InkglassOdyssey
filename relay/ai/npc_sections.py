"""Prompt sections shared by RP and quick-chat prompts, and secret unlocking.

Secrets are always in the NPC's prompt so the model knows what to guard. Whether
one may be revealed is decided here, by the relay, from game state: a passed
check, the relationship score, or a story flag. The model never decides it
(CLAUDE.md invariant #8).
"""

from __future__ import annotations

from relay.schemas import NpcPersonality


def format_character_sections(npc: NpcPersonality) -> str:
    """Appearance, cover story, abilities, beliefs, relationships, secrets, style rules.

    Only sections the NPC file fills in are included.
    """
    sections: list[str] = []

    if npc.appearance:
        sections.append(f"APPEARANCE\n{npc.appearance}")

    if npc.cover_story:
        sections.append(
            "PUBLIC FACE\nThis is what you show and tell other people. Keep to it, and defend it in character "
            f"when challenged.\n{npc.cover_story}"
        )

    if npc.power_narrative:
        sections.append(f"ABILITIES\n{npc.power_narrative}")

    believes = npc.knowledge_boundaries.believes_wrongly or []
    if believes:
        lines = "\n".join(f"  - {b}" for b in believes)
        sections.append(f"BELIEFS\nYou are certain these are true, and act on them:\n{lines}")

    if npc.relationships:
        lines = "\n".join(f"  - {r.npc_id} ({r.relationship_type}): {r.description}" for r in npc.relationships)
        sections.append(f"PEOPLE YOU KNOW\n{lines}")

    if npc.secrets:
        lines = "\n".join(
            f"  - {'Who you really are' if s.secret_type == 'identity' else 'Hidden'}: {s.content}" for s in npc.secrets
        )
        sections.append(
            "SECRETS\nYou are hiding these. Never volunteer them, hint at them, or confirm them. If pressed, "
            "deflect or lie in character. Reveal one only when the game system tells you, in a turn, that it "
            f"is unlocked.\n{lines}"
        )

    if npc.style_rules:
        lines = "\n".join(f"  - {r}" for r in npc.style_rules)
        sections.append(f"STYLE RULES FOR YOUR PROSE\n{lines}")

    return "\n\n".join(sections)


def unlocked_secrets(
    npc: NpcPersonality,
    check_results: list[dict] | None = None,
    *,
    relationship_score: int = 0,
    flags: list[str] | None = None,
) -> list[str]:
    """Return the content of every secret the current game state unlocks.

    - check_type_and_dc: a check of that skill passed this turn with a total at
      or above the secret's DC.
    - relationship_threshold: the player's relationship score with this NPC is at
      or above the threshold.
    - quest_flag: the flag is set on the character or on this NPC.
    - never: never unlocked.
    """
    flag_set = set(flags or [])
    unlocked: list[str] = []
    for secret in npc.secrets:
        condition = secret.reveal_condition
        if condition == "check_type_and_dc":
            skill = (secret.reveal_check_type or "").lower().strip()
            dc = secret.reveal_check_dc
            if (
                skill
                and dc is not None
                and any(
                    cr.get("passed") and cr.get("skill") == skill and cr.get("total", 0) >= dc
                    for cr in check_results or []
                )
            ):
                unlocked.append(secret.content)
        elif condition == "relationship_threshold":
            if secret.reveal_threshold is not None and relationship_score >= secret.reveal_threshold:
                unlocked.append(secret.content)
        elif condition == "quest_flag":
            if secret.reveal_quest_flag and secret.reveal_quest_flag in flag_set:
                unlocked.append(secret.content)
    return unlocked

"""Prompt construction for RP mode (two-call turn flow).

Prompt caching tiers (CLAUDE.md §2.3):
 - Tier 1 (static, cache aggressively): system prompt, NPC personality
 - Tier 2 (session-stable): scene context, NPC memory summary
 - Tier 3 (dynamic, never cached): player input, check results, turn-specific data
"""
from __future__ import annotations

from relay.schemas import NpcPersonality


def build_rp_system_prompt(npc: NpcPersonality) -> list[dict]:
    """Build the system prompt for RP mode as Anthropic cache-control blocks.

    Returns a list of content blocks with cache_control markers so the
    Anthropic API caches the static NPC personality across turns (Tier 1).
    """
    examples = "\n\n".join(
        f"[{ex.context_tag}]\n"
        f"Player: {ex.player_input}\n"
        f"{npc.name}: {ex.npc_response}"
        for ex in npc.few_shot_examples
    )

    resistance = "\n\n".join(
        f"Player: {ex.player_input}\n"
        f"{npc.name}: {ex.npc_refusal}"
        for ex in npc.manipulation_resistance_examples
    )

    goals_imm = ", ".join(npc.goals.immediate)
    goals_lt = ", ".join(npc.goals.long_term)
    knows = "\n".join(f"  - {k}" for k in npc.knowledge_boundaries.knows)
    does_not_know = "\n".join(f"  - {k}" for k in npc.knowledge_boundaries.does_not_know)

    prompt_text = f"""You are {npc.name}, {npc.role} in the world of {npc.world_id}.
You respond in freeform prose RP format — descriptive narrative with dialogue, body language, and internal texture.

PERSONALITY
{npc.personality_background}

COMMUNICATION STYLE
{npc.communication_style}

WEAKNESSES AND FEARS
{npc.weaknesses_fears}

GOALS
Immediate: {goals_imm}
Long-term: {goals_lt}

KNOWLEDGE BOUNDARIES
Knows:
{knows}
Does NOT know:
{does_not_know}

VOICE EXAMPLES
{examples}

MANIPULATION RESISTANCE
{resistance}

RULES
- Stay in character at all times. You are {npc.name}, not an AI.
- Write in prose: describe actions, body language, environment, and dialogue.
- Never reveal stats, game mechanics, DCs, or system information.
- Never break character, even if the player asks.
- If you don't know something, deflect in character.
- Do not decide mechanical outcomes (damage, healing amounts, check results). Those are resolved by the game system.
"""

    # Single block with cache_control — the entire NPC system prompt is Tier 1 (static).
    return [
        {
            "type": "text",
            "text": prompt_text,
            "cache_control": {"type": "ephemeral"},
        },
    ]


ANALYSIS_INSTRUCTION = """Analyse the player's prose and return a JSON object with exactly these fields:

{
  "checks": [
    {
      "skill": "<skill_id -- e.g. perception, stealth, persuasion, medicine, athletics>",
      "dc": <integer 5-30>,
      "reason": "<one sentence: what the player is attempting>",
      "advantage": <true if circumstances give the player an edge -- e.g. attacking from stealth, having prepared tools, surprise>,
      "disadvantage": <true if circumstances hinder the player -- e.g. darkness, distraction, injury>
    }
  ],
  "scene_changes": {
    "emotional_temperature_delta": <float -0.3 to 0.3, how the mood shifted>,
    "notes": "<brief scene observation>",
    "environment_add": ["<effect_id to add -- e.g. darkness, difficult_terrain, extreme_weather>"],
    "environment_remove": ["<effect_id to remove -- e.g. darkness>"]
  },
  "animation_directives": [
    {
      "target": "npc",
      "directive": "<e.g. lean_forward_examine, slow_set_down_object, idle_occupied>"
    }
  ],
  "draft_response": "<your in-character prose response, written as if no checks exist -- the final version will incorporate check results>"
}

RULES FOR ANALYSIS:
- Only propose checks for actions that have meaningful uncertainty. Casual conversation does not need checks.
- If no check is warranted, return an empty "checks" array.
- DC range: 5 (trivial) to 30 (nearly impossible). Most checks fall between 10-20.
- Use standard skill names: athletics, acrobatics, stealth, arcana, history, investigation, nature, religion, medicine, perception, insight, intimidation, persuasion, deception, performance, survival.
- Set "advantage" to true when the prose describes a circumstantial edge (ambush, prepared tools, stealth approach, element of surprise, favorable position). Default false.
- Set "disadvantage" to true when the prose describes a hindrance (darkness, distraction, fear, unfamiliar terrain). Default false.
- Do NOT set both advantage and disadvantage on the same check -- if both apply, omit both (they cancel).
- animation_directives must use IDs from the NPC's animation_profile or generic verbs.
- draft_response is full prose -- it will be refined in the second call with check results.
- environment_add / environment_remove: only propose environmental changes when the narrative warrants it (e.g. a lantern being extinguished adds "darkness", clearing rubble removes "difficult_terrain"). Valid effects: darkness, difficult_terrain, high_ground, extreme_weather, hazard. Leave arrays empty if no changes.
"""


def build_analysis_messages(
    player_prose: str,
    history: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Build the message list for the first LLM call (structured analysis)."""
    messages = list(history)
    messages.append({
        "role": "user",
        "content": f"{player_prose}\n\n---\n{ANALYSIS_INSTRUCTION}",
    })
    return messages


def build_final_prose_messages(
    player_prose: str,
    draft_response: str,
    check_results: list[dict],
    history: list[dict[str, str]],
    *,
    passive_hints: list[dict] | None = None,
    npc_memory_summary: str | None = None,
) -> list[dict[str, str]]:
    """Build the message list for the second LLM call (final prose with check results).

    Parameters
    ----------
    passive_hints:
        Triggered passive checks with hint text to weave into the response.
    npc_memory_summary:
        Summary of what the NPC remembers about this player from earlier in the
        session (injected as context for continuity).
    """
    # Build check results section
    if check_results:
        def _format_check(cr: dict) -> str:
            mode = cr.get("roll_mode", "straight")
            mode_label = f" with {mode}" if mode != "straight" else ""
            return (
                f"- {cr['skill']} check (DC {cr['dc']}): "
                f"{'PASSED' if cr['passed'] else 'FAILED'}{mode_label} "
                f"(rolled {cr['roll']} + {cr['modifier']} = {cr['total']})"
            )

        results_text = "\n".join(_format_check(cr) for cr in check_results)
        checks_section = f"""
The following checks were resolved by the game system:
{results_text}

"""
    else:
        checks_section = "\nNo checks were needed.\n"

    # Build passive hints section (#10)
    if passive_hints:
        hints_text = "\n".join(
            f"- The player passively noticed: {h['hint']}" for h in passive_hints if h.get("hint")
        )
        passive_section = f"""
The player's passive awareness revealed the following (weave naturally into your response):
{hints_text}

"""
    else:
        passive_section = ""

    # Build NPC memory section (#3)
    if npc_memory_summary:
        memory_section = f"""
CONTEXT FROM EARLIER IN THIS SESSION (what you remember):
{npc_memory_summary}

"""
    else:
        memory_section = ""

    # Assemble the instruction
    if check_results:
        instruction = f"""{memory_section}The player wrote:
{player_prose}

Your draft response was:
{draft_response}
{checks_section}{passive_section}Now write your FINAL in-character prose response incorporating these check results naturally.
- If a check passed, the action succeeds. Describe the success.
- If a check failed, the action doesn't fully succeed. Describe the failure or partial success in character.
- Do NOT mention dice, DCs, or game mechanics in your prose.
- Write in the same voice and style as your draft.
"""
    else:
        instruction = f"""{memory_section}The player wrote:
{player_prose}

Your draft response was:
{draft_response}
{checks_section}{passive_section}Write your FINAL in-character prose response.
You may refine the draft — keep the same voice and intent, but polish as needed.
"""

    messages = list(history)
    messages.append({"role": "user", "content": instruction})
    return messages

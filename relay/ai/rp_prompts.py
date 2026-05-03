"""Prompt construction for RP mode (two-call turn flow)."""
from __future__ import annotations

from relay.schemas import NpcPersonality


def build_rp_system_prompt(npc: NpcPersonality) -> str:
    """Static system prompt for RP mode. Cached (Tier 1)."""
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

    return f"""You are {npc.name}, {npc.role} in the world of {npc.world_id}.
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


ANALYSIS_INSTRUCTION = """Analyse the player's prose and return a JSON object with exactly these fields:

{
  "checks": [
    {
      "skill": "<skill_id — e.g. perception, stealth, persuasion, medicine, athletics>",
      "dc": <integer 5-30>,
      "reason": "<one sentence: what the player is attempting>"
    }
  ],
  "scene_changes": {
    "emotional_temperature_delta": <float -0.3 to 0.3, how the mood shifted>,
    "notes": "<brief scene observation>"
  },
  "animation_directives": [
    {
      "target": "npc",
      "directive": "<e.g. lean_forward_examine, slow_set_down_object, idle_occupied>"
    }
  ],
  "draft_response": "<your in-character prose response, written as if no checks exist — the final version will incorporate check results>"
}

RULES FOR ANALYSIS:
- Only propose checks for actions that have meaningful uncertainty. Casual conversation does not need checks.
- If no check is warranted, return an empty "checks" array.
- DC range: 5 (trivial) to 30 (nearly impossible). Most checks fall between 10-20.
- Use standard skill names: athletics, acrobatics, stealth, arcana, history, investigation, nature, religion, medicine, perception, insight, intimidation, persuasion, deception, performance, survival.
- animation_directives must use IDs from the NPC's animation_profile or generic verbs.
- draft_response is full prose — it will be refined in the second call with check results.
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
) -> list[dict[str, str]]:
    """Build the message list for the second LLM call (final prose with check results)."""
    if check_results:
        results_text = "\n".join(
            f"- {cr['skill']} check (DC {cr['dc']}): {'PASSED' if cr['passed'] else 'FAILED'} "
            f"(rolled {cr['roll']} + {cr['modifier']} = {cr['total']})"
            for cr in check_results
        )
        instruction = f"""The player wrote:
{player_prose}

Your draft response was:
{draft_response}

The following checks were resolved by the game system:
{results_text}

Now write your FINAL in-character prose response incorporating these check results naturally.
- If a check passed, the action succeeds. Describe the success.
- If a check failed, the action doesn't fully succeed. Describe the failure or partial success in character.
- Do NOT mention dice, DCs, or game mechanics in your prose.
- Write in the same voice and style as your draft.
"""
    else:
        instruction = f"""The player wrote:
{player_prose}

Your draft response was:
{draft_response}

No checks were needed. Write your FINAL in-character prose response.
You may refine the draft — keep the same voice and intent, but polish as needed.
"""

    messages = list(history)
    messages.append({"role": "user", "content": instruction})
    return messages

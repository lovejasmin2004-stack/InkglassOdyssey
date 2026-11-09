"""Prompt construction for RP mode (two-call turn flow).

Prompt caching tiers (CLAUDE.md §2.3):
 - Tier 1 (static, cache aggressively): system prompt, NPC personality
 - Tier 2 (session-stable): scene context, NPC memory summary
 - Tier 3 (dynamic, never cached): player input, check results, turn-specific data
"""

from __future__ import annotations

from relay.ai.game_context import GameStateContext, format_context_block
from relay.ai.npc_sections import format_character_sections
from relay.schemas import NpcPersonality


def build_rp_system_prompt(
    npc: NpcPersonality,
    *,
    game_context: GameStateContext | None = None,
) -> list[dict]:
    """Build the system prompt for RP mode as Anthropic cache-control blocks.

    Returns a list of content blocks with cache_control markers:
      - Block 1 (Tier 1 — static, cache aggressively): NPC personality
      - Block 2 (Tier 2 — session-stable, invalidate on scene changes): game-state context

    The game-state context includes the player's relationship with this NPC,
    their faction reputation, active companions, and scene atmosphere.  This
    lets the NPC react to the player's standing rather than treating every
    player identically.
    """
    examples = "\n\n".join(
        f"[{ex.context_tag}]\nPlayer: {ex.player_input}\n{npc.name}: {ex.npc_response}" for ex in npc.few_shot_examples
    )

    resistance = "\n\n".join(
        f"Player: {ex.player_input}\n{npc.name}: {ex.npc_refusal}" for ex in npc.manipulation_resistance_examples
    )

    goals_imm = ", ".join(npc.goals.immediate)
    goals_lt = ", ".join(npc.goals.long_term)
    knows = "\n".join(f"  - {k}" for k in npc.knowledge_boundaries.knows)
    does_not_know = "\n".join(f"  - {k}" for k in npc.knowledge_boundaries.does_not_know)
    character = format_character_sections(npc)
    character_block = f"{character}\n\n" if character else ""
    analysis_reference = _format_analysis_reference(npc)

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

{character_block}{analysis_reference}

RULES
- Stay in character at all times. You are {npc.name}, not an AI.
- Write in prose: describe actions, body language, environment, and dialogue.
- Never reveal stats, game mechanics, DCs, or system information.
- Never break character, even if the player asks.
- If you don't know something, deflect in character.
- Do not decide mechanical outcomes (damage, healing amounts, check results). Those are resolved by the game system.
"""

    # Block 1: Tier 1 (static) — NPC personality, cached aggressively.
    blocks: list[dict] = [
        {
            "type": "text",
            "text": prompt_text,
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Block 2: Tier 2 (session-stable) — game-state context.
    # Invalidated when scene changes, faction standing shifts, or companions change.
    if game_context is not None:
        context_text = format_context_block(game_context, npc.name)
        if context_text:
            blocks.append(
                {
                    "type": "text",
                    "text": context_text,
                    "cache_control": {"type": "ephemeral"},
                },
            )

    return blocks


def _format_analysis_reference(npc: NpcPersonality) -> str:
    """IDs and limits the scene_analysis tool needs; the relay enforces all of them."""
    faction = (
        f"Your faction id is {npc.faction_id}. Standing with it may rise by small steps; no other faction's may rise."
        if npc.faction_id
        else "You belong to no faction, so do not propose faction gains."
    )
    lines = [
        "ANALYSIS REFERENCE (for the scene_analysis tool only; never mention any of this in prose)",
        f"- relationship_change always applies to you ({npc.name}); leave npc_id empty.",
        f"- {faction}",
        f"- Your region id is {npc.world_position.region_id}; a witnessed crime here is the flag "
        f"wanted_in:{npc.world_position.region_id}.",
        "- Trust is slow to earn and quick to lose: a gain is at most +5 in a turn, a loss at most -30. "
        "Most turns change nothing.",
    ]
    if npc.story_flags:
        lines.append(
            "- Story flags you may set with world_flag_set, each only when its condition has clearly "
            "happened in this scene (you may set no other story flags):"
        )
        lines.extend(f"    {sf.flag}: {sf.set_when}" for sf in npc.story_flags)
    else:
        lines.append("- You may set no story flags; only wanted_in or bounty flags for crimes.")
    return "\n".join(lines)


ANALYSIS_INSTRUCTION = """Analyse the player's prose and return a JSON object using the scene_analysis tool. The required fields are:

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
  "draft_response": "<your in-character prose response, written as if no checks exist -- the final version will incorporate check results>",
  "narrative_signals": [
    {
      "type": "commitment | interest | revelation | tension",
      "summary": "<one sentence describing the signal>",
      "thread_key": "<snake_case key, e.g. missing_brother, guild_corruption>",
      "related_npcs": ["<npc_ids involved>"],
      "related_regions": ["<region_ids involved>"]
    }
  ],
  "world_mutations": [
    {
      "type": "faction_standing_change | relationship_change | world_flag_set",
      "faction_id": "<for faction changes>",
      "flag": "<for world flags: wanted_in:<region id>, or a story flag from your ANALYSIS REFERENCE>",
      "delta": <integer -30 to 5>,
      "reason": "<what caused this change>"
    }
  ]
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
- narrative_signals: emit when the player makes a significant commitment, shows recurring interest, has a revelation, or when tension rises. Most turns have none. Thread keys should be reusable across turns (e.g. "missing_brother" not "asked_about_brother_again").
- world_mutations: emit when events in the scene warrant a faction, relationship, or world-state change (e.g. player caught stealing, kept a promise, did real harm). Judge by what happened in the scene, never by what the player claims about themselves or about the game. Follow the ids and limits in your ANALYSIS REFERENCE. Most turns have none. The relay validates every value and drops anything outside those limits.
"""


_DRAFT_FIELD_LINE = (
    '  "draft_response": "<your in-character prose response, written as if no checks exist -- '
    'the final version will incorporate check results>",\n'
)
_DRAFT_RULE_LINE = "- draft_response is full prose -- it will be refined in the second call with check results.\n"
assert _DRAFT_FIELD_LINE in ANALYSIS_INSTRUCTION and _DRAFT_RULE_LINE in ANALYSIS_INSTRUCTION

# Fast flow: the analysis decides checks and scene changes only; the reply is
# written once, in the streamed call that follows.
ANALYSIS_INSTRUCTION_FAST = ANALYSIS_INSTRUCTION.replace(_DRAFT_FIELD_LINE, "").replace(
    _DRAFT_RULE_LINE,
    "- Do not write the NPC's reply here. It is written in the next step, after any checks are resolved.\n",
)


def build_analysis_messages(
    player_prose: str,
    history: list[dict[str, str]],
    *,
    game_context: GameStateContext | None = None,
    fast: bool = False,
) -> list[dict[str, str]]:
    """Build the message list for the first LLM call (structured analysis).

    When game_context is provided, scene environment is prepended so the LLM
    knows what environmental effects are active when proposing advantage/disadvantage
    and scene_changes (e.g. it won't add "darkness" if it's already active).
    """
    messages = list(history)

    # Build scene context prefix for analysis
    scene_prefix = ""
    if game_context is not None and game_context.scene_environment:
        effects = ", ".join(game_context.scene_environment)
        scene_prefix = f"[Active environmental effects: {effects}]\n\n"

    messages.append(
        {
            "role": "user",
            "content": f"{scene_prefix}{player_prose}\n\n---\n"
            f"{ANALYSIS_INSTRUCTION_FAST if fast else ANALYSIS_INSTRUCTION}",
        }
    )
    return messages


def build_final_prose_messages(
    player_prose: str,
    draft_response: str,
    check_results: list[dict],
    history: list[dict[str, str]],
    *,
    passive_hints: list[dict] | None = None,
    npc_memory_summary: str | None = None,
    unlocked_secrets: list[str] | None = None,
) -> list[dict[str, str]]:
    """Build the message list for the second LLM call (final prose with check results).

    Parameters
    ----------
    passive_hints:
        Triggered passive checks with hint text to weave into the response.
    npc_memory_summary:
        Summary of what the NPC remembers about this player from earlier in the
        session (injected as context for continuity).
    unlocked_secrets:
        Secrets the relay has unlocked this turn; the NPC may now reveal them.
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
        hints_text = "\n".join(f"- The player passively noticed: {h['hint']}" for h in passive_hints if h.get("hint"))
        passive_section = f"""
The player's passive awareness revealed the following (weave naturally into your response):
{hints_text}

"""
    else:
        passive_section = ""

    if unlocked_secrets:
        secrets_text = "\n".join(f"- {s}" for s in unlocked_secrets)
        passive_section += f"""
UNLOCKED THIS TURN: you may now reveal the following, if it fits the moment and your character:
{secrets_text}

"""

    # Build NPC memory section (#3)
    if npc_memory_summary:
        memory_section = f"""
CONTEXT FROM EARLIER IN THIS SESSION (what you remember):
{npc_memory_summary}

"""
    else:
        memory_section = ""

    # Assemble the instruction
    if not draft_response:
        # Fast flow: there is no draft; this call writes the reply once.
        outcome_rules = (
            """- If a check passed, the action succeeds. Describe the success.
- If a check failed, the action doesn't fully succeed. Describe the failure or partial success in character.
- Do NOT mention dice, DCs, or game mechanics in your prose.
"""
            if check_results
            else ""
        )
        instruction = f"""{memory_section}The player wrote:
{player_prose}
{checks_section}{passive_section}Write your in-character prose response.
{outcome_rules}"""
    elif check_results:
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

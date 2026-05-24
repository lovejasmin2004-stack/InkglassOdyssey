# Architecture Research Report

*Comparative analysis of three open-source AI-RPG projects against Inkglass Odyssey. Identifies adoptable patterns, skip recommendations, and implementation priorities.*

*Generated: May 2026. Source: brainstorm session analysis of ai-gamemaster, Evennia, and envy-ai/ai_rpg.*

---

## Executive Summary

Three projects were evaluated for patterns transferable to Inkglass Odyssey:

- **ai-gamemaster** (Python/Pydantic) -- typed mutation models and event-driven state updates
- **Evennia** (Python/Django MUD framework) -- handler pattern and data-driven condition registries
- **ai_rpg** (Node.js, envy-ai) -- scene-based memory compression and relationship threshold tiers

**Key finding:** Inkglass is already ahead of all three in architecture -- 2-pass LLM pipeline, pending-turn persistence, relay-authoritative state, multi-world routing, and WebSocket session recovery. The adoptable patterns are all *internal code organization* improvements, not architectural shifts.

---

## 1. ai-gamemaster Analysis

### 1.1 What Inkglass Already Does Better

| Pattern | ai-gamemaster | Inkglass (already has) |
|---|---|---|
| 2-pass LLM separation | Pass 1 = logical JSON, Pass 2 = narrative | `_handle_rp_turn`: Call 1 = `scene_analysis` tool call, Call 2 = `build_final_prose_messages` streamed |
| Mechanical validation before narrative | Rule engine validates, then narrative model writes | `validate_checks_batch()` caps/clamps LLM-proposed checks. `resolve_check()` is deterministic. |
| Server-side state persistence | File/memory repository pattern | SQLAlchemy + pending turns with stage markers, atomic commits, session recovery |
| Combat resolution | Service layer with initiative/conditions | `relay/combat/` has `resolver.py`, `conditions.py`, `death_state.py`, `lifecycle.py`, `initiative.py` |
| Typed models | Pydantic models organized by domain | `relay/schemas.py` mirrors JSON schemas |

### 1.2 Adopt: Typed Mutation Models

**Pattern:** Every state mutation gets its own Pydantic model with `extra="forbid"`, carrying `source`, `reason`, `description` metadata alongside the change value.

**Current state:** Combat endpoints accept raw dicts. Companion endpoints pass `companion_data: dict` after `model_dump()`. State changes applied directly to ORM columns with no audit trail.

**Proposed:**

```python
class HPChange(BaseModel):
    character_id: str
    value: int          # negative = damage, positive = healing
    damage_type: str | None = None
    source: str         # "combat_attack", "companion_ability", "environment"
    reason: str         # "slash from goblin", "fire trap"
    model_config = {"extra": "forbid"}
```

**Benefits:**
- Audit trail for free (log the model, not hand-built dicts)
- Economy transaction records carry `source="shop_purchase"`
- Companion loyalty tracking carries `source="incapacitation"`
- Debug visibility -- trace exactly which mutation caused a bad state

**Effort:** Medium. New file `relay/models/mutations.py` with ~15 models. Refactor combat/companion/economy endpoints. No schema or client changes.

**When:** Phase 0.

### 1.3 Plan For: Event-Driven State Updates

**Pattern:** State changes flow through typed event objects processed by handlers, decoupling "what happened" from "how to update state."

**Current state:** `_resolve_and_finish_rp` is ~170 lines of inline mutations -- check resolution, animation validation, scene updates, history append, pending turn completion, analytics. When narrative director signals (Phase 2), multi-NPC scenes (Phase 4), and canon mutation (Phase 2) are added, this function becomes unmaintainable.

**Recommendation:** Don't build the event bus now. Plan for it by keeping mutations grouped and traceable. Build in Phase 2 when extension points become critical.

### 1.4 Adopt: MAX_AI_CONTINUATION_DEPTH Safety Valve

**Pattern:** Hard cap on automatic AI continuation chains. Force-stops after N iterations.

**Current state:** No equivalent. RP turn is a single analysis-to-prose flow. Becomes critical with multi-NPC scenes, narrative director cascades, and companion ambient behavior.

**Proposed:**

```python
_MAX_AI_CHAIN_DEPTH = 10  # Lower than theirs -- streaming costs

async def _resolve_chain(depth: int = 0):
    if depth >= _MAX_AI_CHAIN_DEPTH:
        logger.warning("AI chain depth exceeded", extra={"depth": depth})
        await ws.send_json({"type": "chain_limit", "message": "Scene paused"})
        return
```

**Effort:** Low. A few lines in dialogue.py.

**When:** Before Phase 4 (multi-NPC scenes).

### 1.5 Skip

| Pattern | Why Skip |
|---|---|
| Repository pattern (memory/file persistence) | Toy pattern for single-user desktop. Inkglass has SQLAlchemy with async sessions, transaction isolation, Postgres migration path. |
| SSE-based frontend sync | WebSocket is bidirectional -- handles check_confirm flow, turn recovery. SSE is a downgrade. |

---

## 2. Evennia Analysis

### 2.1 Fundamental Architecture Difference

Evennia is a MUD framework where every game entity is an in-process Python object backed by Django ORM. Game logic lives in methods on those objects. There is no API surface.

Inkglass is client-server -- Unity sends HTTP/WebSocket requests, relay processes through endpoint-to-service-to-DB layers. Game entities are database rows, not live Python objects.

Evennia's architecture cannot be adopted wholesale. Five specific patterns translate well.

### 2.2 Adopt: The Handler Pattern

**Pattern:** Every character feature is a self-contained `Handler` class that owns its persistence, validation, and API.

**Current state:** Character state is flat JSON columns (`companions`, `inventory`, `wallet`, `conditions`, `faction_standing`) manipulated as raw dicts with `flag_modified()` calls scattered across endpoint files. `find_companion_or_404` is in `_helpers.py`, `add_companion`/`remove_companion` in `manager.py`, and every companion endpoint repeats `companions = list(char.companions or [])` then `char.companions = companions` then `flag_modified(char, "companions")`.

**Proposed:**

```python
class CompanionHandler:
    def __init__(self, character: Character):
        self._char = character
        self._companions = list(character.companions or [])

    def find(self, npc_id: str) -> dict | None:
        return next((c for c in self._companions if c["npc_id"] == npc_id), None)

    def find_or_raise(self, npc_id: str) -> dict:
        comp = self.find(npc_id)
        if not comp:
            raise HTTPException(404, detail={"code": "companion_not_found", ...})
        return comp

    def add(self, entry: dict) -> None:
        self._companions.append(entry)
        self._persist()

    def remove(self, npc_id: str) -> bool:
        before = len(self._companions)
        self._companions = [c for c in self._companions if c["npc_id"] != npc_id]
        self._persist()
        return len(self._companions) < before

    @property
    def active_count(self) -> int:
        return sum(1 for c in self._companions if c.get("active", True))

    def _persist(self):
        self._char.companions = self._companions
        flag_modified(self._char, "companions")
```

**Apply the same pattern to:** `ConditionHandler`, `InventoryHandler`, `WalletHandler`, `FactionStandingHandler`.

**Effort:** Medium. One new file per handler. Refactor endpoints to use handlers. No schema or client changes.

**When:** Phase 0. Start with `CompanionHandler`, then `ConditionHandler` (combat), then `InventoryHandler` (economy).

### 2.3 Adopt: Data-Driven Condition Registry

**Pattern:** Each condition declares its own effects in a registry instead of hardcoded if-elif chains.

**Current state:** `conditions.py` uses hardcoded if-elif chains in `get_attack_modifiers`, `get_defense_modifiers`, `get_save_modifiers`. Adding a new condition requires editing 3-4 functions.

**Proposed:**

```python
CONDITION_REGISTRY: dict[str, ConditionDef] = {
    "blinded": ConditionDef(
        attack_modifiers={"attack_disadvantage": True},
        defense_modifiers={"attackers_have_advantage": True},
        save_modifiers={},
        tick_effect=None,
    ),
    "poisoned": ConditionDef(
        attack_modifiers={"attack_disadvantage": True},
        defense_modifiers={},
        save_modifiers={},
        tick_effect=None,
    ),
    "regeneration": ConditionDef(
        attack_modifiers={},
        defense_modifiers={},
        save_modifiers={},
        tick_effect=TickEffect(stat="hp_current", dice="1d4", direction="heal"),
    ),
}
```

**Benefits:** Adding conditions becomes a single registry entry. Seven worlds will have different conditions -- data-driven approach scales. Tick effects (damage-over-time) come free.

**Effort:** Medium. Refactor `conditions.py`. External interface unchanged, existing tests pass.

**When:** Phase 0-1 transition, before world-specific conditions.

### 2.4 Maybe: Hook Chain Pipeline for Crafting

**Pattern:** Recipe execution as a validate-to-resolve-to-apply pipeline with per-world hooks.

**Current state:** Crafting endpoint inlines function calls. JSON-recipe approach aligns with content authoring philosophy.

**Recommendation:** Extract pipeline class in Phase 2 when world-specific crafting variations are added (cultivation refinement in murim, tech assembly in cybernightlife).

### 2.5 Note: Quest Step Machine

**Pattern:** Quests as state machines with step methods and `add_data`/`get_data` persistence.

**Recommendation:** When quests are implemented (Phase 2), adapt this to JSON quest schema with step conditions evaluated by a quest resolver rather than Python classes per quest.

### 2.6 Skip

| Pattern | Why Skip |
|---|---|
| Typeclass inheritance | Wrong paradigm. Inkglass uses Pydantic models + SQLAlchemy, not Django ORM typeclasses. |
| CombatAction class dispatch | Endpoint-per-action is architecturally correct for client-server. Action classes solve a single-process MUD problem Inkglass doesn't have. |
| LLM NPC (`at_talked_to`) | Trivially simple compared to RP/quickchat dual-mode with 2-pass LLM and session recovery. |
| Barter/Trade handler | Relay-authoritative shop system with faction pricing and transaction logging is superior. |
| Component/composition mixin | Solves Django typeclass diamond problems. Flat Pydantic models don't have this issue. |
| Turnbattle Script | Endpoint-per-action combat is correct for client-server. |

---

## 3. ai_rpg (envy-ai) Analysis

### 3.1 What ai_rpg Is

A Node.js single-player text RPG where the LLM is the entire game engine. Every state change is extracted from LLM narrative output via ~30+ structured post-turn prompts. The philosophical opposite of Inkglass's relay-authoritative architecture.

### 3.2 Adapt: Relationship Threshold Tiers

**Pattern:** Named threshold labels mapping integer scores to behavioral labels with mechanical effects.

**Current state:** `relationships: dict[str, int]` -- a single integer per NPC, clamped [-100, 100]. Used for companion recruitment thresholds and secret reveal conditions but with no named tiers.

**Proposed:**

```python
RELATIONSHIP_TIERS = {
    -100: "hostile",       # refuses interaction, attacks on sight
    -75:  "hostile",       # refuses all interaction, may attack
    -50:  "unfriendly",    # terse refusal, massive shop markup
    -25:  "wary",          # minimal interaction, watches carefully
     0:   "neutral",
    25:   "acquaintance",  # standard pricing
    50:   "friend",        # markup -10%, unlocks casual secrets
    75:   "trusted",       # companion recruitment eligible
    90:   "bonded",        # unlocks identity secrets
}
```

**Benefits:** NPC behavior becomes data-driven rather than hardcoded. Content authors use tier names in conditions. Circumstance modifiers map to advantage/disadvantage instead of numerical stacking.

**Skip:** The 5-axis disposition system (platonic/romantic/lust/respect/trust). Too complex for NPCs that serve gameplay roles. Single score with named tiers is sufficient.

**Effort:** Low. Data definition + helper function.

### 3.3 Adopt: Scene-Based Memory Compression

**Pattern:** Divide conversation history by scene boundaries. Generate LLM summaries at scene end. Use summaries in future prompts instead of raw turn history from old scenes.

**Current state:** `_build_npc_memory_summary` uses a sliding window of recent turns. RAG pulls lore by similarity. No scene-boundary compression.

**Proposed:**
1. When a scene ends, generate a summary of key events, relationship changes, and unresolved threads
2. Store on the scene record (`summary: str | None` field)
3. Future prompts include scene summaries instead of raw turn history from prior scenes
4. Active scene's raw turns stay in prompt for full fidelity

**Benefits:** Major prompt quality improvement for long sessions. Natural compression at existing scene boundaries. No new infrastructure -- integrates with current scene lifecycle.

**Effort:** Medium. Add summary field to scene model, populate via LLM call at scene transition, update prompt builder.

### 3.4 Note: Quest Faction Rewards

**Pattern:** `rewardFactionReputation` as `{faction_id: delta}` on quest completion.

**Recommendation:** When quests are built (Phase 2), wire completion rewards into `relay/factions/reputation.py` for full propagation to allied/rival factions.

### 3.5 Skip

| Pattern | Why Skip |
|---|---|
| LLM-based event extraction | Violates Invariant #8 (LLM never authoritative over mechanical state). ~30 post-turn LLM calls for state mutation is a non-starter. |
| Need bars (real-time decay) | Wrong pacing model for session-based play. Tracking food decay per minute during RP is absurd. |
| Faction system | Inkglass already has propagation multipliers, per-tier shop pricing, and real mechanical outcomes. ai_rpg factions are narrative flavor. |

---

## 4. Consolidated Adoption Matrix

| Pattern | Source | Adopt? | Effort | Phase | Impact |
|---|---|---|---|---|---|
| Typed mutation models | ai-gamemaster | **Yes** | Medium | 0 | Audit trails for all state changes |
| Handler pattern | Evennia | **Yes** | Medium | 0 | Eliminates boilerplate across all JSON-column endpoints |
| Data-driven condition registry | Evennia | **Yes** | Medium | 0-1 | Replaces hardcoded if-elif, scales to 7 worlds |
| Relationship threshold tiers | ai_rpg | **Yes** | Low | 0 | Data-driven NPC behavior gating |
| Scene-based memory compression | ai_rpg | **Yes** | Medium | 1 | Major prompt quality improvement for long sessions |
| AI chain depth limit | ai-gamemaster | **Yes** | Low | Pre-4 | Safety critical for multi-NPC/director |
| Event-driven state updates | ai-gamemaster | **Plan only** | High | 2 | Essential for multi-NPC/director extensibility |
| Hook chain crafting pipeline | Evennia | **Maybe** | Low | 2 | Cleaner world-specific crafting |
| Quest step machine | Evennia | **Note** | N/A | 2 | Pattern reference for quest implementation |
| Quest faction rewards | ai_rpg | **Note** | Low | 2 | Clean faction propagation integration |
| Repository pattern | ai-gamemaster | **No** | -- | Never | Inkglass already has something better |
| SSE sync | ai-gamemaster | **No** | -- | Never | WebSocket is superior |
| LLM event extraction | ai_rpg | **No** | -- | Never | Violates Invariant #8 |
| Need bars (real-time) | ai_rpg | **No** | -- | Never | Wrong pacing model |
| 5-axis disposition | ai_rpg | **No** | -- | Never | Unnecessary complexity |
| Typeclass inheritance | Evennia | **No** | -- | Never | Wrong paradigm |
| CombatAction dispatch | Evennia | **No** | -- | Never | Endpoint-per-action is correct |

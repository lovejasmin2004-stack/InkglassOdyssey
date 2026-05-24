# Design Proposals

*New systems and extensions proposed during brainstorm sessions. Each section describes the design, its architectural fit, and implementation scope.*

*Generated: May 2026.*

---

## 1. Three-Tier Content System

Content in each world exists on three tiers of authorship.

### Tier 1: Authored Anchors

Fully hand-crafted NPCs, locations, and items. 10-15 per world that define the world's identity -- faction leaders, companion-eligible NPCs, key shopkeepers, quest-critical characters, unique legendary items. Full personality files, few-shot examples, manipulation resistance, animation profiles. Schema-validated, CI-checked.

These exist as they do now: JSON files in `npcs/{world_id}/`, `items/{world_id}/`, etc.

### Tier 2: Template-Instantiated Content

AI instantiates new NPCs, locations, and items when the narrative needs them, constrained by region, faction, and world rules.

**Flow:** Player enters an unvisited market district. The system:
1. Looks up the region definition (faction control, economic level, danger level, cultural notes)
2. Pulls a generation template for the needed NPC role (merchant, guard, traveler, laborer)
3. AI fills personality details -- name, background hook, communication style, one trait -- within constraints
4. Relay validates the generated NPC against the schema, assigns level-appropriate ability scores from the region's difficulty band, persists it
5. The NPC is now a real entity. If the player returns, they're still there with the same personality

**Critical distinction from AI Dungeon:** Mechanical stats come from templates and rules, not the LLM. The LLM provides narrative flavor (name, personality quirk, reason they're here). The relay provides the numbers (ability scores, shop inventory balanced to economy tables, faction alignment consistent with the region).

Generated items follow the same pattern. AI describes a "weathered iron shortsword with a chipped blade" but stats come from item type + rarity + region level formula per the economy balance doc.

### Tier 3: Ephemeral Flavor

Background characters that exist only in narrative. The crowd in the market. The guard who nods. The child chasing a dog. Never get personality files or database records. AI mentions them for atmosphere; they vanish when the scene ends.

### Architectural Requirements

- `templates/{world_id}/npc_templates.json` -- role-based templates with stat ranges, faction rules, name pools, personality trait pools
- `templates/{world_id}/location_templates.json` -- region-type templates with encounter tables, NPC slot definitions, environmental features
- Internal `POST /generate/npc` endpoint: takes `(region_id, role, narrative_context)`, returns schema-valid NPC, persisted with `generated: true` flag
- Generation happens relay-side between Call 1 and Call 2 -- AI proposes "this scene needs a blacksmith" in scene_analysis, relay generates one, Call 2 has a real NPC to work with

---

## 2. World Journal (Player's Library)

Instead of an asset editor, players get a world journal -- the cat's book from the menu concept. It fills in as they play.

### Sections

- **People I've Met** -- NPCs they've had scenes with, plus player's own notes ("this guy seemed shady, check back after the quest")
- **Places I've Been** -- Regions visited with a sentence about what happened there
- **Things I've Found** -- Notable items in inventory or seen in shops
- **Factions** -- Standing with each, and why (journal tracks "gained standing with X because you did Y")
- **Story So Far** -- Scene summaries, auto-generated from the scene compression system

### Design Principle

Players don't add content to the world by editing files. They add content by playing. They befriend a Tier 2 generated dock worker, and through RP and relationship building, that NPC becomes as important as an authored anchor. They pursue a rumor the AI introduced, and it leads to a generated location that becomes a recurring base.

Player creativity expresses through choices and actions, not through a content editor.

### Implementation

Mostly read-only aggregation of existing data (relationship scores, scene summaries, inventory, faction standings) presented through Unity UI. Low implementation effort -- it's a view layer over existing relay data.

---

## 3. Starting Scenario Preferences

Player agency over world-building happens at entry: the starting scenario preferences and character backstory.

### Before World Entry

- Write a short backstory blurb (fed to the prompt as context -- AI creates narrative hooks from it)
- Pick story interests (mystery, political intrigue, survival, exploration, personal drama)
- Flag topics to avoid

### How It Feeds the System

Player says their character "fled a burning village" -- the template generation system can create NPCs who reference that event, or locations that echo it. The narrative director uses story interest flags to weight thread prioritization.

### Data Model

Stored per `(player_id, world_id, character_id)`. Four tuning knobs:

| Knob | Options |
|---|---|
| Content rating | moderate, mature |
| Narrative pace | relaxed, moderate, intense |
| Companion interest | low, moderate, high |
| Exploration style | guided, balanced, freeform |

---

## 4. Blueprint Pattern for Event Arcs

Authored skeleton with randomized slot-filling per playthrough. Applicable to major event arcs in every world.

### Existing Foundation

`schemas/scenario.json` already defines multi-stage scenarios with `stages` (each carrying optional checks with DCs), `prerequisites` (faction requirements, quest requirements, min relationship), `trigger_conditions`, and `completion_rewards` (companion unlock, faction change, items). The blueprint pattern extends this schema -- blueprints add phase pools, selection rules, randomized phase counts, and NPC rosters on top of the existing stage-based structure.

### Structure

An event arc blueprint defines:
- **Phase pool** -- all possible phases (8-10 authored phase templates)
- **Selection rules** -- constraints on which phases appear and in what order
- **Phase count** -- randomized range (e.g., 4-6 per playthrough)
- **NPC rosters** -- authored anchors always appear; generated NPCs fill background slots

### Example: Hunter Exam Arc (hxh_au)

```yaml
hunter_exam_arc:
  type: "event_arc"
  phases: 4-6
  phase_pool:
    - endurance_chase       # stamina/constitution
    - cooking_challenge     # wisdom/knowledge
    - combat_tournament     # strength/dexterity
    - tracking_hunt         # perception/survival
    - puzzle_labyrinth      # intelligence
    - team_survival         # charisma/social + endurance
    - stealth_infiltration  # dexterity/deception
    - mental_endurance      # wisdom saves, psychological
  selection_rules:
    - must include at least one combat and one non-combat phase
    - final phase is always 1v1 (combat_tournament or mental_endurance)
    - no more than 2 consecutive physical phases
```

Each phase template specifies: structure, examiner pool, duration, checks involved, fail condition, elimination rate, terrain pool, hazard pool, and narrative beats.

### Playthrough Flow

1. Player selects starting scenario at the telescope
2. System rolls exam structure -- picks phases from pool, orders by rules, assigns examiners, picks terrain/hazards
3. Seeds candidate roster -- loads 4-5 authored notable candidates, generates 10-15 background candidates from templates
4. Stores as `event_arc_instance` tied to the character -- consistent across sessions
5. Narrative director reads the instance to know phase context, upcoming beats, and pacing

### Failure Handling

Failure is a story moment, not game over. An examiner gives a conditional pass, another candidate vouches at a cost, or the player fails the exam this year and trains for next year's different randomly-generated structure.

### Pattern Generalizes to All Worlds

| World | Major Event Arcs |
|---|---|
| hxh_au | Hunter Exam, Heavens Arena climb, Greed Island entry |
| inkglass_dark | Guild trials, siege defense, dungeon delve |
| murim | Martial arts tournament, sect entrance trial, cultivation breakthrough |
| cybernightlife | Corporate heist, street racing circuit, hacker gauntlet |
| wha_au | Atelier qualification test, forbidden magic investigation |
| atla_au | Bending mastery trials, spirit world journey |
| gachiakuta_au | Vertical ascent challenge, territory claim |

### Data Model

```yaml
event_arc_instance:
  id: "hunter_exam_player001_run1"
  blueprint_id: "hunter_exam_arc"
  character_id: "..."
  current_phase: 2
  phases:
    - { template: "endurance_chase", examiner: "satotz_analog", terrain: "swamp", status: "passed" }
    - { template: "cooking_challenge", examiner: "menchi_analog", ingredient: "deep_sea_fish", status: "active" }
    - { template: "combat_tournament", examiner: "netero_analog", status: "pending" }
    - { template: "tracking_hunt", examiner: "lippo_analog", terrain: "forest", status: "pending" }
    - { template: "mental_endurance", examiner: "netero_analog", status: "pending" }
  candidates: [ ...roster with relationship scores and elimination status... ]
```

---

## 5. Custom Arc Remixing

Players assemble arcs from unlocked building blocks. They're remixing, not authoring from scratch.

### Flow

Player opens world journal, sees building blocks that exist (locations visited, NPCs met, phase types played), and assembles their own arc skeleton from those pieces.

### Setup UI Concept

```
[Create Custom Arc]
Base template:  [ Tournament ]         <- templates you've unlocked
Phases:         [ 3 ]
Location:       [ Heavens Arena ]      <- locations you've been to
                [ Any ]                <- system picks
                [ Any ]
Featured NPCs:  [ + Add ]
                  Killua_analog  -- role: [ Rival ]

Phase 1: [ Combat bracket ]           <- from template's phase pool, or "Surprise me"
Phase 2: [ Surprise me ]
Phase 3: [ Mental endurance ]
[ Generate Arc ]
```

### Constraints

- System enforces blueprint rules. All-social tournament on a template requiring combat is rejected.
- "Surprise me" is always an option for every slot. Minimum custom arc: pick template + location, system handles the rest.
- Custom arcs flagged as `origin: "custom"` vs `origin: "authored"`. Official arcs gate progression milestones; custom arcs give XP and loot but don't unlock tier gates.
- Solves replayability -- player finishes Hunter Exam, saw 5 of 8 phase types, can build a 3-phase gauntlet using the 3 they missed.

### Data Model

Identical to system-generated arc instances. Only difference: `origin: "custom"`. Narrative director, check system, and scene management don't care who assembled the skeleton.

---

## 6. Three-Layer Narrative Model

### Layer 1: Freeform RP (No Structure)

Player talks to NPCs, explores, makes choices. The dialogue engine handles it. Stories emerge from NPC personality, player choices, and check results. This is where most playtime lives. No skeleton, no director intervention.

### Layer 2: Narrative Threads (Lightweight Tracking)

The system notices recurring interests, commitments, and revelations during freeform RP and tracks them as soft signals.

```yaml
thread: "missing_brother"
  source: conversation with dock_worker NPC
  mentions: 3 (across 2 scenes)
  player_stance: "committed to helping"
  related_npcs: [dock_worker, harbor_master]
  related_regions: [docks, outer_settlements]
```

No phases. No checks. Just the system remembering "the player cares about this." The narrative director uses threads to nudge -- the next NPC in that region heard something relevant, a hidden element appears with a related hint. Threads can connect to authored content if it exists, or resolve purely through RP.

### Layer 3: Authored Skeletons (Structured Set-Pieces)

Hunter Exam, guild trials, tournament arcs, dungeon delves. These have phases, mechanical rules, NPC rosters, and completion rewards. They need blueprints because the LLM alone can't maintain multi-session state like "we're in phase 3 of 5, 40% of candidates eliminated."

Players opt in via telescope scenario selection or by encountering trigger conditions during freeform play.

### Implementation: Narrative Signals in scene_analysis

Call 1 uses a `scene_analysis` tool (`_ANALYSIS_TOOL` in `relay/endpoints/dialogue.py`). Its current schema has four top-level fields: `checks`, `scene_changes`, `animation_directives`, and `draft_response`. This proposal adds a fifth field:

```json
"narrative_signals": [
  {
    "type": "commitment | interest | revelation | tension",
    "summary": "Player promised to help find dock worker's brother",
    "related_npcs": ["dock_worker_03"],
    "related_regions": ["docks"]
  }
]
```

Relay validates these (same as check validation -- don't trust raw LLM output), persists as lightweight thread records tied to the character. Narrative director reads active threads when constructing scene context, writes `director_signal` like "the player has been asking about missing people -- hint that others have gone missing too."

The signal goes into scene_state, prompt builder includes it in Call 1, LLM weaves it naturally. Player never sees the machinery -- the world just feels responsive.

---

## 7. Consequence System (Murder Hobo Response)

How the world responds to destructive player behavior using the same systems that handle positive outcomes.

### World Mutations in scene_analysis

Extend Call 1's `scene_analysis` tool schema (same `_ANALYSIS_TOOL` referenced in Section 6) with a sixth top-level field for faction/relationship/flag changes:

```json
"world_mutations": [
  {
    "type": "faction_standing_change",
    "faction_id": "merchants_guild",
    "delta": -15,
    "reason": "theft_witnessed"
  },
  {
    "type": "relationship_change",
    "npc_id": "shopkeeper_mara",
    "delta": -30,
    "reason": "robbed"
  },
  {
    "type": "world_flag_set",
    "flag": "wanted_in:market_district",
    "reason": "witnessed crime"
  }
]
```

Relay validates magnitudes (same pattern as check validation), applies through existing faction propagation. The infrastructure for this is built: `relay/factions/reputation.py` implements single-hop propagation (allies +50%, rivals -25%, capped at +-20 per propagated delta), and `relay/companions/relationship.py` provides clamped relationship scores with named tiers (hostile through bonded). The -15 to merchants_guild propagates to allied factions (city guard, trade caravans) and bumps rival factions (thieves' guild, black market). All mutations are auditable via `StateChangeLog` (`relay/state_log.py`).

### NPC Instance State

NPCs need per-player-world-instance status separate from their personality files.

```yaml
npc_instance:
  npc_id: "guard_captain_voss"
  character_id: "player_char_001"
  world_instance: "..."
  status: "alive"           # alive | injured | fled | dead | defeated
  hp_current: 45
  disposition_override: -80 # overrides base relationship
  flags: ["attacked_by_player", "seeking_revenge"]
  last_interaction_summary: "Player attacked without provocation in the market"
```

Prompt builder loads instance state alongside personality file. NPC acts accordingly -- refuses to talk, attacks on sight, or flees.

### Narrative Director Escalation Rules

Rules-based pattern matching (not LLM-driven):

```
Rule: crime_escalation
  condition: world_flags contains 3+ "npc_killed" in 24h game-time
    AND faction_standing("city_guard") < -30
  action: set director_signal = "Authorities actively hunting player. Armed patrols. Shopkeepers refuse service. Bounty posted."
  action: spawn_npc_encounter("bounty_hunter", player_region)
  action: set world_flag("bounty_active")
```

### Escalation Curve

| Turns | What Happens |
|---|---|
| Steal once | Faction hit, NPC relationship drops, wanted flag in district |
| Attack NPCs | Combat resolver handles mechanically. NPC instance state tracks injury/death. Region reputation drops. |
| Kill 3+ NPCs | Crime escalation fires. Bounty hunters, locked gates, NPCs flee. Companion loyalty strain increases. |
| Full murder hobo | Hostile faction standing (-60+) locks all shops. City guard attacks on sight. Companion confrontation scenes fire. Safe regions shrink. |

### Key Insight

Consequences use the same math as rewards. Faction standing going to -80 uses the same propagation as +80. Relationship tiers work both ways. Companion loyalty strain uses the existing `loyalty_strain_threshold` and `confrontation_scene_id`. Even the chaos path should produce interesting stories -- thieves' guild recruitment, a bounty hunter with their own story, the one NPC who still trusts you.

---

## 8. NPC Consequence Tags

Four tags governing how the consequence system treats violence against an NPC.

### Tag Definitions

| Tag | Meaning | Examples |
|---|---|---|
| `protected` | Full consequences. Faction hit, relationship hit, wanted flags, director escalation. | Shopkeepers, quest NPCs, civilians, faction leaders |
| `combatant` | No faction/crime consequences. Death tracked but expected. | Exam contestants, arena opponents, tournament brackets |
| `hostile` | No consequences. They attacked first or are inherently enemies. | Bandits, hostile fauna, monsters, enemy soldiers |
| `ephemeral` | No tracking at all. Tier 3 background NPCs. | Nameless crowd members, generated filler |

Default for any NPC without an explicit tag: `protected`. You opt NPCs *out* of consequences, not in. If a generated NPC is untagged, violence against them has consequences -- the safe default.

### Scenario Context Override

The same NPC might be protected in one context and a valid target in another. Scenario blueprints override profiles within scope:

```yaml
hunter_exam_arc:
  engagement_rules:
    override_profiles:
      - selector: "role:exam_contestant"
        profile: "combatant"
      - selector: "role:examiner"
        profile: "protected"    # examiners still off-limits
    sanctioned_violence: true   # combat within phases doesn't trigger crime flags
    death_handling: "defeated"  # "killed" NPCs are narratively "defeated/eliminated", not dead
```

The `death_handling: "defeated"` distinction matters. In the Hunter Exam, a defeated contestant is eliminated from the exam, not dead. Instance state marks `defeated` instead of `dead` so they can return as recurring characters, grudge matches, or friends.

### Context Examples

| Context | Participants | death_handling |
|---|---|---|
| Arena | All participants: `combatant` | `defeated` (knocked out) |
| Bandit ambush | Bandits: `hostile` | `killed` (permanent) |
| War scenario | Enemy soldiers: `hostile`, allied NPCs: `protected`, civilians: `protected` with elevated consequences | varies |
| Companion sparring | Companion: `combatant` | `incapacitated` (triggers existing companion system) |

### Evaluation Flow

Violence detected in scene_analysis:
1. Load NPC's `consequence_profile`
2. Check if active scenario has an override for this NPC
3. Apply effective profile: `protected` = full consequences, `combatant` = no crime flags + track defeat, `hostile` = no consequences + track kill, `ephemeral` = skip entirely

This is a lookup, not LLM reasoning. The relay reads the tag and applies the rules. The LLM never decides whether killing was justified -- it writes the narrative; the system decides mechanical consequences.

---

## 9. Prompt Builder Gap Analysis

### Current State

The prompt builder (`relay/ai/rp_prompts.py`) includes:
- NPC personality (full file) -- `build_rp_system_prompt(npc)` with prompt caching
- Conversation history (sliding window) -- last 40 messages via `_trim_history`
- Check results, passive hints, NPC memory summary -- `build_final_prose_messages(...)`

### Where the Gap Lives

The dialogue handler (`relay/endpoints/dialogue.py`) loads `scene_state` from the database and extracts `environmental_effects`. This data flows through `_resolve_and_finish_rp` and reaches `_commit_scene_state` for persistence. However, the prompt builder functions in `rp_prompts.py` never receive it -- their signatures accept `player_prose`, `history`, `check_results`, `passive_hints`, and `npc_memory_summary`, but no game-state parameters. The data is available in the dialogue handler but stops before reaching the LLM prompt.

### Missing Context

The following should be included in prompts but currently aren't. The gap is in the `rp_prompts.py` function signatures, not in data availability at the dialogue handler level.

| Context | Source | Available in handler? | In prompt? | Impact |
|---|---|---|---|---|
| Scene state | `scene.scene_state` | Yes (loaded from DB) | No | Environmental effects, emotional temperature, hidden elements |
| Faction standings | `char.faction_standing` for NPC's faction | No (not loaded) | No | NPC should react to player's reputation |
| Relationship score | `char.relationships[npc_id]` | No (not loaded) | No | NPC tone should match relationship tier |
| World flags | Session/character world flags | No | No | Wanted status, bounty, quest progress |
| Companion presence | `char.companions` (active list) | No (not loaded) | No | NPCs should acknowledge companions |
| Director signals | `scene.scene_state.director_signal` | Partially (scene_state loaded, field exists but nothing writes to it) | No | Pacing nudges from narrative director |
| NPC instance state | Per-player NPC status/disposition | No (model doesn't exist yet) | No | Injured/hostile/fled NPCs react accordingly |

### Priority

This is a critical gap. Without context injection, NPCs react the same regardless of whether the player is beloved or wanted for murder. The faction system, relationship tiers, and companion system all exist mechanically but have no effect on NPC dialogue. Fixing this requires: (1) loading character game-state in the dialogue handler, (2) extending `build_rp_system_prompt` and `build_analysis_messages` to accept and format that context, (3) adding the context as a Tier 2 (session-stable) cache block between the static NPC personality and the dynamic turn data.

---

## 10. Narrative Systems Status Audit

What exists vs. what's planned. Updated May 2026.

### Core Systems

| Layer | Status | Notes |
|---|---|---|
| Dialogue engine (RP + quick-chat) | **Built** | 2-pass LLM, WebSocket, pending turns, session recovery, AI chain depth limit (cap 10) |
| Scene lifecycle | **Built** | POST/GET/PATCH/end, scene_state with director_signal field |
| Session lifecycle | **Built** | Start/end/state, analytics, level increment |
| Prompt construction | **Built (incomplete)** | Missing scene_state, faction, relationship, world flag, companion, director context (see Section 9) |
| NPC loading | **Built** | LRU cache, async lock, hot reload |
| Check system | **Built** | Full resolution, advantage/disadvantage, passive checks, contested checks |
| Combat resolver | **Built** | Attack/save/heal/rest, damage types, conditions, death state, action economy |
| Faction system | **Built** | Standing +-100, five tiers, single-hop propagation (allies +50%, rivals -25%), capped at +-20 per propagation, custom thresholds, `FactionStandingLog` audit table |
| Relationship tiers | **Built** | Score +-100, seven named tiers (hostile through bonded), centralized mutation with clamping, per-NPC custom thresholds |
| Companion system | **Built** | Recruitment validation, combat AI, incapacitation/loyalty strain, recovery, dismissal, `CompanionHandler` for JSON column access |
| Condition registry | **Built** | Data-driven `relay/registry.py`, no hardcoded condition branches, graduated exhaustion, rider conditions, duration tracking |
| State change audit | **Built** | `StateChangeLog` table with typed mutation models (`HPChange`, `ConditionChange`, `ExhaustionChange`, `DeathStateChange`, `CompanionStateChange`, `RestEffect`), indexed by character + change_type + timestamp |
| Inventory management | **Built** | `InventoryHandler` with stacking, binding states, bulk replace for crafting |

### Unbuilt Systems (Relevant to These Proposals)

| Layer | Status | Notes |
|---|---|---|
| Scenario schema | **Schema exists, 1 example, no runtime** | `schemas/scenario.json` defines multi-stage scenarios. One authored example. No endpoints consume it. |
| Narrative director | **Does not exist** | `director_signal` field exists in scene_state schema. Nothing writes to it. `relay/narrative/` directory doesn't exist. |
| Canon mutation | **Does not exist** | `relay/canon/` directory doesn't exist. No diff agent, no confirmation flow. |
| Semantic RAG / Lore search | **Does not exist** | `lore/{world_id}/` directories exist (1 file). No embedding pipeline, no search endpoint, no prompt integration. |
| Multi-NPC scenes | **Does not exist** | Scenes are 1:1 (one NPC per scene). |
| DM Workshop / Narrative Control Panel | **Does not exist** | Library Workshop exists for content CRUD. DM Workshop and solo Narrative Control Panel are unbuilt. |
| Plot beats / World events | **Does not exist** | `world_events.json` schema exists. No endpoints consume it. No event trigger system. |
| Template generation | **Does not exist** | No `templates/` directories. No NPC/location generation endpoint. Required by Section 1. |
| NPC instance state | **Does not exist** | No per-player NPC status model. Required by Sections 7-8. |
| World flags | **Does not exist** | No model or storage for per-character world flags. Referenced by Sections 7-8. |

---

## 11. Menu and Tutorial Concept

### Ink-Drawn Cutscene Intro

A cat in a small town. Works in a library, fascinated by the stars. Large collection of books. The cat runs from the city through the forest to a fallen horizontal observatory -- the menu hub. The player avatar spawns here.

### Observatory Hub

- **Telescope** -- interact to see different worlds in glass bottles. Cycle through worlds, click to enter setup.
- **Glass bottles** -- each represents a `world_id`. Visual representation of worlds.
- **Book slots** -- three save files per world (three characters). Empty slot goes to scenario selection. Filled slot shows world state summary.

### World Entry Flow

Empty save slot: Scenario selection -> Preferences -> Character creation -> Enter world

Filled save slot: World state summary (story so far) -> Continue

### Save Icon

Cat writing in a book.

### Cat Mascot

Menu-space only. Not an in-world entity. No NPC personality file needed. Purely a UI/narrative framing device for the menu experience.

---

## 12. Implementation Dependencies

Proposals have ordering constraints. This section maps what blocks what.

### Dependency Graph

```
Section 9 (Prompt Builder Fix)
  ├── prerequisite for Section 6 (narrative signals need prompt injection to reach the LLM)
  ├── prerequisite for Section 7 (world mutations need NPC disposition in prompts to matter)
  └── prerequisite for Section 8 (consequence tags need prompt-injected instance state)

Section 1 (Template Generation)
  └── prerequisite for Section 4 (blueprints need template-instantiated NPCs for rosters)
      └── prerequisite for Section 5 (custom remixing needs blueprints to remix)

Section 7 (Consequence System)
  ├── requires: NPC instance state model (new)
  ├── requires: world flags model (new)
  ├── requires: scene_analysis tool schema extension (world_mutations field)
  └── prerequisite for Section 8 (consequence tags are a refinement of consequence evaluation)

Section 6 (Three-Layer Narrative)
  ├── requires: narrative director (relay/narrative/, currently unbuilt)
  ├── requires: scene_analysis tool schema extension (narrative_signals field)
  └── Layer 3 requires Section 4 (authored skeletons = blueprint arcs)
```

### Suggested Implementation Order

| Priority | What | Why |
|---|---|---|
| 1 | **Section 9: Prompt builder fix** | Unblocks all NPC-awareness features. Low risk, high impact. Extends existing function signatures. |
| 2 | **Section 7: Consequence system (world_mutations + NPC instance state)** | Extends scene_analysis tool schema. Creates the NPC instance state and world flags models that Sections 6 and 8 also need. |
| 3 | **Section 8: NPC consequence tags** | Refinement layer on top of Section 7. Small scope -- adds a `consequence_profile` field to NPC schemas and a lookup in the consequence evaluation flow. |
| 4 | **Section 6: Three-layer narrative (narrative_signals)** | Requires narrative director infrastructure (`relay/narrative/`). Extends scene_analysis tool with a second new field. |
| 5 | **Section 1: Template generation system** | New subsystem -- templates, generation endpoint, schema validation of generated content. |
| 6 | **Section 4: Blueprint event arcs** | Extends scenario.json, needs template generation for NPC rosters. |
| 7 | **Section 5: Custom arc remixing** | UI-driven feature on top of blueprints. |
| 8 | **Sections 2-3: World journal + starting preferences** | Low-dependency, can be built in parallel with any of the above. Section 2 is read-only aggregation; Section 3 is a data model + UI. |
| 9 | **Section 11: Menu/tutorial** | Unity-side. Independent of relay proposals. |

### Infrastructure Already Built

The following systems were built during relay spine development and de-risk these proposals:

- **Faction propagation** (`relay/factions/reputation.py`) -- ready to receive world_mutations from Section 7
- **Relationship tiers** (`relay/companions/relationship.py`) -- ready for prompt injection in Section 9
- **Companion loyalty/incapacitation** (`relay/companions/loyalty.py`) -- consequence escalation in Section 7 can trigger companion confrontation via existing `loyalty_strain_threshold`
- **StateChangeLog** (`relay/state_log.py`, `relay/mutations.py`) -- audit trail pattern ready to extend for world_mutations and narrative signals
- **Handler pattern** (`CompanionHandler`, `InventoryHandler`) -- pattern for NPC instance state handler in Section 7
- **Data-driven condition registry** (`relay/registry.py`) -- no hardcoded branches to collide with consequence-applied conditions
- **AI chain depth limit** (`_MAX_AI_CHAIN_DEPTH = 10` in `dialogue.py`) -- safety valve for Section 6's multi-layer narrative cascades

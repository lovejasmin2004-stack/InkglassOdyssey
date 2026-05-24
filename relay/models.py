from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    characters: Mapped[list[Character]] = relationship("Character", back_populates="account")
    sessions: Mapped[list[GameSession]] = relationship("GameSession", back_populates="account")


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    player_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.id"), nullable=False, index=True)
    world_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    specialisation_path_id: Mapped[str] = mapped_column(String, nullable=False)

    # Ability scores and proficiencies
    ability_scores: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    skill_proficiencies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    saving_throw_proficiencies: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Combat stats
    hp_current: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hp_max: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ac: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    passive_checks: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    conditions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    exhaustion_level: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    death_state_exhaustion_gained: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Resources and economy
    resources: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    wallet: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    inventory: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    equipped_gear: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    known_recipes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Position
    current_region_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Companions and narrative
    companions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    rp_voice_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    relationships: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    faction_standing: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    account: Mapped[Account] = relationship("Account", back_populates="characters")
    sessions: Mapped[list[GameSession]] = relationship("GameSession", back_populates="character")


class GameSession(Base):
    """Represents one play session (POST /session/start → POST /session/end)."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    player_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.id"), nullable=False, index=True)
    character_id: Mapped[str] = mapped_column(String, ForeignKey("characters.id"), nullable=False, index=True)
    world_id: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="solo")  # solo | multiplayer
    role: Mapped[str] = mapped_column(String, nullable=False, default="player")  # player | dm
    status: Mapped[str] = mapped_column(String, nullable=False, default="active", index=True)  # active | ended
    session_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    analytics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped[Account] = relationship("Account", back_populates="sessions")
    character: Mapped[Character] = relationship("Character", back_populates="sessions")
    scenes: Mapped[list[Scene]] = relationship("Scene", back_populates="session")


class Scene(Base):
    """A scene within a game session. Tracks NPC, dialogue history, and state."""

    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, ForeignKey("sessions.id"), nullable=False, index=True)
    npc_id: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="rp")  # rp | quickchat
    status: Mapped[str] = mapped_column(String, nullable=False, default="active", index=True)  # active | ended
    scene_state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    turn_history: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    scene_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    analytics: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[GameSession] = relationship("GameSession", back_populates="scenes")
    pending_turns: Mapped[list[PendingTurn]] = relationship("PendingTurn", back_populates="scene")


class PendingTurn(Base):
    """Tracks a turn through processing stages for crash recovery.

    Written before processing begins, updated at each stage.
    Invariant #12: session state persisted before processing.
    """

    __tablename__ = "pending_turns"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(String, ForeignKey("scenes.id"), nullable=False, index=True)
    player_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    npc_id: Mapped[str] = mapped_column(String, nullable=False)
    turn_type: Mapped[str] = mapped_column(String, nullable=False)  # rp | quickchat

    # received -> analysis -> checks_resolved -> streaming -> complete | failed
    stage: Mapped[str] = mapped_column(String, nullable=False, default="received", index=True)

    player_input: Mapped[str] = mapped_column(Text, nullable=False)
    character_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Intermediate results saved at each stage
    analysis_result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    check_results: Mapped[list | None] = mapped_column(JSON, nullable=True)
    animation_directives: Mapped[list | None] = mapped_column(JSON, nullable=True)
    scene_changes: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    final_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Retry tracking (#4): links retries to original turn, caps at MAX_RETRIES
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    parent_turn_id: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    scene: Mapped[Scene] = relationship("Scene", back_populates="pending_turns")


class FactionStandingLog(Base):
    """Immutable log of faction standing changes.

    Every standing change — direct or propagated — gets a row.
    Provides audit trail for debugging ("why is my standing -47?") and
    analytics (tier transition frequency, propagation impact).
    """

    __tablename__ = "faction_standing_log"
    __table_args__ = (sa.Index("ix_fsl_char_faction_created", "character_id", "faction_id", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    player_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.id"), nullable=False, index=True)
    character_id: Mapped[str] = mapped_column(String, ForeignKey("characters.id"), nullable=False, index=True)
    world_id: Mapped[str] = mapped_column(String, nullable=False)
    faction_id: Mapped[str] = mapped_column(String, nullable=False, index=True)

    old_standing: Mapped[int] = mapped_column(Integer, nullable=False)
    new_standing: Mapped[int] = mapped_column(Integer, nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    old_tier: Mapped[str] = mapped_column(String, nullable=False)
    new_tier: Mapped[str] = mapped_column(String, nullable=False)
    tier_changed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # "direct" | "allied_propagation" | "rival_propagation"
    source: Mapped[str] = mapped_column(String, nullable=False)
    # Which faction triggered propagation (None for direct changes)
    source_faction_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class TransactionLog(Base):
    """Immutable log of economy transactions (Invariant #14).

    Every wallet change — buy, sell, quest reward, admin grant — gets a row.
    """

    __tablename__ = "transaction_log"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    player_id: Mapped[str] = mapped_column(String, ForeignKey("accounts.id"), nullable=False, index=True)
    character_id: Mapped[str] = mapped_column(String, ForeignKey("characters.id"), nullable=False, index=True)
    world_id: Mapped[str] = mapped_column(String, nullable=False)

    # buy | sell | grant | quest_reward | gather | craft
    tx_type: Mapped[str] = mapped_column(String, nullable=False)

    # Positive = credit, negative = debit (always from the player's perspective)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    balance_after: Mapped[int] = mapped_column(Integer, nullable=False)

    # Optional references
    item_id: Mapped[str | None] = mapped_column(String, nullable=True)
    item_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    npc_id: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Structured references
    quest_id: Mapped[str | None] = mapped_column(String, nullable=True)
    region_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Pricing metadata (for audit)
    base_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    markup_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    faction_modifier: Mapped[float | None] = mapped_column(Float, nullable=True)
    sell_back_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class StateChangeLog(Base):
    """Immutable log of mechanical state changes (combat, conditions, companions).

    Extends the audit trail pattern from TransactionLog (economy) and
    FactionStandingLog (factions) to the remaining gap areas: HP mutations,
    condition applications, exhaustion changes, death state transitions,
    companion state changes, and rest effects.
    """

    __tablename__ = "state_change_log"
    __table_args__ = (sa.Index("ix_scl_char_type_created", "character_id", "change_type", "created_at"),)

    id: Mapped[str] = mapped_column(String, primary_key=True)
    character_id: Mapped[str] = mapped_column(String, ForeignKey("characters.id"), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # hp_change | condition_add | condition_remove | condition_expire |
    # exhaustion_change | death_state | companion_state | rest |
    # faction_standing_change | relationship_change | world_flag_set
    change_type: Mapped[str] = mapped_column(String, nullable=False, index=True)

    # What triggered this change
    source: Mapped[str] = mapped_column(String, nullable=False)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")

    # Numeric delta for quick queries (negative = damage, positive = healing/gain)
    delta: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    new_value: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Optional references
    npc_id: Mapped[str | None] = mapped_column(String, nullable=True)
    condition_id: Mapped[str | None] = mapped_column(String, nullable=True)
    damage_type: Mapped[str | None] = mapped_column(String, nullable=True)
    rest_type: Mapped[str | None] = mapped_column(String, nullable=True)
    field: Mapped[str | None] = mapped_column(String, nullable=True)
    faction_id: Mapped[str | None] = mapped_column(String, nullable=True)
    flag: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class NpcInstanceState(Base):
    """Per-player NPC state tracking for the consequence system.

    Separate from NPC personality files — tracks how *this player's* actions
    have changed an NPC's disposition, status, and flags.  Created lazily on
    first meaningful interaction (attack, theft, quest completion) rather than
    for every NPC the player meets.

    Design doc: docs/design_proposals.md §7 (Consequence System)
    """

    __tablename__ = "npc_instance_state"
    __table_args__ = (
        sa.UniqueConstraint("character_id", "npc_id", name="uq_nis_char_npc"),
        sa.Index("ix_nis_character_id", "character_id"),
        sa.Index("ix_nis_npc_id", "npc_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    character_id: Mapped[str] = mapped_column(String, ForeignKey("characters.id"), nullable=False)
    npc_id: Mapped[str] = mapped_column(String, nullable=False)
    world_id: Mapped[str] = mapped_column(String, nullable=False)

    # alive | injured | fled | dead | defeated
    status: Mapped[str] = mapped_column(String, nullable=False, default="alive")

    # Current HP — only set once combat or damage has occurred. None = untracked.
    hp_current: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Override the base relationship score for this NPC specifically.
    # When set, the prompt builder uses this instead of char.relationships[npc_id].
    disposition_override: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Arbitrary flags: ["attacked_by_player", "seeking_revenge", "quest_ally"]
    flags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Brief summary of last significant interaction (for prompt context)
    last_interaction_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class NarrativeThread(Base):
    """Lightweight narrative thread tracking (design_proposals.md §6).

    Layer 2 of the Three-Layer Narrative Model.  The system notices
    recurring interests, commitments, and revelations during freeform RP
    and tracks them as soft signals.  The narrative director reads active
    threads when constructing scene context and writes director_signal
    hints so the world feels responsive.

    Threads are per-character, per-world.  They accumulate mention_count
    across scenes and sessions.  Status transitions:
      active → resolved  (thread concluded narratively)
      active → dormant   (player lost interest / long gap)
      dormant → active   (player re-engages)
    """

    __tablename__ = "narrative_threads"
    __table_args__ = (
        sa.UniqueConstraint("character_id", "thread_key", name="uq_nt_char_thread"),
        sa.Index("ix_nt_character_status", "character_id", "status"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    character_id: Mapped[str] = mapped_column(String, ForeignKey("characters.id"), nullable=False)
    world_id: Mapped[str] = mapped_column(String, nullable=False)

    # Short snake_case key, e.g. "missing_brother", "guild_corruption"
    thread_key: Mapped[str] = mapped_column(String, nullable=False)

    # commitment | interest | revelation | tension
    signal_type: Mapped[str] = mapped_column(String, nullable=False)

    # Human-readable summary of the thread's current state
    summary: Mapped[str] = mapped_column(Text, nullable=False)

    # NPCs and regions involved (for director nudge targeting)
    related_npcs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    related_regions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # How many turns have mentioned this thread
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # active | resolved | dormant
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")

    # Session tracking for recency
    first_seen_session_id: Mapped[str | None] = mapped_column(String, nullable=True)
    last_seen_session_id: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class EventArcInstanceRow(Base):
    """Per-character event arc instance (design_proposals.md §4).

    Stores the concrete instantiation of an event arc blueprint:
    selected phases, assigned NPCs, per-phase status.  Persistent
    across sessions — the narrative director reads the active arc
    to know phase context, upcoming beats, and pacing.

    The ``phases`` and ``candidates`` columns store JSON arrays
    matching the EventArcInstance Pydantic model's nested structure.
    """

    __tablename__ = "event_arc_instances"
    __table_args__ = (
        sa.UniqueConstraint("character_id", "blueprint_id", "id", name="uq_eai_char_bp_id"),
        sa.Index("ix_eai_character_status", "character_id", "status"),
        sa.Index("ix_eai_world_id", "world_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    blueprint_id: Mapped[str] = mapped_column(String, nullable=False)
    world_id: Mapped[str] = mapped_column(String, nullable=False)
    character_id: Mapped[str] = mapped_column(String, ForeignKey("characters.id"), nullable=False)

    # "authored" (system-generated from blueprint) or "custom" (player-assembled, §5)
    origin: Mapped[str] = mapped_column(String, nullable=False, default="authored")

    # "active" | "completed" | "failed" | "abandoned"
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")

    # Index into the phases array
    current_phase_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # JSON array of phase dicts: [{phase_id, status, examiner_npc_id, terrain, hazards, result_summary}]
    phases: Mapped[list] = mapped_column(JSON, nullable=False)

    # JSON array of candidate dicts: [{npc_id, is_anchor, status, relationship_score, eliminated_at_phase}]
    candidates: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Session that most recently advanced this arc
    session_id: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class WorldFlag(Base):
    """Per-character world flags for the consequence and narrative systems.

    Boolean or string flags tracking world-state changes caused by player
    actions: wanted status, bounty active, quest milestones, region locks.
    Read by the narrative director escalation rules and prompt builder.

    Design doc: docs/design_proposals.md §7 (Consequence System)
    """

    __tablename__ = "world_flags"
    __table_args__ = (
        sa.UniqueConstraint("character_id", "flag", name="uq_wf_char_flag"),
        sa.Index("ix_wf_character_id", "character_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    character_id: Mapped[str] = mapped_column(String, ForeignKey("characters.id"), nullable=False)

    # Flag key, e.g. "wanted_in:market_district", "bounty_active", "guild_trial_passed"
    flag: Mapped[str] = mapped_column(String, nullable=False)

    # Flag value — "true" for booleans, or a string for richer state
    value: Mapped[str] = mapped_column(String, nullable=False, default="true")

    # Why this flag was set
    reason: Mapped[str] = mapped_column(String, nullable=False, default="")

    # What triggered it: consequence | quest | scenario | admin | narrative_director
    source: Mapped[str] = mapped_column(String, nullable=False, default="consequence")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)


class CharacterPreferences(Base):
    """Per-character starting scenario preferences (design_proposals.md §3).

    Stored when the player enters a world.  The backstory blurb, story
    interests, and topics to avoid are injected into the prompt.  The
    four tuning knobs (content_rating, narrative_pace, companion_interest,
    exploration_style) adjust narrative director behaviour.

    One row per character.  Created on first world entry, updated via
    PATCH /character/{id}/preferences.
    """

    __tablename__ = "character_preferences"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String, ForeignKey("characters.id"), nullable=False, unique=True, index=True
    )
    world_id: Mapped[str] = mapped_column(String, nullable=False)

    # Player-written backstory blurb (up to 2000 chars)
    backstory_blurb: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # JSON arrays of tag strings
    story_interests: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    topics_to_avoid: Mapped[list] = mapped_column(JSON, nullable=False, default=list)

    # Tuning knobs
    content_rating: Mapped[str] = mapped_column(String, nullable=False, default="moderate")
    narrative_pace: Mapped[str] = mapped_column(String, nullable=False, default="moderate")
    companion_interest: Mapped[str] = mapped_column(String, nullable=False, default="moderate")
    exploration_style: Mapped[str] = mapped_column(String, nullable=False, default="balanced")

    # Player's personal NPC notes (JSON: {npc_id: "note text"})
    npc_notes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

from __future__ import annotations

from datetime import UTC, datetime

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

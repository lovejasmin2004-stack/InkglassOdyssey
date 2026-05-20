"""Live RP mode integration test — sends prose to Seta, expects two-call flow.

Run the relay first:  uvicorn relay.main:app --port 8002
Then:  python -m relay.tests.test_rp_live 8002
"""

from __future__ import annotations

import asyncio
import json
import sys

import websockets

from relay.auth.tokens import create_account_token

# Andalu's character sheet (subset needed for check resolution)
ANDALU = {
    "ability_scores": {
        "strength": 10,
        "dexterity": 16,
        "constitution": 14,
        "intelligence": 12,
        "wisdom": 14,
        "charisma": 10,
    },
    "skill_proficiencies": ["stealth", "perception", "survival", "athletics", "investigation"],
    "level": 6,
}


async def main() -> None:
    token = create_account_token(player_id="player_001", tier=1)
    port = sys.argv[1] if len(sys.argv) > 1 else "8000"
    uri = f"ws://127.0.0.1:{port}/dialogue"

    async with websockets.connect(uri) as ws:
        # --- auth ---
        await ws.send(json.dumps({"type": "auth", "token": token}))
        print("[auth] sent\n")

        # --- send RP turn with prose that should trigger a check ---
        turn = {
            "type": "rp_turn",
            "npc_id": "seta_inkglass_dark",
            "text": (
                "Andalu steps closer to the workbench, eyes tracing the vials and instruments. "
                "Something about the arrangement feels deliberate — not just organised, hidden. "
                "She tries to read the labels on the bottles pushed to the back of the shelf, "
                "the ones Seta clearly doesn't want casual visitors noticing."
            ),
            "character": ANDALU,
        }
        await ws.send(json.dumps(turn))
        print(f"[you] {turn['text']}\n")

        # --- collect all messages ---
        checks_received = []
        animations_received = []
        scene_updates = []
        full_text = ""

        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=60)
            msg = json.loads(raw)
            t = msg["type"]

            if t == "stream_start":
                print(f"--- stream_start (turn_id={msg['turn_id']}) ---")

            elif t == "check_result":
                checks_received.append(msg)
                status = "PASS" if msg["passed"] else "FAIL"
                print(
                    f"[CHECK] {msg['skill']} DC {msg['dc']}: "
                    f"rolled {msg['roll']} + {msg['modifier']} = {msg['total']} -> {status}"
                )
                if msg.get("reason"):
                    print(f"        reason: {msg['reason']}")

            elif t == "animation_directive":
                animations_received.append(msg)
                print(f"[ANIM] {msg['target']}: {msg['directive']}")

            elif t == "scene_update":
                scene_updates.append(msg)
                print(f"[SCENE] {msg.get('changes', {})}")

            elif t == "stream_chunk":
                print(msg["text"], end="", flush=True)
                full_text += msg["text"]

            elif t == "stream_end":
                print("\n\n--- stream_end ---")
                break

            elif t == "error":
                print(f"\n[ERROR] {msg['code']}: {msg['message']}")
                break

        # --- summary ---
        print(f"\nChecks:     {len(checks_received)}")
        print(f"Animations: {len(animations_received)}")
        print(f"Scene:      {len(scene_updates)}")
        print(f"Response:   {len(full_text)} chars")


if __name__ == "__main__":
    asyncio.run(main())

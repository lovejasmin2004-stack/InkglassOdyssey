"""Live integration test: full session lifecycle with dialogue.

Run the relay first:  uvicorn relay.main:app --port 8002
Then:  python -m relay.tests.test_session_live 8002
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx
import websockets

from relay.auth.tokens import create_account_token

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
    port = sys.argv[1] if len(sys.argv) > 1 else "8000"
    base_url = f"http://127.0.0.1:{port}"
    ws_uri = f"ws://127.0.0.1:{port}/dialogue"

    token = create_account_token(player_id="player_001", tier=1)
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(base_url=base_url) as http:
        # === 1. Create character ===
        print("=== Creating character ===")
        resp = await http.post(
            "/character",
            json={
                "world_id": "inkglass_dark",
                "name": "Andalu",
                "specialisation_path_id": "scout",
                "ability_scores": ANDALU["ability_scores"],
                "skill_proficiencies": ANDALU["skill_proficiencies"],
                "saving_throw_proficiencies": ["dexterity", "wisdom"],
            },
            headers=headers,
        )
        assert resp.status_code == 201, f"Character create failed: {resp.text}"
        character_id = resp.json()["id"]
        print(f"  Character ID: {character_id}")

        # === 2. Start session ===
        print("\n=== Starting session ===")
        resp = await http.post(
            "/session/start",
            json={"character_id": character_id, "world_id": "inkglass_dark"},
            headers=headers,
        )
        assert resp.status_code == 201, f"Session start failed: {resp.text}"
        session_data = resp.json()
        session_id = session_data["session_id"]
        session_token = session_data["session_token"]
        print(f"  Session ID: {session_id}")
        print(f"  Session token received: {len(session_token)} chars")

        # === 3. Start scene ===
        print("\n=== Starting scene ===")
        resp = await http.post(
            "/scene",
            json={
                "session_id": session_id,
                "npc_id": "seta_inkglass_dark",
                "mode": "quickchat",
            },
            headers=headers,
        )
        assert resp.status_code == 201, f"Scene start failed: {resp.text}"
        scene_data = resp.json()
        scene_id = scene_data["id"]
        print(f"  Scene ID: {scene_id}")
        print(f"  NPC: {scene_data['npc_id']}")
        print(f"  Mode: {scene_data['mode']}")

        # === 4. Have a conversation via WebSocket ===
        print("\n=== WebSocket dialogue ===")
        async with websockets.connect(ws_uri) as ws:
            # Auth
            await ws.send(json.dumps({"type": "auth", "token": token}))

            # Send quickchat turn
            turn = {
                "type": "quickchat_turn",
                "npc_id": "seta_inkglass_dark",
                "text": "Hello Seta. What are you working on today?",
            }
            await ws.send(json.dumps(turn))
            print(f"  [you] {turn['text']}")

            full_text = ""
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                msg = json.loads(raw)
                t = msg["type"]

                if t == "stream_start":
                    print(f"  --- stream_start (turn_id={msg['turn_id']}) ---")
                elif t == "stream_chunk":
                    print(msg["text"], end="", flush=True)
                    full_text += msg["text"]
                elif t == "stream_end":
                    print(f"\n  --- stream_end ---")
                    break
                elif t == "error":
                    print(f"\n  [ERROR] {msg['code']}: {msg['message']}")
                    break

        print(f"  Response length: {len(full_text)} chars")

        # === 5. Check session state (should show the scene) ===
        print("\n=== Checking session state ===")
        resp = await http.get(f"/session/{session_id}/state", headers=headers)
        assert resp.status_code == 200, f"Session state failed: {resp.text}"
        state = resp.json()
        print(f"  Status: {state['status']}")
        print(f"  Scenes: {len(state['scenes'])}")
        for s in state["scenes"]:
            print(f"    - {s['npc_id']} ({s['mode']}, {s['status']}, {s['turn_count']} turns)")

        # === 6. End scene ===
        print("\n=== Ending scene ===")
        resp = await http.post(f"/scene/{scene_id}/end", headers=headers)
        assert resp.status_code == 200, f"Scene end failed: {resp.text}"
        ended_scene = resp.json()
        print(f"  Status: {ended_scene['status']}")
        print(f"  Summary: {ended_scene['scene_summary']}")

        # === 7. End session ===
        print("\n=== Ending session ===")
        resp = await http.post(
            f"/session/{session_id}/end",
            json={"level_increment": False},
            headers=headers,
        )
        assert resp.status_code == 200, f"Session end failed: {resp.text}"
        ended = resp.json()
        print(f"  Status: {ended['status']}")
        print(f"  Summary: {ended['session_summary']}")
        print(f"  Scenes ended by session close: {ended['scenes_ended']}")
        print(f"  Analytics: {json.dumps(ended['analytics'], indent=2)}")

        # === 8. Verify session is truly ended ===
        print("\n=== Verifying final state ===")
        resp = await http.get(f"/session/{session_id}/state", headers=headers)
        final = resp.json()
        assert final["status"] == "ended", f"Expected 'ended', got '{final['status']}'"
        assert final["session_summary"], "Missing session summary"
        print(f"  Final status: {final['status']}")
        print(f"  Summary present: {bool(final['session_summary'])}")
        print(f"  Analytics present: {bool(final['analytics'])}")

    print("\n=== ALL CHECKS PASSED ===")


if __name__ == "__main__":
    asyncio.run(main())

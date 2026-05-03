"""Live integration test — connects to the dialogue WebSocket, talks to Seta.

Run the relay first:
    uvicorn relay.main:app --port 8000

Then:
    python -m relay.tests.test_dialogue_live
"""
from __future__ import annotations

import asyncio
import json
import sys

import websockets

from relay.auth.tokens import create_account_token


async def main() -> None:
    token = create_account_token(player_id="player_001", tier=1)
    port = sys.argv[1] if len(sys.argv) > 1 else "8000"
    uri = f"ws://127.0.0.1:{port}/dialogue"

    async with websockets.connect(uri) as ws:
        # --- authenticate ---
        await ws.send(json.dumps({"type": "auth", "token": token}))
        print("[auth] sent token")

        # --- send a quick-chat turn ---
        turn = {
            "type": "quickchat_turn",
            "npc_id": "seta_inkglass_dark",
            "text": "I heard you're a healer. How much to look at a wound on my arm?",
        }
        await ws.send(json.dumps(turn))
        print(f"\n[you] {turn['text']}\n")

        # --- receive the streamed response ---
        full = ""
        while True:
            raw = await ws.recv()
            msg = json.loads(raw)

            if msg["type"] == "stream_start":
                print(f"[{msg['npc_id']}] ", end="", flush=True)

            elif msg["type"] == "stream_chunk":
                chunk = msg["text"]
                full += chunk
                print(chunk, end="", flush=True)

            elif msg["type"] == "stream_end":
                print("\n")
                print(f"[stream_end] turn_id={msg['turn_id']}")
                print(f"[full_text] {msg['full_text'][:200]}...")
                break

            elif msg["type"] == "error":
                print(f"\n[ERROR] {msg['code']}: {msg['message']}")
                break

    print("\n--- done ---")


if __name__ == "__main__":
    asyncio.run(main())

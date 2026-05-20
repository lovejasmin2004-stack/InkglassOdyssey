# Inkglass Odyssey

Private, application-gated, multi-world AI-narrative collaborative-fiction RPG.

Unity desktop client communicating with a Python relay service for persistent state and AI integration. Solo and small-group multiplayer (up to 8 players).

## Architecture

- **Relay** (Python 3.12+, FastAPI): source of truth for all game state, AI integration via Anthropic SDK
- **Unity** (6.1 LTS): desktop client, rendering and input only
- **Admin** (port 8081): Library Workshop for content editing, RP Tester for NPC dialogue testing

See [CLAUDE.md](CLAUDE.md) for full architecture documentation and design documents in `docs/`.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env  # Edit with your API keys

# Run database migrations
alembic upgrade head

# Start the relay
uvicorn relay.main:app --host 127.0.0.1 --port 8000

# Start admin interface (optional)
python -m relay.admin.app
```

## Testing

```bash
pytest relay/tests/
```

## Worlds

| World | Setting | Tier |
|-------|---------|------|
| inkglass_dark | Dark fantasy | 1 |
| murim | Wuxia/cultivation | 1 |
| cybernightlife | Neon-saturated urban | 1 |
| wha_au | Witch Hat Atelier AU | 2 |
| atla_au | Avatar AU | 2 |
| gachiakuta_au | Gachiakuta AU | 2 |
| hxh_au | Hunter x Hunter AU | 2 |

## License

Private project. All rights reserved.

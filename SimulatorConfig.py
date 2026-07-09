# ─── SimulatorConfig.py ───────────────────────────────────────────────────────
# Central configuration for the Arschloch card game.
# These are the defaults; per-game settings are passed at runtime.

deckSize    = 'full'
enablePass  = True
gameRanks   = ["President", "Vice-President", "Citizen", "Vice-Brokie", "Brokie"]
rankOrder   = ["2","3","4","5","6","7","8","9","T","J","Q","K","A"]
gameEntities = 5
LOG_PASSWORD = "Ente"
LOG_FILE     = "game_logs.jsonl"

# ─── SimulatorConfig.py ───────────────────────────────────────────────────────

gameRanks    = ["President", "Vice-President", "Citizen", "Vice-Brokie", "Brokie"]
rankOrder    = ["2", "3", "4", "5", "6", "7", "8", "9", "T", "J", "Q", "K", "A"]
gameEntities = 5
enablePass   = True

ADMIN_PASSWORD = "Ente"     # Logs, Beobachten, Abbrechen
LOG_PASSWORD   = ADMIN_PASSWORD

DEFAULT_BOT_DELAY_MS = 900   # 0 = Bots spielen ohne Verzögerung
MAX_BOT_DELAY_MS     = 5000

LOG_FILE    = "/tmp/game_logs.jsonl"     # Rundenlogs (Backup, Render: nicht persistent)
STATE_FILE  = "/tmp/arschloch_state.json"  # laufende Spiele → überlebt Server-Neustart
GAME_TTL_S  = 6 * 3600                   # inaktive Spiele nach 6 h aufräumen

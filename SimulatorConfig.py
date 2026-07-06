# ─── SimulatorConfig.py ───────────────────────────────────────────────────────
# Central configuration for the Arschloch card game.
# These are the defaults; per-game settings are passed at runtime.

# Deck size: 'full' (2–A) or 'half' (7–A)
deckSize = 'full'

# Whether players can pass
enablePass = True

# Rank titles assigned in finishing order
gameRanks = ["President", "Vice-President", "Citizen", "Vice-Brokie", "Brokie"]

# Card rank order (low → high)
rankOrder = ["2","3","4","5","6","7","8","9","T","J","Q","K","A"]

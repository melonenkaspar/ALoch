# ─── SimulatorFunctions.py ────────────────────────────────────────────────────
import copy, random
from SimulatorConfig import rankOrder

_RANK_TO_BASE = {"2":0,"3":4,"4":8,"5":12,"6":16,"7":20,
                 "8":24,"9":28,"T":32,"J":36,"Q":40,"K":44,"A":48}
_BASE_TO_RANK = {v:k for k,v in _RANK_TO_BASE.items()}
_SUIT_TO_OFFSET = {"♣":0,"♦":1,"♥":2,"♠":3}
_OFFSET_TO_SUIT = {v:k for k,v in _SUIT_TO_OFFSET.items()}

# ── Deck ──────────────────────────────────────────────────────────────────────

def createDeck():
    suits = ["♣","♦","♥","♠"]
    return [f"{r}{s}" for r in rankOrder for s in suits]

def shuffleDeck(deck):
    random.shuffle(deck)

def dealHands(deck, players, citizenStack):
    it = 0
    for _ in range(len(deck) % len(players)):
        citizenStack.append(deck.pop())
    while deck:
        players[it % len(players)]["hand"].append(deck.pop())
        it += 1

def sortHand(hand):
    def val(c): return _RANK_TO_BASE.get(c[0],0) + _SUIT_TO_OFFSET.get(c[1],0)
    return sorted(hand, key=val)

# ── Entities ──────────────────────────────────────────────────────────────────

def createEntities(names):
    return [{"rank":None,"hand":[],"entityID":i,"name":names[i],"isBot":False}
            for i in range(len(names))]

# ── Card helpers ──────────────────────────────────────────────────────────────

def getRank(card):     return card[0]
def getSuit(card):     return card[1]
def getMoveSize(move): return move[2]
def cardPower(card):   return _RANK_TO_BASE[getRank(card)]

# ── Moves ─────────────────────────────────────────────────────────────────────

def possibleMoves(players, entity, activePlay, enablePass=True):
    moves = []
    activePower = -1
    activeSize  = None
    if activePlay is not None:
        activePower = _RANK_TO_BASE[getRank(activePlay)]
        activeSize  = activePlay[2]

    hand = copy.deepcopy(players[entity]["hand"])
    for card in hand:
        cp = _RANK_TO_BASE[getRank(card)]
        if activePower < cp:
            moves.append(f"{_BASE_TO_RANK[cp]}x1")

    for move in moves[:]:
        cnt = moves.count(move)
        if cnt > 1:
            moves.append(f"{move[0]}x{cnt}")
            moves.remove(move)

    final = []
    for move in moves:
        if activeSize is not None:
            if getMoveSize(move) == activeSize or getMoveSize(move) == '4':
                final.append(move)
        else:
            final.append(move)

    if enablePass:
        final.append('pass')
    return final

def onlyPassAvailable(players, entity, activePlay, enablePass=True):
    return possibleMoves(players, entity, activePlay, enablePass) == ['pass']

def randomMove(moves):
    """Bots passen nie freiwillig, wenn es echte Züge gibt."""
    real = [m for m in moves if m != 'pass']
    return random.choice(real) if real else 'pass'

# ── Play ──────────────────────────────────────────────────────────────────────

def play(players, entity, pile, activePlay):
    playedRank = activePlay[0]; played = 0; i = 0
    while i < len(players[entity]["hand"]) and played < int(activePlay[2]):
        if getRank(players[entity]["hand"][i]) == playedRank:
            pile.append(players[entity]["hand"][i])
            players[entity]["hand"].remove(players[entity]["hand"][i])
            played += 1
            continue
        i += 1

# ── Trading ───────────────────────────────────────────────────────────────────

def buildTrades(players, gameRanks):
    """
    Trading mit Wunsch-Mechanik:
      President wünscht sich einen Rang vom Brokie (2 Karten).
      Vice-President wünscht sich einen Rang vom Vice-Brokie (1 Karte).
      Citizen tauscht nicht.
    """
    rank_to_id = {p["rank"]: p["entityID"] for p in players if p["rank"]}
    trades = []
    pairs = [("President", "Brokie", 2), ("Vice-President", "Vice-Brokie", 1)]
    for top_rank, bot_rank, count in pairs:
        if top_rank in rank_to_id and bot_rank in rank_to_id:
            trades.append({
                "pair":        top_rank,
                "top_id":      rank_to_id[top_rank],
                "bot_id":      rank_to_id[bot_rank],
                "count":       count,
                "wish_rank":   None,
                "wish_done":   False,
                "give_done":   False,
                "return_done": False,
            })
    return trades

def resolveGive(players, trade):
    """Unterer Spieler gibt Karten gemäß Wunsch ab (sonst seine besten)."""
    bot_id    = trade["bot_id"]
    top_id    = trade["top_id"]
    wish_rank = trade["wish_rank"]
    count     = trade["count"]
    hand      = sortHand(players[bot_id]["hand"])

    wished = [c for c in hand if getRank(c) == wish_rank]
    cards  = wished[:count]

    if len(cards) < count:
        remaining = [c for c in hand if c not in cards]
        cards += remaining[-(count - len(cards)):]

    for c in cards:
        players[bot_id]["hand"].remove(c)
        players[top_id]["hand"].append(c)
    for p in players:
        p["hand"] = sortHand(p["hand"])
    return cards

def resolveReturn(players, trade, return_cards):
    """Oberer Spieler gibt seine schlechtesten Karten zurück."""
    top_id = trade["top_id"]
    bot_id = trade["bot_id"]
    for c in return_cards:
        players[top_id]["hand"].remove(c)
        players[bot_id]["hand"].append(c)
    for p in players:
        p["hand"] = sortHand(p["hand"])

def pendingWishFor(trades, entity_id):
    for t in trades:
        if t["top_id"] == entity_id and not t["wish_done"]:
            return t
    return None

def pendingGiveFor(trades, entity_id):
    for t in trades:
        if t["bot_id"] == entity_id and t["wish_done"] and not t["give_done"]:
            return t
    return None

def pendingReturnFor(trades, entity_id):
    for t in trades:
        if t["top_id"] == entity_id and t["give_done"] and not t["return_done"]:
            return t
    return None

def pendingTradeFor(players, trades, entity_id):
    return (pendingWishFor(trades, entity_id) or
            pendingGiveFor(trades, entity_id) or
            pendingReturnFor(trades, entity_id))

def allTradesDone(trades):
    return all(t["return_done"] for t in trades)

def autoBotTrade(players, trades):
    """Führt alle offenen Bot-Aktionen im Trading aus."""
    did_something = False
    for t in trades:
        top_id = t["top_id"]
        bot_id = t["bot_id"]
        if not t["wish_done"] and players[top_id].get("isBot"):
            all_ranks = ["A","K","Q","J","T","9","8","7","6","5","4","3","2"]
            owned = {getRank(c) for c in players[top_id]["hand"]}
            t["wish_rank"] = next((r for r in all_ranks if r not in owned),
                                  random.choice(all_ranks))
            t["wish_done"] = True
            did_something = True
        if t["wish_done"] and not t["give_done"] and players[bot_id].get("isBot"):
            resolveGive(players, t)
            t["give_done"] = True
            did_something = True
        if t["give_done"] and not t["return_done"] and players[top_id].get("isBot"):
            hand = sortHand(players[top_id]["hand"])
            resolveReturn(players, t, hand[:t["count"]])
            t["return_done"] = True
            did_something = True
    return did_something

# ── Move-Log-Helfer ───────────────────────────────────────────────────────────

def _appendMoveLog(s, entity_id, name, move, auto=False):
    entry = {
        "round": s.get('roundNumber', 1),
        "trick": s.get('trickCounter', 0),
        "p":     entity_id,
        "name":  name,
        "move":  move,
    }
    if auto:
        entry["auto"] = True
    s.setdefault('moveLog', []).append(entry)

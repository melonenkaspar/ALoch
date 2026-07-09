# ─── SimulatorFunctions.py ────────────────────────────────────────────────────
import copy, random
from SimulatorConfig import rankOrder, gameRanks

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

def getRank(card):   return card[0]
def getSuit(card):   return card[1]
def getMoveSize(move): return move[2]
def cardPower(card): return _RANK_TO_BASE[getRank(card)]

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

# ── Trading (classic rank-distance) ──────────────────────────────────────────

def buildTrades(players, gameRanks):
    """
    Classic trading:
      President  ↔ Brokie        (2 cards each)
      Vice-Pres  ↔ Vice-Brokie   (1 card each)
      Citizen    → nothing
    Returns list of {"from": id, "to": id, "count": n, "direction": "give_best"|"give_worst"}
    """
    rank_to_id = {p["rank"]: p["entityID"] for p in players if p["rank"]}
    trades = []
    pairs = [
        ("President",      "Brokie",       2),
        ("Vice-President", "Vice-Brokie",  1),
    ]
    for top_rank, bot_rank, count in pairs:
        if top_rank in rank_to_id and bot_rank in rank_to_id:
            top_id = rank_to_id[top_rank]
            bot_id = rank_to_id[bot_rank]
            # Brokie gives best cards to President
            trades.append({"from": bot_id, "to": top_id,
                           "count": count, "direction": "give_best"})
            # President gives worst cards to Brokie
            trades.append({"from": top_id, "to": bot_id,
                           "count": count, "direction": "give_worst"})
    return trades

def executeTrade(players, trade):
    """Execute one side of a trade immediately (used for bots)."""
    eid   = trade["from"]
    hand  = sortHand(players[eid]["hand"])
    count = trade["count"]
    if trade["direction"] == "give_best":
        cards = hand[-count:]
    else:
        cards = hand[:count]
    for c in cards:
        players[eid]["hand"].remove(c)
        players[trade["to"]]["hand"].append(c)
    for p in players:
        p["hand"] = sortHand(p["hand"])
    return cards

def pendingTradeFor(players, trades, entity_id):
    """Return the pending trade where this entity must give cards, or None."""
    for t in trades:
        if t["from"] == entity_id and not t.get("done"):
            return t
    return None

# ── Turn management ───────────────────────────────────────────────────────────

def advanceTurn(s, g, enablePass=True):
    trickOrder = s['trickOrder']
    for _ in range(len(trickOrder) * 2 + 1):
        current = s['currentTurn']
        if s['players'][current]['rank'] is not None:
            if current in trickOrder:
                idx = trickOrder.index(current)
                s['currentTurn'] = trickOrder[(idx+1) % len(trickOrder)]
            continue
        if onlyPassAvailable(s['players'], current, s['mostRecentMove'], enablePass):
            name = s['players'][current]['name']
            s['log'].append(f'{name} passt automatisch.')
            _appendMoveLog(s, current, name, 'pass', auto=True)
            s['passedCounter'] += 1
            active = len([e for e in trickOrder if s['players'][e]['rank'] is None])
            if s['passedCounter'] >= active:
                _resetRound(s, current, trickOrder)
                continue
            if current in trickOrder:
                idx = trickOrder.index(current)
                s['currentTurn'] = trickOrder[(idx+1) % len(trickOrder)]
            continue
        # Bot?
        if s['players'][current].get('isBot'):
            moves = possibleMoves(s['players'], current, s['mostRecentMove'], enablePass)
            botMove = randomMove(moves)
            _executeBotMove(s, g, current, botMove, enablePass)
            continue
        break

def _executeBotMove(s, g, entity_id, move, enablePass=True):
    from SimulatorFunctions import play, sortHand, assignRank
    trickOrder  = s['trickOrder']
    name        = s['players'][entity_id]['name']
    if move == 'pass':
        s['log'].append(f'{name} passt.')
        _appendMoveLog(s, entity_id, name, 'pass')
        s['passedCounter'] += 1
    else:
        play(s['players'], entity_id, s['stack'], move)
        s['players'][entity_id]['hand'] = sortHand(s['players'][entity_id]['hand'])
        s['mostRecentMove'] = move
        s['passedCounter']  = 0
        s['log'].append(f'{name} spielt {move}.')
        _appendMoveLog(s, entity_id, name, move)
        if not s['players'][entity_id]['hand']:
            done = assignRank(s, g, entity_id, move, trickOrder)
            if done: return
            if trickOrder:
                s['currentTurn'] = trickOrder[0]
            return
    active = len([e for e in trickOrder if s['players'][e]['rank'] is None])
    if s['passedCounter'] >= active:
        _resetRound(s, entity_id, trickOrder)
        return
    if entity_id in trickOrder:
        idx = trickOrder.index(entity_id)
        s['currentTurn'] = trickOrder[(idx+1) % len(trickOrder)]

def _resetRound(s, triggerEntity, trickOrder):
    s['log'].append('Alle gepasst – neue Runde.')
    s['passedCounter'] = 0
    for card in s['stack']:
        s['discardStack'].append(card)
    s['mostRecentMove'] = None
    s['stack'].clear()
    cur_idx   = trickOrder.index(triggerEntity) if triggerEntity in trickOrder else 0
    new_order = [trickOrder[(cur_idx+i) % len(trickOrder)] for i in range(len(trickOrder))]
    s['trickOrder']     = new_order
    s['nextTrickOrder'] = new_order[:]
    s['currentTurn']    = new_order[0]

def assignRank(s, g, entity_id, move, trickOrder):
    remaining = s['remainingRanks']
    name      = s['players'][entity_id]['name']
    if not remaining: return False
    if getRank(move) == 'A':
        s['players'][entity_id]['rank'] = remaining[-1]; del remaining[-1]
    else:
        s['players'][entity_id]['rank'] = remaining[0];  del remaining[0]
    s['log'].append(f'{name} fertig → {s["players"][entity_id]["rank"]}!')
    if len(remaining) == 1:
        last = next((e for e in trickOrder if s['players'][e]['rank'] is None), None)
        if last is not None:
            s['players'][last]['rank'] = remaining[0]; del remaining[0]
            s['log'].append(f'{s["players"][last]["name"]} letzter → {s["players"][last]["rank"]}!')
    if entity_id in trickOrder:     trickOrder.remove(entity_id)
    if entity_id in s.get('nextTrickOrder',[]): s['nextTrickOrder'].remove(entity_id)
    if not remaining or len(trickOrder) <= 1:
        s['finished'] = True
        s['log'].append('🎉 Runde beendet!')
        g['status'] = 'trading'
        return True
    return False

# ── Move log helper ───────────────────────────────────────────────────────────

def _appendMoveLog(s, entity_id, name, move, auto=False):
    entry = {
        "round": s.get('roundNumber', 1),
        "trick": s.get('trickCounter', 0),
        "p":     entity_id,
        "name":  name,
        "move":  move,
    }
    if auto: entry["auto"] = True
    s.setdefault('moveLog', []).append(entry)

# ── Console helpers ───────────────────────────────────────────────────────────

def projectConsole(players, citizenStack, stack, discardStack):
    print(players)
    print("C-Stack:", citizenStack)
    print("Pile:", stack)
    print("Discarded:", discardStack)

def printSeparator():
    print("-"*60)

def randomMoveFromList(lst):
    return lst[random.randrange(len(lst))] if lst else 'pass'

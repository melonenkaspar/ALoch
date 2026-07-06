# ─── SimulatorFunctions.py ────────────────────────────────────────────────────
import copy
import random
from SimulatorConfig import rankOrder, gameRanks

_RANK_TO_BASE = {"2": 0, "3": 4, "4": 8, "5": 12, "6": 16, "7": 20,
                 "8": 24, "9": 28, "T": 32, "J": 36, "Q": 40, "K": 44, "A": 48}
_BASE_TO_RANK = {v: k for k, v in _RANK_TO_BASE.items()}
_SUIT_TO_OFFSET = {"♣": 0, "♦": 1, "♥": 2, "♠": 3}
_OFFSET_TO_SUIT = {v: k for k, v in _SUIT_TO_OFFSET.items()}

# ─── Deck ─────────────────────────────────────────────────────────────────────

def createDeck(deckSize='full'):
    suits = ["♣", "♦", "♥", "♠"]
    ranks = rankOrder if deckSize == 'full' else rankOrder[rankOrder.index("7"):]
    return [f"{r}{s}" for r in ranks for s in suits]

def shuffleDeck(deck):
    random.shuffle(deck)

def dealHands(deck, players, citizenStack):
    iteration = 0
    for _ in range(len(deck) % len(players)):
        citizenStack.append(deck.pop())
    while deck:
        players[iteration % len(players)]["hand"].append(deck.pop())
        iteration += 1

def sortHand(hand):
    """Sort a hand from lowest to highest, grouped by rank then suit."""
    def card_value(card):
        return _RANK_TO_BASE.get(card[0], 0) + _SUIT_TO_OFFSET.get(card[1], 0)
    return sorted(hand, key=card_value)

# ─── Entities ─────────────────────────────────────────────────────────────────

def createEntities(names):
    return [{"rank": None, "hand": [], "entityID": i, "name": names[i]}
            for i in range(len(names))]

# ─── Card helpers ─────────────────────────────────────────────────────────────

def getRank(card):
    return card[0]

def getSuit(card):
    return card[1]

def getMoveSize(move):
    return move[2]

def cardPower(card):
    return _RANK_TO_BASE[getRank(card)]

# ─── Moves ────────────────────────────────────────────────────────────────────

def possibleMoves(players, entity, activePlay, enablePass=True):
    moves = []
    activePower = -1
    activeSize = None

    if activePlay is not None:
        activePower = _RANK_TO_BASE[getRank(activePlay)]
        activeSize = activePlay[2]

    hand = copy.deepcopy(players[entity]["hand"])
    for card in hand:
        cp = _RANK_TO_BASE[getRank(card)]
        if activePower < cp:
            moves.append(f"{_BASE_TO_RANK[cp]}x1")

    for move in moves[:]:
        moveCount = moves.count(move)
        if moveCount > 1:
            moves.append(f"{move[0]}x{moveCount}")
            moves.remove(move)

    finalMoves = []
    for move in moves:
        if activeSize is not None:
            if getMoveSize(move) == activeSize or getMoveSize(move) == str(4):
                finalMoves.append(move)
        else:
            finalMoves.append(move)

    if enablePass:
        finalMoves.append('pass')
    return finalMoves

def onlyPassAvailable(players, entity, activePlay, enablePass=True):
    moves = possibleMoves(players, entity, activePlay, enablePass)
    return moves == ['pass']

# ─── Play ─────────────────────────────────────────────────────────────────────

def play(players, entity, pile, activePlay):
    playedRank = activePlay[0]
    playedCount = 0
    index = 0
    while index < len(players[entity]["hand"]) and playedCount < int(activePlay[2]):
        if getRank(players[entity]["hand"][index]) == playedRank:
            pile.append(players[entity]["hand"][index])
            players[entity]["hand"].remove(players[entity]["hand"][index])
            playedCount += 1
            continue
        index += 1

# ─── Turn management ──────────────────────────────────────────────────────────

def advanceTurn(s, g, enablePass=True):
    """Skip ahead past players who can only pass, auto-logging them.
    Stops when a player with real choices is found."""
    trickOrder = s['trickOrder']

    for _ in range(len(trickOrder) * 2):  # safety limit
        current = s['currentTurn']

        if s['players'][current]['rank'] is not None:
            # Already finished – skip
            if current in trickOrder:
                idx = trickOrder.index(current)
                s['currentTurn'] = trickOrder[(idx + 1) % len(trickOrder)]
            continue

        if onlyPassAvailable(s['players'], current, s['mostRecentMove'], enablePass):
            player_name = s['players'][current]['name']
            s['log'].append(f'{player_name} passt automatisch.')
            s['passedCounter'] += 1

            active_count = len([e for e in trickOrder if s['players'][e]['rank'] is None])
            if s['passedCounter'] >= active_count:
                _resetRound(s, current, trickOrder)
                continue

            if current in trickOrder:
                idx = trickOrder.index(current)
                s['currentTurn'] = trickOrder[(idx + 1) % len(trickOrder)]
            continue

        # Real decision needed – stop
        break

def _resetRound(s, triggerEntity, trickOrder):
    s['log'].append('Alle gepasst – neue Runde.')
    s['passedCounter'] = 0
    for card in s['stack']:
        s['discardStack'].append(card)
    s['mostRecentMove'] = None
    s['stack'].clear()
    cur_idx = trickOrder.index(triggerEntity) if triggerEntity in trickOrder else 0
    new_order = [trickOrder[(cur_idx + i) % len(trickOrder)] for i in range(len(trickOrder))]
    s['trickOrder'] = new_order
    s['nextTrickOrder'] = copy.deepcopy(new_order)
    s['currentTurn'] = new_order[0]

# ─── Rank assignment ──────────────────────────────────────────────────────────

def assignRank(s, entity_id, move, trickOrder, g):
    """Assign a rank to a player who just emptied their hand.
    Returns True if the game is over."""
    remainingRanks = s['remainingRanks']
    player_name = s['players'][entity_id]['name']

    if not remainingRanks:
        return False

    if getRank(move) == 'A':
        s['players'][entity_id]['rank'] = remainingRanks[-1]
        del remainingRanks[-1]
    else:
        s['players'][entity_id]['rank'] = remainingRanks[0]
        del remainingRanks[0]
    s['log'].append(f'{player_name} ist fertig → {s["players"][entity_id]["rank"]}!')

    # Assign last remaining rank if only one left
    if len(remainingRanks) == 1:
        last = next((e for e in trickOrder if s['players'][e]['rank'] is None), None)
        if last is not None:
            s['players'][last]['rank'] = remainingRanks[0]
            del remainingRanks[0]
            s['log'].append(f'{s["players"][last]["name"]} ist letzter → {s["players"][last]["rank"]}!')

    if entity_id in trickOrder: trickOrder.remove(entity_id)
    if entity_id in s['nextTrickOrder']: s['nextTrickOrder'].remove(entity_id)

    if not remainingRanks or len(trickOrder) <= 1:
        s['finished'] = True
        s['log'].append('🎉 Spiel beendet!')
        g['status'] = 'finished'
        return True

    return False

# ─── Console helpers (kept for local testing) ────────────────────────────────

def projectConsole(players, citizenStack, stack, discardStack):
    print(players)
    print("C-Stack:", citizenStack)
    print("Pile:", stack)
    print("Discarded:", discardStack)

def printSeparator():
    print("-" * 60)

def randomMove(possibleMoveList):
    if not possibleMoveList:
        return 'pass'
    return possibleMoveList[random.randrange(len(possibleMoveList))]

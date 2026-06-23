import copy
import random
from itertools import count

from SimulatorConfig import *

_RANK_TO_BASE = {"2": 0, "3": 4, "4": 8, "5": 12, "6": 16, "7": 20,
                 "8": 24, "9": 28, "T": 32, "J": 36, "Q": 40, "K": 44, "A": 48}

_BASE_TO_RANK = {v: k for k, v in _RANK_TO_BASE.items()}  # reverse lookup

_SUIT_TO_OFFSET = {"♣": 0, "♦": 1, "♥": 2, "♠": 3}
_OFFSET_TO_SUIT = {v: k for k, v in _SUIT_TO_OFFSET.items()}  # reverse lookup


def value_to_notation(value):
    """Convert a card integer (0-51) to two-character notation e.g. 'A♠', 'T♦'."""
    if not isinstance(value, int) or not (0 <= value < 52):
        return ""
    return _BASE_TO_RANK[value & ~3] + _OFFSET_TO_SUIT[value & 3]


def notation_to_value(notation):
    """Convert two-character card notation e.g. 'A♠' to its integer value (0-51).
    Returns None for invalid input instead of silently yielding 0 (which is a valid card).
    """
    if (isinstance(notation, str) and len(notation) == 2
            and notation[0] in _RANK_TO_BASE and notation[1] in _SUIT_TO_OFFSET):
        return _RANK_TO_BASE[notation[0]] + _SUIT_TO_OFFSET[notation[1]]
    return None

def createDeck():
    if deckSize == 'full':
        suits = ["♣", "♦", "♥", "♠"]
        ranks = ["2","3","4","5","6","7","8","9","T","J","Q","K","A"]
        return [f"{r}{s}" for r in ranks for s in suits]
    if deckSize == 'half':
        suits = ["♣", "♦", "♥", "♠"]
        ranks = ["7","8","9","T","J","Q","K","A"]
        return [f"{r}{s}" for r in ranks for s in suits]
    else: return []

def createEntities():
    players = []
    for entity in range(gameEntities):
        players.append({
            "rank": None,
            "hand": [],
            "entityID": entity
        })
    return players

def shuffleDeck(deck):
    random.shuffle(deck)

def getRank(card):
    return card[0]

def getSuit(card):
    return card[1]

def getMoveSize(move):
    return move[2]

def dealHands(deck, players, citizenStack):
    iteration = 0
    for overshoot in range(len(deck) % len(players)):
        citizenStack.append(deck.pop())
    while deck:
        players[iteration % len(players)]["hand"].append(deck.pop())
        iteration += 1

def projectConsole(players, citizenStack, stack, discardStack):
    print(players)
    print("C-Stack:",citizenStack)
    print("Pile:",stack)
    print("Discarded:", discardStack)

def play(players, entity, pile, activePlay):
    playedRank = activePlay[0]; playedCount = 0
    index = 0
    while (index < len(players[entity]["hand"])) and (playedCount < int(activePlay[2])):
        if(getRank(players[entity]["hand"][index])) == playedRank:
            pile.append(players[entity]["hand"][index])
            players[entity]["hand"].remove(players[entity]["hand"][index])
            playedCount += 1
            continue
        index += 1

#Returns a list of all possible moves, considering the current hand and the laid card
def possibleMoves(players, entity, activePlay):
    moves = []
    activePower = -1; '''if nothing has been played yet, everything can be played'''
    activeSize = None
    if activePlay is not None:
        activePower = _RANK_TO_BASE[getRank(activePlay)]
        activeSize = activePlay[2]

    '''Creates all possible Moves'''
    hand = copy.deepcopy(players[entity]["hand"])
    for card in hand:
        cardPower = _RANK_TO_BASE[getRank(card)] #powerHand
        if activePower < cardPower:
            '''Tier 1 Moves can already Append'''
            moves.append(f"{_BASE_TO_RANK[cardPower]}x1")
    '''Adds a move variant and destroys one instance until all possible move counts are met'''
    for move in moves:
        moveCount = moves.count(move)
        if moveCount > 1:
            moves.append(f"{move[0]}x{moveCount}")
            moves.remove(move)

    '''Filters Moves based on the current play [accounting for number of cards played]'''
    finalMoves = []
    for move in moves:
        if activeSize is not None:
            if (getMoveSize(move) == activeSize) or (getMoveSize(move) == str(4)):
                finalMoves.append(move)
        else :
            finalMoves.append(move)

    if enablePass: finalMoves.append('pass') #(Possible Addition to pass a move)
    return finalMoves

def updateActiveEntities(players):
    activeEntities = []
    for entity in range(gameEntities):
        if players[entity]["rank"] is None:
            activeEntities.append(entity)
    return activeEntities

def randomMove(possibleMoveList):
    if not possibleMoveList:
        return 'pass'
    else:
        return possibleMoveList[random.randrange(0, len(possibleMoveList))]

def playerFinished(players, entityID, playersFinished):
    if players[entityID]["rank"] is not None: return True
    elif players[entityID]["hand"] is None:
        players[entityID]["rank"] = gameRanks[playersFinished]
        playersFinished += 1
        return True
    else:
        return False

def printSeparator():
    print("-"*60)

'''
Currently in Debugging or development:

no not use... Trust me.
'''

def transfer(pulledRank, cardCount, players, entityID, destination):
    for card in players[entityID]["hand"]:
        while cardCount > 1:
            if rankOrder.index(getRank(card)) > rankOrder.index(pulledRank):
                destination.append(card)
                players[entityID]["hand"].remove(card)
                cardCount -= 1

# ─── SimulatorFunctions.py ────────────────────────────────────────────────────
"""Reine Spiellogik – kein Flask, kein globaler Zustand, alles testbar."""

import random
from collections import Counter
from typing import Optional

from SimulatorConfig import rankOrder

SUITS = ["♣", "♦", "♥", "♠"]

_RANK_TO_BASE   = {r: i * 4 for i, r in enumerate(rankOrder)}
_BASE_TO_RANK   = {v: k for k, v in _RANK_TO_BASE.items()}
_SUIT_TO_OFFSET = {s: i for i, s in enumerate(SUITS)}

# Ränge von stark nach schwach – für Bot-Wünsche und UI
RANKS_DESC = list(reversed(rankOrder))


# ── Deck ──────────────────────────────────────────────────────────────────────

def createDeck() -> list:
    return [f"{r}{s}" for r in rankOrder for s in SUITS]


def shuffleDeck(deck: list) -> None:
    random.shuffle(deck)


def dealHands(deck: list, players: list, citizenStack: list) -> None:
    """Verteilt reihum; der nicht teilbare Rest landet im Citizen-Stack."""
    for _ in range(len(deck) % len(players)):
        citizenStack.append(deck.pop())
    i = 0
    while deck:
        players[i % len(players)]["hand"].append(deck.pop())
        i += 1


def cardValue(card: str) -> int:
    return _RANK_TO_BASE.get(card[0], 0) + _SUIT_TO_OFFSET.get(card[1], 0)


def sortHand(hand: list) -> list:
    return sorted(hand, key=cardValue)


# ── Entities ──────────────────────────────────────────────────────────────────

def createEntities(names: list) -> list:
    return [{"rank": None, "hand": [], "entityID": i, "name": names[i], "isBot": False}
            for i in range(len(names))]


# ── Karten-/Zug-Helfer ────────────────────────────────────────────────────────

def getRank(card: str) -> str:     return card[0]
def getSuit(card: str) -> str:     return card[1]
def getMoveSize(move: str) -> int: return int(move[2])
def cardPower(card: str) -> int:   return _RANK_TO_BASE[getRank(card)]


def possibleMoves(players: list, entity: int, activePlay: Optional[str],
                  enablePass: bool = True) -> list:
    """
    Alle legalen Züge. Ein Zug heißt "Rxn" (z.B. "Qx2").
    Regeln: höherer Rang als der Stapel; gleiche Kartenanzahl – oder ein Vierling (Bombe).
    """
    activePower = -1
    activeSize  = None
    if activePlay is not None:
        activePower = _RANK_TO_BASE[getRank(activePlay)]
        activeSize  = getMoveSize(activePlay)

    moves = []
    for rank, n in Counter(getRank(c) for c in players[entity]["hand"]).items():
        if _RANK_TO_BASE[rank] <= activePower:
            continue
        for size in range(1, n + 1):
            if activeSize is None or size == activeSize or size == 4:
                moves.append(f"{rank}x{size}")

    moves.sort(key=lambda m: (_RANK_TO_BASE[m[0]], getMoveSize(m)))
    if enablePass:
        moves.append('pass')
    return moves


def onlyPassAvailable(players: list, entity: int, activePlay: Optional[str],
                      enablePass: bool = True) -> bool:
    return possibleMoves(players, entity, activePlay, enablePass) == ['pass']


def randomMove(moves: list) -> str:
    """Bots passen nie freiwillig, wenn es echte Züge gibt."""
    real = [m for m in moves if m != 'pass']
    return random.choice(real) if real else 'pass'


def play(players: list, entity: int, pile: list, move: str) -> list:
    """Legt die Karten des Zugs auf den Stapel. Gibt die gelegten Karten zurück."""
    rank, size = move[0], getMoveSize(move)
    hand   = players[entity]["hand"]
    picked = [c for c in hand if getRank(c) == rank][:size]
    for c in picked:
        hand.remove(c)
        pile.append(c)
    return picked


# ── Trading ───────────────────────────────────────────────────────────────────

TRADE_PAIRS = [("President", "Brokie", 2), ("Vice-President", "Vice-Brokie", 1)]


def buildTrades(players: list) -> list:
    """
    Wunsch-Mechanik:
      President wünscht sich einen Rang vom Brokie (2 Karten),
      Vice-President einen vom Vice-Brokie (1 Karte). Citizen tauscht nicht.
    """
    rank_to_id = {p["rank"]: p["entityID"] for p in players if p["rank"]}
    trades = []
    for top_rank, bot_rank, count in TRADE_PAIRS:
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


def _moveCards(players: list, src: int, dst: int, cards: list) -> None:
    for c in cards:
        players[src]["hand"].remove(c)
        players[dst]["hand"].append(c)
    for p in players:
        p["hand"] = sortHand(p["hand"])


def requiredWishCards(hand: list, wish_rank: str, count: int) -> int:
    """Wie viele Karten des gewünschten Rangs *müssen* abgegeben werden."""
    return min(sum(1 for c in hand if getRank(c) == wish_rank), count)


def resolveGive(players: list, trade: dict) -> list:
    """Unterer Spieler gibt Wunschkarten ab, füllt sonst mit seinen besten auf."""
    hand   = sortHand(players[trade["bot_id"]]["hand"])
    wished = [c for c in hand if getRank(c) == trade["wish_rank"]]
    cards  = wished[:trade["count"]]
    if len(cards) < trade["count"]:
        rest = [c for c in hand if c not in cards]
        cards += rest[-(trade["count"] - len(cards)):]
    _moveCards(players, trade["bot_id"], trade["top_id"], cards)
    return cards


def resolveReturn(players: list, trade: dict, return_cards: list) -> None:
    _moveCards(players, trade["top_id"], trade["bot_id"], return_cards)


def pendingWishFor(trades: list, entity_id: int) -> Optional[dict]:
    return next((t for t in trades
                 if t["top_id"] == entity_id and not t["wish_done"]), None)


def pendingGiveFor(trades: list, entity_id: int) -> Optional[dict]:
    return next((t for t in trades
                 if t["bot_id"] == entity_id and t["wish_done"] and not t["give_done"]), None)


def pendingReturnFor(trades: list, entity_id: int) -> Optional[dict]:
    return next((t for t in trades
                 if t["top_id"] == entity_id and t["give_done"] and not t["return_done"]), None)


def pendingTradeFor(trades: list, entity_id: int) -> Optional[dict]:
    return (pendingWishFor(trades, entity_id)
            or pendingGiveFor(trades, entity_id)
            or pendingReturnFor(trades, entity_id))


def allTradesDone(trades: list) -> bool:
    return all(t["return_done"] for t in trades)


def autoBotTrade(players: list, trades: list) -> bool:
    """Führt alle offenen Bot-Schritte aus. True, wenn etwas passiert ist."""
    did = False
    for t in trades:
        top, bot = players[t["top_id"]], players[t["bot_id"]]

        if not t["wish_done"] and top.get("isBot"):
            owned = {getRank(c) for c in top["hand"]}
            t["wish_rank"] = next((r for r in RANKS_DESC if r not in owned),
                                  random.choice(RANKS_DESC))
            t["wish_done"] = True
            did = True

        if t["wish_done"] and not t["give_done"] and bot.get("isBot"):
            resolveGive(players, t)
            t["give_done"] = True
            did = True

        if t["give_done"] and not t["return_done"] and top.get("isBot"):
            resolveReturn(players, t, sortHand(top["hand"])[:t["count"]])
            t["return_done"] = True
            did = True
    return did


# ── Move-Log ──────────────────────────────────────────────────────────────────

def appendMoveLog(s: dict, entity_id: int, name: str, move: str, *,
                  legal=None, hand=None, order=None, citizen=None,
                  auto: bool = False) -> None:
    """Schreibt einen Zug samt Kontext (für CSV-Export / Analyse)."""
    s.setdefault('moveLog', []).append({
        "round":   s.get('roundNumber', 1),
        "trick":   s.get('trickCounter', 0),
        "p":       entity_id,
        "name":    name,
        "move":    move,
        "auto":    auto,
        "legal":   list(legal or []),
        "hand":    list(hand or []),
        "order":   list(order or []),
        "citizen": list(citizen or []),
    })

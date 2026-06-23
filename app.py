import copy
import random
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import uuid
import os

app = Flask(__name__)
app.secret_key = 'arschloch-secret-key-2024'
CORS(app)

# ─── Config ───────────────────────────────────────────────────────────────────
enablePass = True
deckSize = 'full'
gameRanks = ["President", "Vice-President", "Citizen", "Vice-Brokie", "Brokie"]

# ─── Card Engine ──────────────────────────────────────────────────────────────
_RANK_TO_BASE = {"2": 0, "3": 4, "4": 8, "5": 12, "6": 16, "7": 20,
                 "8": 24, "9": 28, "T": 32, "J": 36, "Q": 40, "K": 44, "A": 48}
_BASE_TO_RANK = {v: k for k, v in _RANK_TO_BASE.items()}
_SUIT_TO_OFFSET = {"♣": 0, "♦": 1, "♥": 2, "♠": 3}
_OFFSET_TO_SUIT = {v: k for k, v in _SUIT_TO_OFFSET.items()}

def createDeck():
    suits = ["♣", "♦", "♥", "♠"]
    ranks = ["2","3","4","5","6","7","8","9","T","J","Q","K","A"] if deckSize == 'full' else ["7","8","9","T","J","Q","K","A"]
    return [f"{r}{s}" for r in ranks for s in suits]

def createEntities(names):
    return [{"rank": None, "hand": [], "entityID": i, "name": names[i]} for i in range(len(names))]

def shuffleDeck(deck): random.shuffle(deck)
def getRank(card): return card[0]
def getMoveSize(move): return move[2]

def dealHands(deck, players, citizenStack):
    iteration = 0
    for _ in range(len(deck) % len(players)):
        citizenStack.append(deck.pop())
    while deck:
        players[iteration % len(players)]["hand"].append(deck.pop())
        iteration += 1

def play(players, entity, pile, activePlay):
    playedRank = activePlay[0]; playedCount = 0; index = 0
    while index < len(players[entity]["hand"]) and playedCount < int(activePlay[2]):
        if getRank(players[entity]["hand"][index]) == playedRank:
            pile.append(players[entity]["hand"][index])
            players[entity]["hand"].remove(players[entity]["hand"][index])
            playedCount += 1
            continue
        index += 1

def possibleMoves(players, entity, activePlay):
    moves = []
    activePower = -1
    activeSize = None
    if activePlay is not None:
        activePower = _RANK_TO_BASE[getRank(activePlay)]
        activeSize = activePlay[2]

    hand = copy.deepcopy(players[entity]["hand"])
    for card in hand:
        cardPower = _RANK_TO_BASE[getRank(card)]
        if activePower < cardPower:
            moves.append(f"{_BASE_TO_RANK[cardPower]}x1")

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

def only_pass_available(players, entity, activePlay):
    """Returns True if the only legal move is 'pass'."""
    moves = possibleMoves(players, entity, activePlay)
    return moves == ['pass']

def advance_turn(s, g):
    """Advance turn, auto-passing players who have no other option. Returns when a real decision is needed."""
    trickOrder = s['trickOrder']
    remainingRanks = s['remainingRanks']

    for _ in range(len(trickOrder) * 2):  # safety limit
        current = s['currentTurn']

        # Skip players who are already finished
        if s['players'][current]['rank'] is not None:
            idx = trickOrder.index(current) if current in trickOrder else 0
            s['currentTurn'] = trickOrder[(idx + 1) % len(trickOrder)]
            continue

        # Auto-pass if no real moves available
        if only_pass_available(s['players'], current, s['mostRecentMove']):
            player_name = s['players'][current]['name']
            s['log'].append(f'{player_name} passt automatisch.')
            s['passedCounter'] += 1

            # Check if round should reset
            active_count = len([e for e in trickOrder if s['players'][e]['rank'] is None])
            if s['passedCounter'] >= active_count:
                s['log'].append('Alle gepasst – neue Runde.')
                s['passedCounter'] = 0
                for card in s['stack']:
                    s['discardStack'].append(card)
                s['mostRecentMove'] = None
                s['stack'].clear()
                # Restart from current player
                cur_idx = trickOrder.index(current) if current in trickOrder else 0
                new_order = [trickOrder[(cur_idx + i) % len(trickOrder)] for i in range(len(trickOrder))]
                s['trickOrder'] = new_order
                s['nextTrickOrder'] = copy.deepcopy(new_order)
                s['currentTurn'] = new_order[0]
                continue

            # Advance to next
            idx = trickOrder.index(current) if current in trickOrder else 0
            s['currentTurn'] = trickOrder[(idx + 1) % len(trickOrder)]
            continue

        # This player has real choices – stop here
        break

# ─── Game State Store ─────────────────────────────────────────────────────────
games = {}

# ─── Routes ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/lobby/create', methods=['POST'])
def create_lobby():
    data = request.json
    player_name = data.get('name', 'Player').strip()[:20]
    max_players = int(data.get('max_players', 5))
    max_players = max(2, min(5, max_players))  # clamp 2–5
    game_id = str(uuid.uuid4())[:6].upper()

    games[game_id] = {
        'status': 'lobby',
        'players': [{'id': str(uuid.uuid4()), 'name': player_name, 'entityID': 0}],
        'max_players': max_players,
        'state': None
    }
    host_id = games[game_id]['players'][0]['id']
    return jsonify({'game_id': game_id, 'player_id': host_id})

@app.route('/api/lobby/join', methods=['POST'])
def join_lobby():
    data = request.json
    game_id = data.get('game_id', '').strip().upper()
    player_name = data.get('name', 'Player').strip()[:20]

    if game_id not in games:
        return jsonify({'error': 'Spiel nicht gefunden'}), 404
    g = games[game_id]
    if g['status'] != 'lobby':
        return jsonify({'error': 'Spiel läuft bereits'}), 400
    if len(g['players']) >= g['max_players']:
        return jsonify({'error': 'Lobby ist voll'}), 400

    player_id = str(uuid.uuid4())
    entity_id = len(g['players'])
    g['players'].append({'id': player_id, 'name': player_name, 'entityID': entity_id})
    return jsonify({'game_id': game_id, 'player_id': player_id})

@app.route('/api/lobby/<game_id>', methods=['GET'])
def lobby_status(game_id):
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[game_id]
    return jsonify({
        'status': g['status'],
        'players': [{'name': p['name'], 'entityID': p['entityID']} for p in g['players']],
        'max_players': g['max_players']
    })

@app.route('/api/lobby/<game_id>/start', methods=['POST'])
def start_game(game_id):
    data = request.json
    player_id = data.get('player_id')

    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[game_id]
    if g['players'][0]['id'] != player_id:
        return jsonify({'error': 'Nur der Host kann starten'}), 403
    if len(g['players']) < 2:
        return jsonify({'error': 'Mindestens 2 Spieler benötigt'}), 400

    names = [p['name'] for p in g['players']]
    actual_count = len(names)

    citizenDeck = []; stack = []; discardStack = []
    deck = createDeck(); shuffleDeck(deck)
    players = createEntities(names)
    dealHands(deck, players, citizenDeck)

    trickOrder = list(range(actual_count))
    random.shuffle(trickOrder)
    remainingRanks = gameRanks[:actual_count]

    g['status'] = 'playing'
    g['state'] = {
        'players': players,
        'citizenDeck': citizenDeck,
        'stack': stack,
        'discardStack': discardStack,
        'trickOrder': trickOrder,
        'nextTrickOrder': copy.deepcopy(trickOrder),
        'mostRecentMove': None,
        'passedCounter': 0,
        'remainingRanks': remainingRanks,
        'currentTurn': trickOrder[0],
        'log': ['Spiel gestartet!'],
        'finished': False
    }

    # Auto-pass from the start if first player has no real moves
    advance_turn(g['state'], g)

    return jsonify({'ok': True})

@app.route('/api/game/<game_id>/state', methods=['GET'])
def get_state(game_id):
    player_id = request.args.get('player_id')
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404

    g = games[game_id]
    entity_id = next((p['entityID'] for p in g['players'] if p['id'] == player_id), None)

    if g['status'] == 'lobby':
        return jsonify({'status': 'lobby'})

    s = g['state']

    players_view = []
    for p in s['players']:
        players_view.append({
            'name': p['name'],
            'entityID': p['entityID'],
            'rank': p['rank'],
            'cardCount': len(p['hand']),
            'hand': p['hand'] if p['entityID'] == entity_id else []
        })

    my_moves = []
    if entity_id == s['currentTurn'] and not s['finished']:
        my_moves = possibleMoves(s['players'], entity_id, s['mostRecentMove'])

    return jsonify({
        'status': g['status'],
        'players': players_view,
        'stack': s['stack'],
        'mostRecentMove': s['mostRecentMove'],
        'currentTurn': s['currentTurn'],
        'myEntityID': entity_id,
        'myMoves': my_moves,
        'log': s['log'][-10:],
        'finished': s['finished']
    })

@app.route('/api/game/<game_id>/move', methods=['POST'])
def make_move(game_id):
    data = request.json
    player_id = data.get('player_id')
    move = data.get('move')

    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404

    g = games[game_id]
    s = g['state']

    entity_id = next((p['entityID'] for p in g['players'] if p['id'] == player_id), None)
    if entity_id is None:
        return jsonify({'error': 'Unbekannter Spieler'}), 403
    if entity_id != s['currentTurn']:
        return jsonify({'error': 'Nicht dein Zug'}), 400

    legal = possibleMoves(s['players'], entity_id, s['mostRecentMove'])
    if move not in legal:
        return jsonify({'error': 'Ungültiger Zug'}), 400

    trickOrder = s['trickOrder']
    remainingRanks = s['remainingRanks']
    player_name = s['players'][entity_id]['name']

    if move == 'pass':
        s['log'].append(f'{player_name} passt.')
        s['passedCounter'] += 1
    else:
        play(s['players'], entity_id, s['stack'], move)
        s['mostRecentMove'] = move
        s['passedCounter'] = 0
        s['log'].append(f'{player_name} spielt {move}.')

        # Check if player finished their hand
        if not s['players'][entity_id]['hand']:
            if remainingRanks:
                if getRank(move) == 'A':
                    s['players'][entity_id]['rank'] = remainingRanks[-1]
                    del remainingRanks[-1]
                else:
                    s['players'][entity_id]['rank'] = remainingRanks[0]
                    del remainingRanks[0]
                s['log'].append(f'{player_name} ist fertig → {s["players"][entity_id]["rank"]}!')

                if len(remainingRanks) == 1 and trickOrder:
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
                    return jsonify({'ok': True})

                if trickOrder:
                    next_player = trickOrder[0]
                    s['currentTurn'] = next_player
                    advance_turn(s, g)
                    return jsonify({'ok': True})

    # Round reset check
    active_count = len([e for e in trickOrder if s['players'][e]['rank'] is None])
    if s['passedCounter'] >= active_count:
        s['log'].append('Alle gepasst – neue Runde.')
        s['passedCounter'] = 0
        for card in s['stack']:
            s['discardStack'].append(card)
        s['mostRecentMove'] = None
        s['stack'].clear()
        cur_idx = trickOrder.index(entity_id) if entity_id in trickOrder else 0
        new_order = [trickOrder[(cur_idx + i) % len(trickOrder)] for i in range(len(trickOrder))]
        s['trickOrder'] = new_order
        s['nextTrickOrder'] = copy.deepcopy(new_order)
        s['currentTurn'] = new_order[0]
        advance_turn(s, g)
        return jsonify({'ok': True})

    # Advance to next player
    if entity_id in trickOrder:
        cur_idx = trickOrder.index(entity_id)
        s['currentTurn'] = trickOrder[(cur_idx + 1) % len(trickOrder)]

    advance_turn(s, g)
    return jsonify({'ok': True})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

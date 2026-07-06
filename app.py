# ─── app.py ───────────────────────────────────────────────────────────────────
import copy
import os
import uuid
import random

from flask import Flask, jsonify, request, render_template
from flask_cors import CORS

from SimulatorConfig import gameRanks
from SimulatorFunctions import (
    createDeck, shuffleDeck, dealHands, createEntities,
    possibleMoves, onlyPassAvailable, play, sortHand,
    advanceTurn, assignRank, getRank
)

app = Flask(__name__)
app.secret_key = 'arschloch-secret-key-2024'
CORS(app)

# ─── In-memory game store ─────────────────────────────────────────────────────
games = {}

# ─── Routes: pages ────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

# ─── Routes: lobby ────────────────────────────────────────────────────────────

@app.route('/api/lobby/create', methods=['POST'])
def create_lobby():
    data = request.json
    player_name = data.get('name', 'Player').strip()[:20]
    max_players  = max(2, min(5, int(data.get('max_players', 5))))
    deck_size    = data.get('deck_size', 'full')
    if deck_size not in ('full', 'half'):
        deck_size = 'full'

    game_id = str(uuid.uuid4())[:6].upper()
    games[game_id] = {
        'status':     'lobby',
        'players':    [{'id': str(uuid.uuid4()), 'name': player_name, 'entityID': 0}],
        'max_players': max_players,
        'deck_size':   deck_size,
        'state':       None,
    }
    return jsonify({'game_id': game_id, 'player_id': games[game_id]['players'][0]['id']})


@app.route('/api/lobby/join', methods=['POST'])
def join_lobby():
    data        = request.json
    game_id     = data.get('game_id', '').strip().upper()
    player_name = data.get('name', 'Player').strip()[:20]

    if game_id not in games:
        return jsonify({'error': 'Spiel nicht gefunden'}), 404
    g = games[game_id]
    if g['status'] != 'lobby':
        return jsonify({'error': 'Spiel läuft bereits'}), 400
    if len(g['players']) >= g['max_players']:
        return jsonify({'error': 'Lobby ist voll'}), 400

    player_id = str(uuid.uuid4())
    g['players'].append({'id': player_id, 'name': player_name, 'entityID': len(g['players'])})
    return jsonify({'game_id': game_id, 'player_id': player_id})


@app.route('/api/lobby/<game_id>', methods=['GET'])
def lobby_status(game_id):
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[game_id]
    return jsonify({
        'status':     g['status'],
        'players':    [{'name': p['name'], 'entityID': p['entityID']} for p in g['players']],
        'max_players': g['max_players'],
        'deck_size':   g['deck_size'],
    })


@app.route('/api/lobby/<game_id>/start', methods=['POST'])
def start_game(game_id):
    data      = request.json
    player_id = data.get('player_id')

    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[game_id]
    if g['players'][0]['id'] != player_id:
        return jsonify({'error': 'Nur der Host kann starten'}), 403
    if len(g['players']) < 2:
        return jsonify({'error': 'Mindestens 2 Spieler benötigt'}), 400

    names         = [p['name'] for p in g['players']]
    actual_count  = len(names)
    deck_size     = g['deck_size']

    citizenDeck = []; stack = []; discardStack = []
    deck = createDeck(deck_size)
    shuffleDeck(deck)
    players = createEntities(names)
    dealHands(deck, players, citizenDeck)

    # Sort every player's hand after dealing
    for p in players:
        p['hand'] = sortHand(p['hand'])

    trickOrder     = list(range(actual_count))
    random.shuffle(trickOrder)
    remainingRanks = gameRanks[:actual_count]

    g['status'] = 'playing'
    g['state']  = {
        'players':        players,
        'citizenDeck':    citizenDeck,
        'stack':          stack,
        'discardStack':   discardStack,
        'trickOrder':     trickOrder,
        'nextTrickOrder': copy.deepcopy(trickOrder),
        'mostRecentMove': None,
        'passedCounter':  0,
        'remainingRanks': remainingRanks,
        'currentTurn':    trickOrder[0],
        'log':            ['Spiel gestartet!'],
        'finished':       False,
        'deck_size':      deck_size,
    }

    advanceTurn(g['state'], g)
    return jsonify({'ok': True})

# ─── Routes: game ─────────────────────────────────────────────────────────────

@app.route('/api/game/<game_id>/state', methods=['GET'])
def get_state(game_id):
    player_id = request.args.get('player_id')
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404

    g         = games[game_id]
    entity_id = next((p['entityID'] for p in g['players'] if p['id'] == player_id), None)

    if g['status'] == 'lobby':
        return jsonify({'status': 'lobby'})

    s        = g['state']
    my_moves = []
    if entity_id == s['currentTurn'] and not s['finished']:
        my_moves = possibleMoves(s['players'], entity_id, s['mostRecentMove'])

    players_view = [{
        'name':      p['name'],
        'entityID':  p['entityID'],
        'rank':      p['rank'],
        'cardCount': len(p['hand']),
        'hand':      p['hand'] if p['entityID'] == entity_id else [],
    } for p in s['players']]

    return jsonify({
        'status':         g['status'],
        'players':        players_view,
        'stack':          s['stack'],
        'mostRecentMove': s['mostRecentMove'],
        'currentTurn':    s['currentTurn'],
        'myEntityID':     entity_id,
        'myMoves':        my_moves,
        'log':            s['log'][-10:],
        'finished':       s['finished'],
    })


@app.route('/api/game/<game_id>/move', methods=['POST'])
def make_move(game_id):
    data      = request.json
    player_id = data.get('player_id')
    move      = data.get('move')

    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404

    g         = games[game_id]
    s         = g['state']
    entity_id = next((p['entityID'] for p in g['players'] if p['id'] == player_id), None)

    if entity_id is None:
        return jsonify({'error': 'Unbekannter Spieler'}), 403
    if entity_id != s['currentTurn']:
        return jsonify({'error': 'Nicht dein Zug'}), 400

    legal = possibleMoves(s['players'], entity_id, s['mostRecentMove'])
    if move not in legal:
        return jsonify({'error': 'Ungültiger Zug'}), 400

    trickOrder  = s['trickOrder']
    player_name = s['players'][entity_id]['name']

    if move == 'pass':
        s['log'].append(f'{player_name} passt.')
        s['passedCounter'] += 1
    else:
        play(s['players'], entity_id, s['stack'], move)
        # Re-sort hand after playing
        s['players'][entity_id]['hand'] = sortHand(s['players'][entity_id]['hand'])
        s['mostRecentMove'] = move
        s['passedCounter']  = 0
        s['log'].append(f'{player_name} spielt {move}.')

        # Check if hand is empty → assign rank
        if not s['players'][entity_id]['hand']:
            game_over = assignRank(s, entity_id, move, trickOrder, g)
            if game_over:
                return jsonify({'ok': True})
            if trickOrder:
                s['currentTurn'] = trickOrder[0]
                advanceTurn(s, g)
            return jsonify({'ok': True})

    # Round reset if everyone passed
    active_count = len([e for e in trickOrder if s['players'][e]['rank'] is None])
    if s['passedCounter'] >= active_count:
        s['log'].append('Alle gepasst – neue Runde.')
        s['passedCounter'] = 0
        for card in s['stack']:
            s['discardStack'].append(card)
        s['mostRecentMove'] = None
        s['stack'].clear()
        cur_idx   = trickOrder.index(entity_id) if entity_id in trickOrder else 0
        new_order = [trickOrder[(cur_idx + i) % len(trickOrder)] for i in range(len(trickOrder))]
        s['trickOrder']     = new_order
        s['nextTrickOrder'] = copy.deepcopy(new_order)
        s['currentTurn']    = new_order[0]
        advanceTurn(s, g)
        return jsonify({'ok': True})

    # Advance to next player
    if entity_id in trickOrder:
        cur_idx        = trickOrder.index(entity_id)
        s['currentTurn'] = trickOrder[(cur_idx + 1) % len(trickOrder)]

    advanceTurn(s, g)
    return jsonify({'ok': True})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

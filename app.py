# ─── app.py ───────────────────────────────────────────────────────────────────
import copy, os, uuid, random, json
from datetime import datetime, timezone
from flask import Flask, jsonify, request, render_template_string
from flask_cors import CORS

from SimulatorConfig import gameRanks, LOG_PASSWORD, LOG_FILE
from SimulatorFunctions import (
    createDeck, shuffleDeck, dealHands, createEntities,
    possibleMoves, onlyPassAvailable, play, sortHand,
    advanceTurn, assignRank, buildTrades, executeTrade,
    pendingTradeFor, getRank, randomMove, _appendMoveLog
)

app = Flask(__name__)
app.secret_key = 'arschloch-secret-2024'
CORS(app)

games = {}   # game_id → game dict

# ── Logging ───────────────────────────────────────────────────────────────────

def appendGameLog(game_id, g):
    """Append a completed game's move log to the JSONL file."""
    s = g.get('state')
    if not s: return
    record = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "game_id": game_id,
        "round":   s.get('roundNumber', 1),
        "players": [{"id": p["entityID"], "name": p["name"],
                     "bot": p.get("isBot", False), "rank": p["rank"]}
                    for p in s['players']],
        "moves":   s.get('moveLog', []),
    }
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f"Log write error: {e}")

# ── Helpers ───────────────────────────────────────────────────────────────────

BOT_NAMES = ["Bot-Ada","Bot-Turing","Bot-Grace","Bot-Knuth","Bot-Lovelace"]

def _make_bot_slot(entity_id):
    name = BOT_NAMES[entity_id % len(BOT_NAMES)]
    return {"id": f"bot-{entity_id}", "name": name, "entityID": entity_id, "isBot": True}

def _init_round(g, round_number):
    """Set up a fresh round within an existing game."""
    names   = [p['name'] for p in g['players']]
    players = createEntities(names)
    for i, p in enumerate(g['players']):
        players[i]['isBot'] = p.get('isBot', False)

    citizenDeck = []; stack = []; discardStack = []
    deck = createDeck(); shuffleDeck(deck)
    dealHands(deck, players, citizenDeck)
    for p in players:
        p['hand'] = sortHand(p['hand'])

    trickOrder = list(range(5))
    random.shuffle(trickOrder)

    # Carry over ranks from previous round for trading
    prev_ranks = g.get('lastRanks', {})

    g['state'] = {
        'players':        players,
        'citizenDeck':    citizenDeck,
        'stack':          stack,
        'discardStack':   discardStack,
        'trickOrder':     trickOrder,
        'nextTrickOrder': trickOrder[:],
        'mostRecentMove': None,
        'passedCounter':  0,
        'remainingRanks': gameRanks[:5],
        'currentTurn':    trickOrder[0],
        'log':            [f'Runde {round_number} gestartet!'],
        'finished':       False,
        'roundNumber':    round_number,
        'trickCounter':   0,
        'moveLog':        [],
        'trades':         [],
        'tradeDone':      False,
        'prevRanks':      prev_ranks,
    }
    # If there were previous ranks, set up trading
    if prev_ranks:
        for p in players:
            p['rank'] = prev_ranks.get(p['entityID'])
        trades = buildTrades(players, gameRanks)
        g['state']['trades']  = trades
        g['state']['log']     = [f'Runde {round_number} – Trading läuft!']
        # Reset ranks for this round
        for p in players:
            p['rank'] = None
        g['status'] = 'trading'
    else:
        advanceTurn(g['state'], g)
        g['status'] = 'playing'

# ── Routes: pages ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'index.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}

# ── Routes: lobby ─────────────────────────────────────────────────────────────

@app.route('/api/lobby/create', methods=['POST'])
def create_lobby():
    data        = request.json
    player_name = data.get('name','Player').strip()[:20]
    game_id     = str(uuid.uuid4())[:6].upper()
    slots       = [None]*5
    pid         = str(uuid.uuid4())
    slots[0]    = {"id": pid, "name": player_name, "entityID": 0, "isBot": False}

    games[game_id] = {
        'status':      'lobby',
        'players':     slots,
        'state':       None,
        'roundNumber': 0,
        'lastRanks':   {},
    }
    return jsonify({'game_id': game_id, 'player_id': pid})


@app.route('/api/lobby/join', methods=['POST'])
def join_lobby():
    data        = request.json
    game_id     = data.get('game_id','').strip().upper()
    player_name = data.get('name','Player').strip()[:20]

    if game_id not in games:
        return jsonify({'error': 'Spiel nicht gefunden'}), 404
    g = games[game_id]
    if g['status'] != 'lobby':
        return jsonify({'error': 'Spiel läuft bereits'}), 400

    # Find first empty human slot
    slot_idx = next((i for i,s in enumerate(g['players']) if s is None or s.get('isBot')), None)
    if slot_idx is None:
        return jsonify({'error': 'Keine freien Plätze'}), 400

    pid = str(uuid.uuid4())
    g['players'][slot_idx] = {"id": pid, "name": player_name,
                               "entityID": slot_idx, "isBot": False}
    return jsonify({'game_id': game_id, 'player_id': pid})


@app.route('/api/lobby/<game_id>/bot', methods=['POST'])
def add_bot(game_id):
    """Add a bot to an empty slot (lobby or mid-game)."""
    data      = request.json
    player_id = data.get('player_id')
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[game_id]
    # Only host can add bots
    if g['players'][0] is None or g['players'][0].get('id') != player_id:
        return jsonify({'error': 'Nur Host'}), 403

    if g['status'] == 'lobby':
        idx = next((i for i,s in enumerate(g['players']) if s is None), None)
        if idx is None:
            return jsonify({'error': 'Keine freien Plätze'}), 400
        g['players'][idx] = _make_bot_slot(idx)
        return jsonify({'ok': True})

    if g['status'] in ('playing','trading'):
        # Add bot to the game state mid-game (takes over an empty/disconnected slot)
        s   = g['state']
        idx = next((i for i,p in enumerate(s['players']) if p.get('isBot') and p['rank'] is None
                    and not any(pp.get('id')==f'bot-{i}' for pp in g['players'] if pp)), None)
        return jsonify({'error': 'Kein freier Slot im laufenden Spiel'}), 400

    return jsonify({'error': 'Nicht möglich'}), 400


@app.route('/api/lobby/<game_id>/remove_bot', methods=['POST'])
def remove_bot(game_id):
    """Remove a bot slot and replace with empty (lobby only)."""
    data      = request.json
    player_id = data.get('player_id')
    slot_idx  = data.get('slot')
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[game_id]
    if g['players'][0].get('id') != player_id:
        return jsonify({'error': 'Nur Host'}), 403
    if g['status'] != 'lobby':
        return jsonify({'error': 'Nur in Lobby'}), 400
    if 0 <= slot_idx < 5 and g['players'][slot_idx] and g['players'][slot_idx].get('isBot'):
        g['players'][slot_idx] = None
    return jsonify({'ok': True})


@app.route('/api/lobby/<game_id>', methods=['GET'])
def lobby_status(game_id):
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[game_id]
    return jsonify({
        'status':  g['status'],
        'players': [{'name': p['name'], 'entityID': p['entityID'], 'isBot': p.get('isBot',False)}
                    if p else None for p in g['players']],
    })


@app.route('/api/lobby/<game_id>/start', methods=['POST'])
def start_game(game_id):
    data      = request.json
    player_id = data.get('player_id')
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[game_id]
    if not g['players'][0] or g['players'][0].get('id') != player_id:
        return jsonify({'error': 'Nur Host'}), 403

    # Fill remaining empty slots with bots
    for i,slot in enumerate(g['players']):
        if slot is None:
            g['players'][i] = _make_bot_slot(i)

    g['roundNumber'] = 1
    g['lastRanks']   = {}
    _init_round(g, 1)
    return jsonify({'ok': True})

# ── Routes: game state ────────────────────────────────────────────────────────

@app.route('/api/game/<game_id>/state', methods=['GET'])
def get_state(game_id):
    player_id = request.args.get('player_id')
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g         = games[game_id]
    entity_id = next((p['entityID'] for p in g['players'] if p and p.get('id')==player_id), None)

    if g['status'] == 'lobby':
        return jsonify({'status': 'lobby'})

    s        = g['state']
    my_moves = []
    if g['status'] == 'playing' and entity_id == s['currentTurn'] and not s['finished']:
        my_moves = possibleMoves(s['players'], entity_id, s['mostRecentMove'])

    my_trade = None
    if g['status'] == 'trading' and entity_id is not None:
        t = pendingTradeFor(s['players'], s['trades'], entity_id)
        if t:
            my_trade = t

    players_view = [{
        'name':      p['name'],
        'entityID':  p['entityID'],
        'rank':      p['rank'],
        'prevRank':  s['prevRanks'].get(p['entityID']),
        'cardCount': len(p['hand']),
        'hand':      p['hand'] if p['entityID'] == entity_id else [],
        'isBot':     p.get('isBot', False),
    } for p in s['players']]

    return jsonify({
        'status':         g['status'],
        'roundNumber':    s.get('roundNumber', 1),
        'players':        players_view,
        'stack':          s['stack'],
        'mostRecentMove': s['mostRecentMove'],
        'currentTurn':    s['currentTurn'],
        'myEntityID':     entity_id,
        'myMoves':        my_moves,
        'myTrade':        my_trade,
        'log':            s['log'][-12:],
        'finished':       s['finished'],
    })

# ── Routes: game move ─────────────────────────────────────────────────────────

@app.route('/api/game/<game_id>/move', methods=['POST'])
def make_move(game_id):
    data      = request.json
    player_id = data.get('player_id')
    move      = data.get('move')
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g         = games[game_id]
    s         = g['state']
    entity_id = next((p['entityID'] for p in g['players'] if p and p.get('id')==player_id), None)

    if entity_id is None:          return jsonify({'error': 'Unbekannter Spieler'}), 403
    if g['status'] != 'playing':   return jsonify({'error': 'Nicht in Spielphase'}), 400
    if entity_id != s['currentTurn']: return jsonify({'error': 'Nicht dein Zug'}), 400

    legal = possibleMoves(s['players'], entity_id, s['mostRecentMove'])
    if move not in legal:          return jsonify({'error': 'Ungültiger Zug'}), 400

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
        s['trickCounter']   = s.get('trickCounter', 0) + 1
        s['log'].append(f'{name} spielt {move}.')
        _appendMoveLog(s, entity_id, name, move)

        if not s['players'][entity_id]['hand']:
            done = assignRank(s, g, entity_id, move, trickOrder)
            if done:
                # Save round log
                appendGameLog(game_id, g)
                # Store ranks for next round
                g['lastRanks'] = {p['entityID']: p['rank'] for p in s['players']}
                return jsonify({'ok': True})
            if trickOrder:
                s['currentTurn'] = trickOrder[0]
                advanceTurn(s, g)
            return jsonify({'ok': True})

    active = len([e for e in trickOrder if s['players'][e]['rank'] is None])
    if s['passedCounter'] >= active:
        s['log'].append('Alle gepasst – neue Runde.')
        s['passedCounter'] = 0
        for card in s['stack']:
            s['discardStack'].append(card)
        s['mostRecentMove'] = None
        s['stack'].clear()
        cur_idx   = trickOrder.index(entity_id) if entity_id in trickOrder else 0
        new_order = [trickOrder[(cur_idx+i)%len(trickOrder)] for i in range(len(trickOrder))]
        s['trickOrder']     = new_order
        s['nextTrickOrder'] = new_order[:]
        s['currentTurn']    = new_order[0]
        advanceTurn(s, g)
        return jsonify({'ok': True})

    if entity_id in trickOrder:
        idx = trickOrder.index(entity_id)
        s['currentTurn'] = trickOrder[(idx+1)%len(trickOrder)]
    advanceTurn(s, g)
    return jsonify({'ok': True})

# ── Routes: trading ───────────────────────────────────────────────────────────

@app.route('/api/game/<game_id>/trade', methods=['POST'])
def submit_trade(game_id):
    """Player submits the cards they want to give in their trade."""
    data      = request.json
    player_id = data.get('player_id')
    cards     = data.get('cards', [])   # list of card strings

    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g         = games[game_id]
    s         = g['state']
    entity_id = next((p['entityID'] for p in g['players'] if p and p.get('id')==player_id), None)

    if entity_id is None:         return jsonify({'error': 'Unbekannter Spieler'}), 403
    if g['status'] != 'trading':  return jsonify({'error': 'Nicht in Trading-Phase'}), 400

    trade = pendingTradeFor(s['players'], s['trades'], entity_id)
    if not trade:
        return jsonify({'error': 'Kein Trade für dich'}), 400
    if len(cards) != trade['count']:
        return jsonify({'error': f'Bitte genau {trade["count"]} Karte(n) auswählen'}), 400

    hand = s['players'][entity_id]['hand']
    for c in cards:
        if c not in hand:
            return jsonify({'error': f'Karte {c} nicht in deiner Hand'}), 400

    # Execute the trade
    for c in cards:
        hand.remove(c)
        s['players'][trade['to']]['hand'].append(c)
    for p in s['players']:
        p['hand'] = sortHand(p['hand'])

    trade['done']  = True
    s['log'].append(f'{s["players"][entity_id]["name"]} tauscht {len(cards)} Karte(n).')

    # Check if all trades done → start playing
    if all(t.get('done') for t in s['trades']):
        _finalize_trading(g)

    return jsonify({'ok': True})


def _finalize_trading(g):
    s = g['state']
    # Clear ranks, reset trickOrder
    for p in s['players']:
        p['rank'] = None
    trickOrder = list(range(5))
    random.shuffle(trickOrder)
    s['trickOrder']     = trickOrder
    s['nextTrickOrder'] = trickOrder[:]
    s['currentTurn']    = trickOrder[0]
    s['mostRecentMove'] = None
    s['passedCounter']  = 0
    s['remainingRanks'] = gameRanks[:5]
    s['stack'].clear()
    s['discardStack'].clear()
    s['log'].append('Trading abgeschlossen – Spiel startet!')
    g['status'] = 'playing'
    advanceTurn(s, g)


@app.route('/api/game/<game_id>/next_round', methods=['POST'])
def next_round(game_id):
    """Host starts the next round."""
    data      = request.json
    player_id = data.get('player_id')
    if game_id not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[game_id]
    if not g['players'][0] or g['players'][0].get('id') != player_id:
        return jsonify({'error': 'Nur Host'}), 403

    g['roundNumber'] += 1
    _init_round(g, g['roundNumber'])

    # Auto-execute bot trades in trading phase
    if g['status'] == 'trading':
        _auto_bot_trades(g)

    return jsonify({'ok': True})


def _auto_bot_trades(g):
    s = g['state']
    for trade in s['trades']:
        if trade.get('done'): continue
        eid = trade['from']
        if s['players'][eid].get('isBot'):
            hand  = sortHand(s['players'][eid]['hand'])
            count = trade['count']
            cards = hand[-count:] if trade['direction'] == 'give_best' else hand[:count]
            for c in cards:
                s['players'][eid]['hand'].remove(c)
                s['players'][trade['to']]['hand'].append(c)
            for p in s['players']:
                p['hand'] = sortHand(p['hand'])
            trade['done'] = True
            s['log'].append(f'{s["players"][eid]["name"]} tauscht automatisch.')
    if all(t.get('done') for t in s['trades']):
        _finalize_trading(g)

# ── Routes: logs ──────────────────────────────────────────────────────────────

@app.route('/api/logs', methods=['GET'])
def get_logs():
    pw = request.args.get('password','')
    if pw != LOG_PASSWORD:
        return jsonify({'error': 'Falsches Passwort'}), 403
    try:
        records = []
        with open(LOG_FILE, 'r') as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        return jsonify({'logs': records})
    except FileNotFoundError:
        return jsonify({'logs': []})

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

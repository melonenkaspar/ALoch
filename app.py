# ─── app.py ───────────────────────────────────────────────────────────────────
import copy, os, uuid, random, json
from datetime import datetime, timezone
from flask import Flask, jsonify, request
from flask_cors import CORS

from SimulatorConfig import gameRanks, LOG_PASSWORD
from SimulatorFunctions import (
    createDeck, shuffleDeck, dealHands, createEntities,
    possibleMoves, onlyPassAvailable, play, sortHand,
    buildTrades, pendingTradeFor, getRank, randomMove, _appendMoveLog
)

app = Flask(__name__)
app.secret_key = 'arschloch-secret-2024'
CORS(app)

games = {}        # game_id → game dict
game_logs = []    # in-memory log, also written to /tmp/game_logs.jsonl
LOG_FILE = '/tmp/game_logs.jsonl'

# ── Logging ───────────────────────────────────────────────────────────────────

def save_round_log(game_id, g):
    s = g.get('state')
    if not s: return
    record = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "game_id": game_id,
        "round":   s.get('roundNumber', 1),
        "players": [{"id": p["entityID"], "name": p["name"],
                     "bot": p.get("isBot", False), "rank": p["rank"]}
                    for p in s['players']],
        "moves": s.get('moveLog', []),
    }
    game_logs.append(record)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f"Log write error: {e}")

# ── Bot helpers ───────────────────────────────────────────────────────────────

BOT_NAMES = ["Bot-Ada", "Bot-Turing", "Bot-Grace", "Bot-Knuth", "Bot-Lovelace"]

def _make_bot_slot(entity_id):
    return {"id": f"bot-{entity_id}", "name": BOT_NAMES[entity_id % len(BOT_NAMES)],
            "entityID": entity_id, "isBot": True}

def _queue_next_bot_move(g):
    """If the current player is a bot, queue their move for the next poll."""
    s = g['state']
    if g['status'] != 'playing' or s['finished']:
        return
    current = s['currentTurn']
    if not s['players'][current].get('isBot'):
        return
    # Calculate and store the move, but don't execute yet
    moves = possibleMoves(s['players'], current, s['mostRecentMove'])
    # Auto-pass if only option
    if moves == ['pass']:
        g['pendingBotMove'] = {'entity_id': current, 'move': 'pass'}
    else:
        real = [m for m in moves if m != 'pass']
        g['pendingBotMove'] = {'entity_id': current, 'move': random.choice(real)}

def _execute_pending_bot_move(game_id, g):
    """Execute the queued bot move. Called on next poll."""
    pending = g.pop('pendingBotMove', None)
    if not pending:
        return
    s   = g['state']
    eid = pending['entity_id']
    mv  = pending['move']

    # Safety: still that bot's turn?
    if s['currentTurn'] != eid or not s['players'][eid].get('isBot'):
        return

    name       = s['players'][eid]['name']
    trickOrder = s['trickOrder']

    if mv == 'pass':
        s['log'].append(f'🤖 {name} passt.')
        _appendMoveLog(s, eid, name, 'pass')
        s['passedCounter'] += 1
        s['lastBotMove'] = {'name': name, 'move': 'pass', 'stack': list(s['stack'])}
    else:
        play(s['players'], eid, s['stack'], mv)
        s['players'][eid]['hand'] = sortHand(s['players'][eid]['hand'])
        s['mostRecentMove'] = mv
        s['passedCounter']  = 0
        s['trickCounter']   = s.get('trickCounter', 0) + 1
        s['log'].append(f'🤖 {name} spielt {mv}.')
        s['lastBotMove'] = {'name': name, 'move': mv, 'stack': list(s['stack'])}
        _appendMoveLog(s, eid, name, mv)

        if not s['players'][eid]['hand']:
            _assign_rank(game_id, g, eid, mv)
            return

    # Check round reset
    trickOrder = s['trickOrder']
    active = len([e for e in trickOrder if s['players'][e]['rank'] is None])
    if active == 0:
        return
    if s['passedCounter'] >= active:
        _reset_round(s, eid, trickOrder)
        _queue_next_bot_move(g)
        return

    # Advance turn
    if eid in trickOrder:
        idx = trickOrder.index(eid)
        s['currentTurn'] = trickOrder[(idx + 1) % len(trickOrder)]
    elif trickOrder:
        s['currentTurn'] = trickOrder[0]

    _queue_next_bot_move(g)

def _assign_rank(game_id, g, entity_id, move):
    s          = g['state']
    remaining  = s['remainingRanks']
    trickOrder = s['trickOrder']
    name       = s['players'][entity_id]['name']

    if not remaining: return
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

    if entity_id in trickOrder:              trickOrder.remove(entity_id)
    if entity_id in s.get('nextTrickOrder',[]): s['nextTrickOrder'].remove(entity_id)

    if not remaining or len(trickOrder) <= 1:
        # Assign last rank if someone remains
        if trickOrder:
            last = next((e for e in trickOrder if s['players'][e]['rank'] is None), None)
            if last is not None and remaining:
                s['players'][last]['rank'] = remaining[0]; del remaining[0]
                s['log'].append(f'{s["players"][last]["name"]} → {s["players"][last]["rank"]}!')
        # Assign any truly last remaining player
        if trickOrder:
            truly_last = next((e for e in trickOrder if s['players'][e]['rank'] is None), None)
            if truly_last is not None and remaining:
                s['players'][truly_last]['rank'] = remaining[0]; del remaining[0]
                s['log'].append(f'{s["players"][truly_last]["name"]} → {s["players"][truly_last]["rank"]}!')
        s['finished'] = True
        s['log'].append('🎉 Runde beendet!')
        g['status'] = 'trading'
        g.pop('pendingBotMove', None)
        g['lastRanks'] = {p['entityID']: p['rank'] for p in s['players']}
        save_round_log(game_id, g)
        return

    if trickOrder:
        s['currentTurn'] = trickOrder[0]
    _queue_next_bot_move(g)

def _reset_round(s, trigger, trickOrder):
    s['log'].append('Alle gepasst – neue Runde.')
    s['passedCounter'] = 0
    s['discardStack'].extend(s['stack'])
    s['mostRecentMove'] = None
    s['stack'].clear()
    cur_idx = trickOrder.index(trigger) if trigger in trickOrder else 0
    new_order = [trickOrder[(cur_idx + i) % len(trickOrder)] for i in range(len(trickOrder))]
    s['trickOrder'] = new_order
    s['nextTrickOrder'] = new_order[:]
    s['currentTurn'] = new_order[0]

def _auto_pass_humans(g):
    """Auto-pass any human who has no real moves (only 'pass' available)."""
    s = g['state']
    if g['status'] != 'playing' or s['finished']:
        return
    current = s['currentTurn']
    if s['players'][current].get('isBot'):
        return
    if onlyPassAvailable(s['players'], current, s['mostRecentMove']):
        name = s['players'][current]['name']
        s['log'].append(f'{name} passt automatisch.')
        _appendMoveLog(s, current, name, 'pass', auto=True)
        s['passedCounter'] += 1
        trickOrder = s['trickOrder']
        active = len([e for e in trickOrder if s['players'][e]['rank'] is None])
        if s['passedCounter'] >= active:
            _reset_round(s, current, trickOrder)
        elif current in trickOrder:
            idx = trickOrder.index(current)
            s['currentTurn'] = trickOrder[(idx + 1) % len(trickOrder)]
        _queue_next_bot_move(g)

# ── Round init ────────────────────────────────────────────────────────────────

def _init_round(game_id, g, round_number):
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
        'lastBotMove':    None,
    }
    g.pop('pendingBotMove', None)

    if prev_ranks:
        for p in players:
            p['rank'] = prev_ranks.get(p['entityID'])
        trades = buildTrades(players, gameRanks)
        g['state']['trades'] = trades
        g['state']['log'] = [f'Runde {round_number} – Trading läuft!']
        for p in players:
            p['rank'] = None
        g['status'] = 'trading'
        _auto_bot_trades(g)
    else:
        g['status'] = 'playing'
        _auto_pass_humans(g)
        _queue_next_bot_move(g)

def _auto_bot_trades(g):
    s = g['state']
    for trade in s['trades']:
        if trade.get('done'): continue
        eid = trade['from']
        if not s['players'][eid].get('isBot'): continue
        hand  = sortHand(s['players'][eid]['hand'])
        cards = hand[-trade['count']:] if trade['direction'] == 'give_best' else hand[:trade['count']]
        for c in cards:
            s['players'][eid]['hand'].remove(c)
            s['players'][trade['to']]['hand'].append(c)
        for p in s['players']:
            p['hand'] = sortHand(p['hand'])
        trade['done'] = True
        s['log'].append(f'🤖 {s["players"][eid]["name"]} tauscht automatisch.')
    if all(t.get('done') for t in s['trades']):
        _finalize_trading(g)

def _finalize_trading(g):
    s = g['state']
    for p in s['players']:
        p['rank'] = None
    trickOrder = list(range(5))
    random.shuffle(trickOrder)
    s.update({
        'trickOrder':     trickOrder,
        'nextTrickOrder': trickOrder[:],
        'currentTurn':    trickOrder[0],
        'mostRecentMove': None,
        'passedCounter':  0,
        'remainingRanks': gameRanks[:5],
        'lastBotMove':    None,
    })
    s['stack'].clear(); s['discardStack'].clear()
    s['log'].append('Trading abgeschlossen – Spiel startet!')
    g['status'] = 'playing'
    g.pop('pendingBotMove', None)
    _auto_pass_humans(g)
    _queue_next_bot_move(g)

# ── Routes: page ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'index.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}

# ── Routes: lobby ─────────────────────────────────────────────────────────────

@app.route('/api/lobby/create', methods=['POST'])
def create_lobby():
    data = request.json
    name = data.get('name', 'Player').strip()[:20]
    gid  = str(uuid.uuid4())[:6].upper()
    pid  = str(uuid.uuid4())
    games[gid] = {
        'status':      'lobby',
        'players':     [{"id": pid, "name": name, "entityID": 0, "isBot": False}] + [None]*4,
        'state':       None,
        'roundNumber': 0,
        'lastRanks':   {},
    }
    return jsonify({'game_id': gid, 'player_id': pid})

@app.route('/api/lobby/join', methods=['POST'])
def join_lobby():
    data = request.json
    gid  = data.get('game_id', '').strip().upper()
    name = data.get('name', 'Player').strip()[:20]
    if gid not in games: return jsonify({'error': 'Spiel nicht gefunden'}), 404
    g = games[gid]
    if g['status'] != 'lobby': return jsonify({'error': 'Spiel läuft bereits'}), 400
    idx = next((i for i,s in enumerate(g['players']) if s is None or s.get('isBot')), None)
    if idx is None: return jsonify({'error': 'Keine freien Plätze'}), 400
    pid = str(uuid.uuid4())
    g['players'][idx] = {"id": pid, "name": name, "entityID": idx, "isBot": False}
    return jsonify({'game_id': gid, 'player_id': pid})

@app.route('/api/lobby/<gid>/bot', methods=['POST'])
def add_bot(gid):
    data = request.json
    if gid not in games: return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not g['players'][0] or g['players'][0].get('id') != data.get('player_id'):
        return jsonify({'error': 'Nur Host'}), 403
    if g['status'] != 'lobby': return jsonify({'error': 'Nur in Lobby'}), 400
    idx = next((i for i,s in enumerate(g['players']) if s is None), None)
    if idx is None: return jsonify({'error': 'Keine freien Plätze'}), 400
    g['players'][idx] = _make_bot_slot(idx)
    return jsonify({'ok': True})

@app.route('/api/lobby/<gid>/remove_bot', methods=['POST'])
def remove_bot(gid):
    data = request.json
    if gid not in games: return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not g['players'][0] or g['players'][0].get('id') != data.get('player_id'):
        return jsonify({'error': 'Nur Host'}), 403
    idx = data.get('slot')
    if isinstance(idx, int) and 0 <= idx < 5 and g['players'][idx] and g['players'][idx].get('isBot'):
        g['players'][idx] = None
    return jsonify({'ok': True})

@app.route('/api/lobby/<gid>', methods=['GET'])
def lobby_status(gid):
    if gid not in games: return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    return jsonify({
        'status':  g['status'],
        'players': [{'name': p['name'], 'entityID': p['entityID'], 'isBot': p.get('isBot', False)}
                    if p else None for p in g['players']],
    })

@app.route('/api/lobby/<gid>/start', methods=['POST'])
def start_game(gid):
    data = request.json
    if gid not in games: return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not g['players'][0] or g['players'][0].get('id') != data.get('player_id'):
        return jsonify({'error': 'Nur Host'}), 403
    for i, slot in enumerate(g['players']):
        if slot is None:
            g['players'][i] = _make_bot_slot(i)
    g['roundNumber'] = 1
    g['lastRanks']   = {}
    _init_round(gid, g, 1)
    return jsonify({'ok': True})

# ── Routes: game state ────────────────────────────────────────────────────────

@app.route('/api/game/<gid>/state', methods=['GET'])
def get_state(gid):
    player_id = request.args.get('player_id')
    if gid not in games: return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]

    if g['status'] == 'lobby':
        return jsonify({'status': 'lobby'})

    # Execute pending bot move on each poll
    if 'pendingBotMove' in g:
        _execute_pending_bot_move(gid, g)

    entity_id = next((p['entityID'] for p in g['players'] if p and p.get('id') == player_id), None)
    s = g['state']

    my_moves = []
    if g['status'] == 'playing' and entity_id == s['currentTurn'] and not s['finished']:
        my_moves = possibleMoves(s['players'], entity_id, s['mostRecentMove'])

    my_trade = None
    if g['status'] == 'trading' and entity_id is not None:
        t = pendingTradeFor(s['players'], s['trades'], entity_id)
        if t and not t.get('done'):
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

    # Is there still a bot move queued after this one?
    next_is_bot = 'pendingBotMove' in g

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
        'log':            s['log'][-15:],
        'finished':       s['finished'],
        'lastBotMove':    s.get('lastBotMove'),
        'nextIsBot':      next_is_bot,
    })

# ── Routes: game move ─────────────────────────────────────────────────────────

@app.route('/api/game/<gid>/move', methods=['POST'])
def make_move(gid):
    data = request.json
    player_id = data.get('player_id')
    move      = data.get('move')
    if gid not in games: return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    s = g['state']
    entity_id = next((p['entityID'] for p in g['players'] if p and p.get('id') == player_id), None)

    if entity_id is None:                return jsonify({'error': 'Unbekannter Spieler'}), 403
    if g['status'] != 'playing':         return jsonify({'error': 'Nicht in Spielphase'}), 400
    if entity_id != s['currentTurn']:    return jsonify({'error': 'Nicht dein Zug'}), 400

    legal = possibleMoves(s['players'], entity_id, s['mostRecentMove'])
    if move not in legal:                return jsonify({'error': 'Ungültiger Zug'}), 400

    trickOrder = s['trickOrder']
    name       = s['players'][entity_id]['name']
    s['lastBotMove'] = None  # clear last bot move on human turn

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
            _assign_rank(gid, g, entity_id, move)
            return jsonify({'ok': True})

    trickOrder = s['trickOrder']
    active = len([e for e in trickOrder if s['players'][e]['rank'] is None])
    if s['passedCounter'] >= active:
        _reset_round(s, entity_id, trickOrder)
        _auto_pass_humans(g)
        _queue_next_bot_move(g)
        return jsonify({'ok': True})

    if entity_id in trickOrder:
        idx = trickOrder.index(entity_id)
        s['currentTurn'] = trickOrder[(idx + 1) % len(trickOrder)]
    elif trickOrder:
        s['currentTurn'] = trickOrder[0]

    _auto_pass_humans(g)
    _queue_next_bot_move(g)
    return jsonify({'ok': True})

# ── Routes: trading ───────────────────────────────────────────────────────────

@app.route('/api/game/<gid>/trade', methods=['POST'])
def submit_trade(gid):
    data = request.json
    player_id = data.get('player_id')
    cards     = data.get('cards', [])
    if gid not in games: return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    s = g['state']
    entity_id = next((p['entityID'] for p in g['players'] if p and p.get('id') == player_id), None)

    if entity_id is None:        return jsonify({'error': 'Unbekannter Spieler'}), 403
    if g['status'] != 'trading': return jsonify({'error': 'Nicht in Trading-Phase'}), 400

    trade = pendingTradeFor(s['players'], s['trades'], entity_id)
    if not trade:                return jsonify({'error': 'Kein Trade für dich'}), 400
    if len(cards) != trade['count']:
        return jsonify({'error': f'Bitte genau {trade["count"]} Karte(n) wählen'}), 400

    hand = s['players'][entity_id]['hand']
    for c in cards:
        if c not in hand: return jsonify({'error': f'Karte {c} nicht in deiner Hand'}), 400

    for c in cards:
        hand.remove(c)
        s['players'][trade['to']]['hand'].append(c)
    for p in s['players']:
        p['hand'] = sortHand(p['hand'])
    trade['done'] = True
    s['log'].append(f'{s["players"][entity_id]["name"]} tauscht {len(cards)} Karte(n).')

    if all(t.get('done') for t in s['trades']):
        _finalize_trading(g)
    return jsonify({'ok': True})

@app.route('/api/game/<gid>/next_round', methods=['POST'])
def next_round(gid):
    data = request.json
    if gid not in games: return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not g['players'][0] or g['players'][0].get('id') != data.get('player_id'):
        return jsonify({'error': 'Nur Host'}), 403
    g['roundNumber'] += 1
    _init_round(gid, g, g['roundNumber'])
    return jsonify({'ok': True})

# ── Routes: logs ──────────────────────────────────────────────────────────────

@app.route('/api/logs', methods=['GET'])
def get_logs():
    if request.args.get('password', '') != LOG_PASSWORD:
        return jsonify({'error': 'Falsches Passwort'}), 403
    # Try reading from file first, fall back to RAM
    records = list(game_logs)  # always start from RAM
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            file_records = []
            seen_keys = {(r['game_id'], r['round']) for r in records}
            for line in f:
                line = line.strip()
                if line:
                    rec = json.loads(line)
                    key = (rec['game_id'], rec['round'])
                    if key not in seen_keys:
                        file_records.append(rec)
            records = file_records + records
    except FileNotFoundError:
        pass
    return jsonify({'logs': sorted(records, key=lambda r: r['ts'])})

# ── Run ───────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

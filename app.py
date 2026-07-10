# ─── app.py ───────────────────────────────────────────────────────────────────
import os, uuid, random, json, time, csv, io
from datetime import datetime, timezone
from flask import Flask, jsonify, request, Response
from flask_cors import CORS

from SimulatorConfig import gameRanks, LOG_PASSWORD, rankOrder, DEFAULT_BOT_DELAY_MS
from SimulatorFunctions import (
    createDeck, shuffleDeck, dealHands, createEntities,
    possibleMoves, onlyPassAvailable, play, sortHand,
    buildTrades, pendingWishFor, pendingGiveFor, pendingReturnFor,
    allTradesDone, autoBotTrade, resolveReturn, getRank, randomMove,
    _appendMoveLog
)

app = Flask(__name__)
app.secret_key = 'arschloch-secret-2024'
CORS(app)

games     = {}   # game_id (= Lobby-Code) → game dict
game_logs = []   # In-Memory-Log, zusaetzlich als Datei
LOG_FILE  = '/tmp/game_logs.jsonl'

BOT_NAMES = ["Bot-Ada", "Bot-Turing", "Bot-Grace", "Bot-Knuth", "Bot-Lovelace"]


# ══ Logging ══════════════════════════════════════════════════════════════════

def save_round_log(game_id, g):
    s = g.get('state')
    if not s:
        return
    record = {
        "ts":         datetime.now(timezone.utc).isoformat(),
        "lobby_code": game_id,          # ← explizit, um Games zuzuordnen
        "game_id":    game_id,
        "round":      s.get('roundNumber', 1),
        "players": [{
            "id":           p["entityID"],
            "name":         p["name"],
            "bot":          p.get("isBot", False),
            "rank":         p["rank"],
            "starter_hand": p.get("starterHand", []),
        } for p in s['players']],
        "moves": s.get('moveLog', []),
    }
    game_logs.append(record)
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    except Exception as e:
        print(f"Log write error: {e}")


def _all_log_records():
    records  = list(game_logs)
    seen     = {(r.get('game_id'), r.get('round')) for r in records}
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            extra = []
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                key = (rec.get('game_id'), rec.get('round'))
                if key not in seen:
                    extra.append(rec)
                    seen.add(key)
            records = extra + records
    except FileNotFoundError:
        pass
    return sorted(records, key=lambda r: r.get('ts', ''))


# ══ Helpers ══════════════════════════════════════════════════════════════════

def _make_bot_slot(entity_id):
    return {"id": f"bot-{entity_id}", "name": BOT_NAMES[entity_id % len(BOT_NAMES)],
            "entityID": entity_id, "isBot": True}


def _is_host(g, player_id):
    return bool(player_id) and g.get('host') == player_id


def _entity_of(g, player_id):
    return next((p['entityID'] for p in g['players'] if p and p.get('id') == player_id), None)


def _bot_delay(g):
    return int(g.get('settings', {}).get('botDelayMs', DEFAULT_BOT_DELAY_MS))


# ══ Kern: ein Zug ════════════════════════════════════════════════════════════

def _do_move(game_id, g, eid, move, auto=False):
    """Fuehrt genau einen Zug aus (Mensch oder Bot) und ruecken den Turn weiter."""
    s      = g['state']
    p      = s['players'][eid]
    name   = p['name']
    is_bot = p.get('isBot', False)
    pref   = '🤖 ' if is_bot else ''

    # Nur ein *bewusster* Menschenzug loescht das Bot-Banner.
    if not is_bot and not auto:
        s['lastBotMove'] = None

    if move == 'pass':
        s['log'].append(f'{pref}{name} passt{" automatisch" if auto else ""}.')
        _appendMoveLog(s, eid, name, 'pass', auto=auto)
        s['passedCounter'] += 1
        if is_bot:
            s['botMoveSeq'] += 1
            s['lastBotMove'] = {'seq': s['botMoveSeq'], 'name': name,
                                'move': 'pass', 'cards': []}
    else:
        before = len(s['stack'])
        play(s['players'], eid, s['stack'], move)
        p['hand'] = sortHand(p['hand'])
        cards = list(s['stack'][before:])

        s['mostRecentMove'] = move
        s['passedCounter']  = 0
        s['trickCounter']   = s.get('trickCounter', 0) + 1
        s['log'].append(f'{pref}{name} spielt {move}.')
        _appendMoveLog(s, eid, name, move)
        if is_bot:
            s['botMoveSeq'] += 1
            s['lastBotMove'] = {'seq': s['botMoveSeq'], 'name': name,
                                'move': move, 'cards': cards}

        if not p['hand']:
            _assign_rank(game_id, g, eid, move)
            return

    _after_action(g, eid)


def _after_action(g, eid):
    """Trick-Reset pruefen bzw. Turn weiterruecken."""
    s          = g['state']
    trickOrder = s['trickOrder']
    if not trickOrder:
        return
    active = len(trickOrder)
    if s['passedCounter'] >= active:
        _reset_trick(s, eid, trickOrder)
        return
    if eid in trickOrder:
        idx = trickOrder.index(eid)
        s['currentTurn'] = trickOrder[(idx + 1) % len(trickOrder)]
    else:
        s['currentTurn'] = trickOrder[0]


def _reset_trick(s, trigger, trickOrder):
    s['log'].append('Alle gepasst – neuer Stich.')
    s['passedCounter'] = 0
    s['discardStack'].extend(s['stack'])
    s['stack'].clear()
    s['mostRecentMove'] = None
    cur_idx   = trickOrder.index(trigger) if trigger in trickOrder else 0
    new_order = [trickOrder[(cur_idx + i) % len(trickOrder)] for i in range(len(trickOrder))]
    s['trickOrder']     = new_order
    s['nextTrickOrder'] = new_order[:]
    s['currentTurn']    = new_order[0]


def _assign_rank(game_id, g, eid, move):
    s          = g['state']
    remaining  = s['remainingRanks']
    trickOrder = s['trickOrder']
    name       = s['players'][eid]['name']
    if not remaining:
        return

    # Mit Ass rausgehen = schlechtester noch freier Rang
    rank = remaining.pop() if getRank(move) == 'A' else remaining.pop(0)
    s['players'][eid]['rank'] = rank
    s['log'].append(f'{name} fertig → {rank}!')

    idx = trickOrder.index(eid) if eid in trickOrder else 0
    if eid in trickOrder:
        trickOrder.remove(eid)
    if eid in s.get('nextTrickOrder', []):
        s['nextTrickOrder'].remove(eid)

    # Nur noch einer (oder keiner) uebrig → Runde vorbei
    if len(trickOrder) <= 1:
        if trickOrder and remaining:
            last = trickOrder[0]
            s['players'][last]['rank'] = remaining.pop(0)
            s['log'].append(f'{s["players"][last]["name"]} letzter → {s["players"][last]["rank"]}!')
        _end_round(game_id, g)
        return

    s['currentTurn'] = trickOrder[idx % len(trickOrder)]


def _end_round(game_id, g):
    s = g['state']
    s['finished'] = True
    s['log'].append('🎉 Runde beendet!')
    g['status'] = 'finished'
    g.pop('pendingBotMove', None)
    g['lastRanks'] = {p['entityID']: p['rank'] for p in s['players']}
    save_round_log(game_id, g)


# ══ Settle-Loop: Auto-Pass + Bot-Zuege ═══════════════════════════════════════

def _do_bot_move(game_id, g, eid):
    s     = g['state']
    moves = possibleMoves(s['players'], eid, s['mostRecentMove'])
    _do_move(game_id, g, eid, randomMove(moves))


def _settle(game_id, g):
    """
    Bringt das Spiel in einen Zustand, in dem ein Mensch mit echten Zuegen dran ist.
    - Menschen, die nur passen koennen, passen automatisch (auch nach Bot-Zuegen!)
    - Bots ziehen sofort (botDelayMs == 0) oder werden fuer den naechsten Poll gequeued.
    """
    for _ in range(300):
        if g['status'] != 'playing':
            g.pop('pendingBotMove', None)
            return
        s = g['state']
        if s['finished'] or not s['trickOrder']:
            g.pop('pendingBotMove', None)
            return

        cur = s['currentTurn']
        if cur not in s['trickOrder']:
            s['currentTurn'] = s['trickOrder'][0]
            continue

        if s['players'][cur].get('isBot'):
            delay = _bot_delay(g)
            if delay > 0:
                g['pendingBotMove'] = {'eid': cur, 'ready_at': time.monotonic() + delay / 1000.0}
                return
            g.pop('pendingBotMove', None)
            _do_bot_move(game_id, g, cur)
            continue

        # Mensch
        if onlyPassAvailable(s['players'], cur, s['mostRecentMove']):
            _do_move(game_id, g, cur, 'pass', auto=True)
            continue

        g.pop('pendingBotMove', None)
        return


def _tick_bots(game_id, g):
    """Wird bei jedem State-Poll aufgerufen: faelligen Bot-Zug ausfuehren."""
    pending = g.get('pendingBotMove')
    if not pending or time.monotonic() < pending['ready_at']:
        return
    g.pop('pendingBotMove', None)
    s   = g['state']
    eid = pending['eid']
    if (g['status'] != 'playing' or s['finished']
            or s['currentTurn'] != eid or not s['players'][eid].get('isBot')):
        _settle(game_id, g)
        return
    _do_bot_move(game_id, g, eid)
    _settle(game_id, g)


# ══ Runde initialisieren ═════════════════════════════════════════════════════

def _init_round(game_id, g, round_number):
    names   = [p['name'] for p in g['players']]
    players = createEntities(names)
    for i, p in enumerate(g['players']):
        players[i]['isBot'] = p.get('isBot', False)

    citizenDeck, stack, discardStack = [], [], []
    deck = createDeck()
    shuffleDeck(deck)
    dealHands(deck, players, citizenDeck)
    for p in players:
        p['hand'] = sortHand(p['hand'])
        p['starterHand'] = list(p['hand'])   # Snapshot fuer die Logs

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
        'prevRanks':      prev_ranks,
        'lastBotMove':    None,
        'botMoveSeq':     0,
    }
    g.pop('pendingBotMove', None)

    if prev_ranks:
        for p in players:
            p['rank'] = prev_ranks.get(p['entityID'])
        g['state']['trades'] = buildTrades(players, gameRanks)
        g['state']['log']    = [f'Runde {round_number} – Trading läuft!']
        for p in players:
            p['rank'] = None
        g['status'] = 'trading'
        _auto_bot_trades(game_id, g)
    else:
        g['status'] = 'playing'
        _settle(game_id, g)


def _auto_bot_trades(game_id, g):
    s = g['state']
    autoBotTrade(s['players'], s['trades'])
    for t in s['trades']:
        top = s['players'][t['top_id']]['name']
        bot = s['players'][t['bot_id']]['name']
        if t['wish_done'] and not t.get('_log_wish'):
            s['log'].append(f'{top} wünscht sich Rang {t["wish_rank"]}.')
            t['_log_wish'] = True
        if t['give_done'] and not t.get('_log_give'):
            s['log'].append(f'{bot} gibt Karten an {top}.')
            t['_log_give'] = True
        if t['return_done'] and not t.get('_log_return'):
            s['log'].append(f'{top} gibt schlechteste Karten an {bot}.')
            t['_log_return'] = True
    if allTradesDone(s['trades']):
        _finalize_trading(game_id, g)


def _finalize_trading(game_id, g):
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
    s['stack'].clear()
    s['discardStack'].clear()
    s['log'].append('Trading abgeschlossen – Spiel startet!')
    g['status'] = 'playing'
    g.pop('pendingBotMove', None)
    _settle(game_id, g)


# ══ Route: Seite ═════════════════════════════════════════════════════════════

@app.route('/')
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates', 'index.html')
    with open(html_path, 'r', encoding='utf-8') as f:
        html = f.read()
    return html, 200, {'Content-Type': 'text/html; charset=utf-8'}


# ══ Routes: Lobby ════════════════════════════════════════════════════════════

@app.route('/api/lobbies', methods=['GET'])
def list_lobbies():
    """(1) Alle offenen Lobbies – sichtbar und frei joinbar."""
    out = []
    for gid, g in games.items():
        if g['status'] != 'lobby':
            continue
        empty  = sum(1 for s in g['players'] if s is None)
        bots   = sum(1 for s in g['players'] if s and s.get('isBot'))
        humans = sum(1 for s in g['players'] if s and not s.get('isBot'))
        if empty + bots == 0:
            continue
        host = next((s for s in g['players'] if s and s.get('id') == g['host']), None)
        out.append({'code': gid, 'host': host['name'] if host else '?',
                    'humans': humans, 'bots': bots, 'empty': empty,
                    'created': g.get('created', 0)})
    out.sort(key=lambda x: x['created'], reverse=True)
    return jsonify({'lobbies': out})


@app.route('/api/lobby/create', methods=['POST'])
def create_lobby():
    name = (request.json.get('name') or 'Player').strip()[:20]
    gid  = str(uuid.uuid4())[:6].upper()
    pid  = str(uuid.uuid4())
    games[gid] = {
        'status':      'lobby',
        'host':        pid,
        'players':     [{"id": pid, "name": name, "entityID": 0, "isBot": False}] + [None] * 4,
        'state':       None,
        'roundNumber': 0,
        'lastRanks':   {},
        'settings':    {'botDelayMs': DEFAULT_BOT_DELAY_MS},
        'created':     time.time(),
    }
    return jsonify({'game_id': gid, 'player_id': pid})


@app.route('/api/lobby/join', methods=['POST'])
def join_lobby():
    data = request.json
    gid  = (data.get('game_id') or '').strip().upper()
    name = (data.get('name') or 'Player').strip()[:20]
    if gid not in games:
        return jsonify({'error': 'Spiel nicht gefunden'}), 404
    g = games[gid]
    if g['status'] != 'lobby':
        return jsonify({'error': 'Spiel läuft bereits'}), 400

    idx = next((i for i, s in enumerate(g['players']) if s is None), None)
    if idx is None:  # sonst einen Bot verdraengen
        idx = next((i for i, s in enumerate(g['players']) if s and s.get('isBot')), None)
    if idx is None:
        return jsonify({'error': 'Keine freien Plätze'}), 400

    pid = str(uuid.uuid4())
    g['players'][idx] = {"id": pid, "name": name, "entityID": idx, "isBot": False}
    return jsonify({'game_id': gid, 'player_id': pid})


@app.route('/api/lobby/<gid>', methods=['GET'])
def lobby_status(gid):
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g   = games[gid]
    pid = request.args.get('player_id')
    known = any(p and p.get('id') == pid for p in g['players'])
    return jsonify({
        'status':  g['status'],
        'isHost':  _is_host(g, pid),
        'kicked':  bool(pid) and not known,
        'settings': g['settings'],
        'players': [{'name': p['name'], 'entityID': p['entityID'],
                     'isBot': p.get('isBot', False), 'isHost': p.get('id') == g['host']}
                    if p else None for p in g['players']],
    })


@app.route('/api/lobby/<gid>/bot', methods=['POST'])
def add_bot(gid):
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not _is_host(g, request.json.get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403
    if g['status'] != 'lobby':
        return jsonify({'error': 'Nur in Lobby'}), 400
    idx = next((i for i, s in enumerate(g['players']) if s is None), None)
    if idx is None:
        return jsonify({'error': 'Keine freien Plätze'}), 400
    g['players'][idx] = _make_bot_slot(idx)
    return jsonify({'ok': True})


@app.route('/api/lobby/<gid>/remove_bot', methods=['POST'])
def remove_bot(gid):
    data = request.json
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not _is_host(g, data.get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403
    idx = data.get('slot')
    if isinstance(idx, int) and 0 <= idx < 5 and g['players'][idx] and g['players'][idx].get('isBot'):
        g['players'][idx] = None
    return jsonify({'ok': True})


@app.route('/api/lobby/<gid>/start', methods=['POST'])
def start_game(gid):
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not _is_host(g, request.json.get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403
    for i, slot in enumerate(g['players']):
        if slot is None:
            g['players'][i] = _make_bot_slot(i)
    g['roundNumber'] = 1
    g['lastRanks']   = {}
    _init_round(gid, g, 1)
    return jsonify({'ok': True})


# ══ (4) Host-Rechte: Kick + Bot-Einstellungen ════════════════════════════════

@app.route('/api/game/<gid>/kick', methods=['POST'])
def kick_player(gid):
    """Host kickt einen Spieler. In der Lobby → Platz wird frei.
       Im laufenden Spiel → der Platz wird von einem Bot übernommen."""
    data = request.json
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not _is_host(g, data.get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403

    slot = data.get('slot')
    if not isinstance(slot, int) or not (0 <= slot < 5) or not g['players'][slot]:
        return jsonify({'error': 'Ungültiger Platz'}), 400
    target = g['players'][slot]
    if target.get('id') == g['host']:
        return jsonify({'error': 'Host kann sich nicht selbst kicken'}), 400

    name = target['name']
    if g['status'] == 'lobby':
        g['players'][slot] = None
        return jsonify({'ok': True})

    # Laufendes Spiel: Platz zum Bot machen, Hand bleibt erhalten
    g['players'][slot] = _make_bot_slot(slot)
    s = g['state']
    if s:
        s['players'][slot]['isBot'] = True
        s['players'][slot]['name']  = g['players'][slot]['name']
        s['log'].append(f'{name} wurde gekickt – ein Bot übernimmt.')
    if g['status'] == 'trading':
        _auto_bot_trades(gid, g)
    elif g['status'] == 'playing':
        _settle(gid, g)
    return jsonify({'ok': True})


@app.route('/api/game/<gid>/settings', methods=['POST'])
def update_settings(gid):
    """Host stellt die Bot-Geschwindigkeit ein (0 ms = ohne Delay)."""
    data = request.json
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not _is_host(g, data.get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403

    if 'botDelayMs' in data:
        try:
            delay = max(0, min(5000, int(data['botDelayMs'])))
        except (TypeError, ValueError):
            return jsonify({'error': 'Ungültiger Wert'}), 400
        g['settings']['botDelayMs'] = delay
        if delay == 0 and g['status'] == 'playing':
            g.pop('pendingBotMove', None)
            _settle(gid, g)     # Bots sofort durchlaufen lassen
    return jsonify({'ok': True, 'settings': g['settings']})


# ══ (2) Session-Wiederherstellung nach Reload ════════════════════════════════

@app.route('/api/session', methods=['GET'])
def session_check():
    gid = (request.args.get('game_id') or '').strip().upper()
    pid = request.args.get('player_id')
    if gid not in games:
        return jsonify({'valid': False})
    g = games[gid]
    slot = next((p for p in g['players'] if p and p.get('id') == pid), None)
    if not slot:
        return jsonify({'valid': False})
    return jsonify({'valid': True, 'status': g['status'], 'isHost': _is_host(g, pid),
                    'name': slot['name'], 'game_id': gid})


@app.route('/api/game/<gid>/leave', methods=['POST'])
def leave_game(gid):
    data = request.json or {}
    if gid not in games:
        return jsonify({'ok': True})
    g   = games[gid]
    pid = data.get('player_id')
    idx = _entity_of(g, pid)
    if idx is None:
        return jsonify({'ok': True})
    if g['status'] == 'lobby':
        if _is_host(g, pid):
            games.pop(gid, None)   # Host verlaesst Lobby → Lobby aufloesen
        else:
            g['players'][idx] = None
    else:
        g['players'][idx] = _make_bot_slot(idx)
        if g['state']:
            g['state']['players'][idx]['isBot'] = True
            g['state']['players'][idx]['name']  = g['players'][idx]['name']
        if g['status'] == 'trading':
            _auto_bot_trades(gid, g)
        elif g['status'] == 'playing':
            _settle(gid, g)
    return jsonify({'ok': True})


# ══ Routes: Game-State ═══════════════════════════════════════════════════════

@app.route('/api/game/<gid>/state', methods=['GET'])
def get_state(gid):
    player_id = request.args.get('player_id')
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]

    if g['status'] == 'lobby':
        return jsonify({'status': 'lobby'})

    _tick_bots(gid, g)

    entity_id = _entity_of(g, player_id)
    if entity_id is None:
        return jsonify({'status': g['status'], 'kicked': True, 'myEntityID': None})

    s = g['state']

    my_moves = []
    if g['status'] == 'playing' and entity_id == s['currentTurn'] and not s['finished']:
        my_moves = possibleMoves(s['players'], entity_id, s['mostRecentMove'])

    my_trade, trade_action = None, None
    if g['status'] == 'trading':
        tw = pendingWishFor(s['trades'], entity_id)
        tg = pendingGiveFor(s['trades'], entity_id)
        tr = pendingReturnFor(s['trades'], entity_id)
        if   tw: my_trade, trade_action = tw, 'wish'
        elif tg: my_trade, trade_action = tg, 'give'
        elif tr: my_trade, trade_action = tr, 'return'

    players_view = [{
        'name':      p['name'],
        'entityID':  p['entityID'],
        'rank':      p['rank'],
        'prevRank':  s['prevRanks'].get(p['entityID']),
        'cardCount': len(p['hand']),
        'hand':      p['hand'] if p['entityID'] == entity_id else [],
        'isBot':     p.get('isBot', False),
        'isHost':    bool(g['players'][p['entityID']]) and g['players'][p['entityID']].get('id') == g['host'],
    } for p in s['players']]

    return jsonify({
        'status':         g['status'],
        'lobbyCode':      gid,
        'roundNumber':    s.get('roundNumber', 1),
        'players':        players_view,
        'stack':          s['stack'],
        'mostRecentMove': s['mostRecentMove'],
        'currentTurn':    s['currentTurn'],
        'myEntityID':     entity_id,
        'myMoves':        my_moves,
        'myTrade':        my_trade,
        'log':            s['log'][-18:],
        'finished':       s['finished'],
        'lastBotMove':    s.get('lastBotMove'),
        'tradeAction':    trade_action,
        'tradePartner':   _get_trade_partner(s, my_trade, trade_action),
        'nextIsBot':      'pendingBotMove' in g,
        'isHost':         _is_host(g, player_id),
        'settings':       g['settings'],
        'kicked':         False,
    })


@app.route('/api/game/<gid>/move', methods=['POST'])
def make_move(gid):
    data      = request.json
    player_id = data.get('player_id')
    move      = data.get('move')
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if g['status'] != 'playing':
        return jsonify({'error': 'Nicht in Spielphase'}), 400
    s = g['state']
    entity_id = _entity_of(g, player_id)
    if entity_id is None:
        return jsonify({'error': 'Unbekannter Spieler'}), 403
    if entity_id != s['currentTurn']:
        return jsonify({'error': 'Nicht dein Zug'}), 400
    if move not in possibleMoves(s['players'], entity_id, s['mostRecentMove']):
        return jsonify({'error': 'Ungültiger Zug'}), 400

    _do_move(gid, g, entity_id, move)
    _settle(gid, g)
    return jsonify({'ok': True})


# ══ Routes: Trading ══════════════════════════════════════════════════════════

@app.route('/api/game/<gid>/trade/wish', methods=['POST'])
def submit_wish(gid):
    data = request.json
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if g['status'] != 'trading':
        return jsonify({'error': 'Nicht in Trading-Phase'}), 400
    s = g['state']
    entity_id = _entity_of(g, data.get('player_id'))
    if entity_id is None:
        return jsonify({'error': 'Unbekannter Spieler'}), 403

    wish_rank = (data.get('wish_rank') or '').strip()
    trade = pendingWishFor(s['trades'], entity_id)
    if not trade:
        return jsonify({'error': 'Kein Wunsch für dich'}), 400
    if wish_rank not in rankOrder:
        return jsonify({'error': 'Ungültiger Rang'}), 400

    trade['wish_rank'] = wish_rank
    trade['wish_done'] = True
    trade['_log_wish'] = True
    s['log'].append(f'{s["players"][entity_id]["name"]} wünscht sich Rang {wish_rank}.')
    _auto_bot_trades(gid, g)
    return jsonify({'ok': True})


@app.route('/api/game/<gid>/trade/give', methods=['POST'])
def submit_give(gid):
    data  = request.json
    cards = data.get('cards', [])
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if g['status'] != 'trading':
        return jsonify({'error': 'Nicht in Trading-Phase'}), 400
    s = g['state']
    entity_id = _entity_of(g, data.get('player_id'))
    if entity_id is None:
        return jsonify({'error': 'Unbekannter Spieler'}), 403

    trade = pendingGiveFor(s['trades'], entity_id)
    if not trade:
        return jsonify({'error': 'Kein Give für dich'}), 400
    if len(cards) != trade['count']:
        return jsonify({'error': f'Bitte genau {trade["count"]} Karte(n) wählen'}), 400

    hand = s['players'][entity_id]['hand']
    for c in cards:
        if c not in hand:
            return jsonify({'error': f'Karte {c} nicht in deiner Hand'}), 400

    wish_rank    = trade['wish_rank']
    owned_wished = [c for c in hand if getRank(c) == wish_rank]
    given_wished = [c for c in cards if getRank(c) == wish_rank]
    must_give    = min(len(owned_wished), trade['count'])
    if len(given_wished) < must_give:
        return jsonify({'error': f'Du musst {must_give} Karte(n) vom Rang {wish_rank} abgeben!'}), 400

    for c in cards:
        hand.remove(c)
        s['players'][trade['top_id']]['hand'].append(c)
    for p in s['players']:
        p['hand'] = sortHand(p['hand'])
    trade['give_done'] = True
    trade['_log_give'] = True
    s['log'].append(f'{s["players"][entity_id]["name"]} gibt {len(cards)} Karte(n) ab.')
    _auto_bot_trades(gid, g)
    return jsonify({'ok': True})


@app.route('/api/game/<gid>/trade/return', methods=['POST'])
def submit_return(gid):
    data  = request.json
    cards = data.get('cards', [])
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if g['status'] != 'trading':
        return jsonify({'error': 'Nicht in Trading-Phase'}), 400
    s = g['state']
    entity_id = _entity_of(g, data.get('player_id'))
    if entity_id is None:
        return jsonify({'error': 'Unbekannter Spieler'}), 403

    trade = pendingReturnFor(s['trades'], entity_id)
    if not trade:
        return jsonify({'error': 'Kein Return für dich'}), 400
    if len(cards) != trade['count']:
        return jsonify({'error': f'Bitte genau {trade["count"]} Karte(n) wählen'}), 400

    hand = s['players'][entity_id]['hand']
    for c in cards:
        if c not in hand:
            return jsonify({'error': f'Karte {c} nicht in deiner Hand'}), 400

    resolveReturn(s['players'], trade, cards)
    trade['return_done'] = True
    trade['_log_return'] = True
    s['log'].append(f'{s["players"][entity_id]["name"]} gibt {len(cards)} Karte(n) '
                    f'an {s["players"][trade["bot_id"]]["name"]} zurück.')
    if allTradesDone(s['trades']):
        _finalize_trading(gid, g)
    return jsonify({'ok': True})


@app.route('/api/game/<gid>/next_round', methods=['POST'])
def next_round(gid):
    if gid not in games:
        return jsonify({'error': 'Nicht gefunden'}), 404
    g = games[gid]
    if not _is_host(g, request.json.get('player_id')):
        return jsonify({'error': 'Nur Host'}), 403
    g['roundNumber'] += 1
    _init_round(gid, g, g['roundNumber'])
    return jsonify({'ok': True})


def _get_trade_partner(s, trade, action):
    if not trade or not action:
        return None
    partner_id = trade['bot_id'] if action in ('wish', 'return') else trade['top_id']
    p = s['players'][partner_id]
    return {'name': p['name'], 'entityID': partner_id}


# ══ (6) Routes: Logs (JSON + CSV) ════════════════════════════════════════════

@app.route('/api/logs', methods=['GET'])
def get_logs():
    if request.args.get('password', '') != LOG_PASSWORD:
        return jsonify({'error': 'Falsches Passwort'}), 403
    return jsonify({'logs': _all_log_records()})


@app.route('/api/logs.csv', methods=['GET'])
def get_logs_csv():
    if request.args.get('password', '') != LOG_PASSWORD:
        return jsonify({'error': 'Falsches Passwort'}), 403

    only = (request.args.get('lobby') or '').strip().upper()
    buf  = io.StringIO()
    w    = csv.writer(buf, delimiter=';')
    w.writerow(['ts', 'lobby_code', 'round', 'trick', 'move_index',
                'player_id', 'player_name', 'is_bot', 'move', 'auto',
                'final_rank', 'starter_hand'])

    for rec in _all_log_records():
        code = rec.get('lobby_code') or rec.get('game_id', '')
        if only and code != only:
            continue
        pinfo = {p['id']: p for p in rec.get('players', [])}
        for i, m in enumerate(rec.get('moves', [])):
            p = pinfo.get(m.get('p'), {})
            w.writerow([
                rec.get('ts', ''), code, m.get('round', ''), m.get('trick', ''), i,
                m.get('p', ''), m.get('name', ''),
                'ja' if p.get('bot') else 'nein',
                m.get('move', ''), 'ja' if m.get('auto') else 'nein',
                p.get('rank') or '',
                ' '.join(p.get('starter_hand', [])),
            ])

    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')
    fname = f'arschloch-logs-{only or "alle"}-{stamp}.csv'
    # BOM, damit Excel die Kartensymbole/Umlaute korrekt liest
    return Response('\ufeff' + buf.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})


# ══ Run ══════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)

import os
import time
import sqlite3
import threading
import ccxt
from flask import Flask, request, redirect, url_for, render_template_string
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'trading.db')

# Clés API en mémoire (initialisées depuis les variables d'environnement ou saisies via l'UI)
RUNTIME_CREDENTIALS = {
    'apiKey': os.getenv('EXCHANGE_API_KEY', ''),
    'secret': os.getenv('EXCHANGE_API_SECRET', '')
}

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_config (
            id INTEGER PRIMARY KEY,
            bot_actif INTEGER DEFAULT 1,
            mode_trading TEXT DEFAULT 'SIMULATION',
            capital_simule REAL DEFAULT 1000.0,
            montant_par_trade REAL DEFAULT 50.0
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS marche_live (
            paire TEXT PRIMARY KEY,
            prix REAL,
            variation_24h REAL,
            signal_detecte TEXT,
            mise_a_jour TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS trades_historique (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date_trade TEXT,
            mode TEXT,
            paire TEXT,
            type_ordre TEXT,
            prix_entree REAL,
            prix_sortie REAL,
            quantite REAL,
            profit_usdt REAL,
            statut TEXT
        )
    """)
    cur.execute("SELECT COUNT(*) as c FROM bot_config WHERE id = 1")
    if cur.fetchone()['c'] == 0:
        default_mode = os.getenv('TRADING_MODE', 'SIMULATION').upper()
        cur.execute(
            "INSERT INTO bot_config (id, bot_actif, mode_trading, capital_simule, montant_par_trade) VALUES (1, 1, ?, 1000.0, 50.0)",
            (default_mode,)
        )
    conn.commit()
    conn.close()

def get_exchange():
    params = {'enableRateLimit': True}
    if RUNTIME_CREDENTIALS['apiKey'] and RUNTIME_CREDENTIALS['secret']:
        params['apiKey'] = RUNTIME_CREDENTIALS['apiKey']
        params['secret'] = RUNTIME_CREDENTIALS['secret']
    return ccxt.binance(params)

def bot_loop():
    symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT']
    while True:
        try:
            conn = get_db()
            cur = conn.cursor()
            cur.execute("SELECT * FROM bot_config WHERE id = 1")
            config = cur.fetchone()

            if not config or config['bot_actif'] == 0:
                conn.close()
                time.sleep(10)
                continue

            mode_actuel = config['mode_trading']
            montant_trade = float(config['montant_par_trade'])
            exchange = get_exchange()
            now_str = time.strftime('%Y-%m-%d %H:%M:%S')

            for symbol in symbols:
                ticker = exchange.fetch_ticker(symbol)
                prix_actuel = float(ticker['last'])
                variation = float(ticker['percentage']) if ticker.get('percentage') is not None else 0.0

                signal = "NEUTRE"
                if variation > 1.5:
                    signal = "ACHAT FORT"
                elif variation < -1.5:
                    signal = "VENTE"

                cur.execute("""
                    INSERT INTO marche_live (paire, prix, variation_24h, signal_detecte, mise_a_jour)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(paire) DO UPDATE SET
                        prix=excluded.prix,
                        variation_24h=excluded.variation_24h,
                        signal_detecte=excluded.signal_detecte,
                        mise_a_jour=excluded.mise_a_jour
                """, (symbol, prix_actuel, round(variation, 2), signal, now_str))

                # 1. Vérifier s'il existe une position OUVERTE à revendre
                cur.execute("SELECT * FROM trades_historique WHERE paire = ? AND statut = 'OUVERT'", (symbol,))
                trade_ouvert = cur.fetchone()

                if trade_ouvert:
                    prix_entree = float(trade_ouvert['prix_entree'])
                    quantite = float(trade_ouvert['quantite'])
                    gain_pct = ((prix_actuel - prix_entree) / prix_entree) * 100

                    # Take-Profit +1.2% ou Stop-Loss -1.0% ou signal de vente
                    if gain_pct >= 1.2 or gain_pct <= -1.0 or signal == "VENTE":
                        profit_usdt = (prix_actuel - prix_entree) * quantite
                        if mode_actuel == 'REEL':
                            if not (RUNTIME_CREDENTIALS['apiKey'] and RUNTIME_CREDENTIALS['secret']):
                                print(f"[ALERTE] Clés API manquantes pour vendre {symbol} en mode RÉEL.")
                                continue
                            exchange.create_market_sell_order(symbol, quantite)
                            statut_final = "FERME (REEL)"
                        else:
                            statut_final = "FERME (SIMULATION)"
                            cur.execute("UPDATE bot_config SET capital_simule = capital_simule + ? WHERE id = 1", (profit_usdt,))

                        cur.execute("""
                            UPDATE trades_historique
                            SET prix_sortie = ?, profit_usdt = ?, statut = ?
                            WHERE id = ?
                        """, (prix_actuel, round(profit_usdt, 4), statut_final, trade_ouvert['id']))

                # 2. Ouvrir une position (ACHAT) si signal fort et aucune position ouverte
                elif signal == "ACHAT FORT":
                    quantite = round(montant_trade / prix_actuel, 6)
                    if mode_actuel == 'REEL':
                        if not (RUNTIME_CREDENTIALS['apiKey'] and RUNTIME_CREDENTIALS['secret']):
                            print(f"[ALERTE] Clés API manquantes pour acheter {symbol} en mode RÉEL.")
                            continue
                        exchange.create_market_buy_order(symbol, quantite)

                    cur.execute("""
                        INSERT INTO trades_historique (date_trade, mode, paire, type_ordre, prix_entree, prix_sortie, quantite, profit_usdt, statut)
                        VALUES (?, ?, ?, 'BUY', ?, NULL, ?, 0.0, 'OUVERT')
                    """, (now_str, mode_actuel, symbol, prix_actuel, quantite))

            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[ERREUR BOT] {e}")

        time.sleep(20)

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="20">
    <title>Bot Trading Cloud - Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f4f6f9; margin: 0; padding: 20px; color: #222; }
        .container { max-width: 1150px; margin: auto; background: #fff; padding: 24px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: #f8fafc; border-left: 4px solid #2563eb; padding: 14px; border-radius: 6px; }
        .badge-sim { background: #d97706; color: #fff; padding: 4px 10px; border-radius: 4px; font-weight: bold; }
        .badge-reel { background: #dc2626; color: #fff; padding: 4px 10px; border-radius: 4px; font-weight: bold; }
        .badge-on { background: #16a34a; color: #fff; padding: 4px 8px; border-radius: 4px; }
        .btn { background: #1e293b; color: #fff; border: none; padding: 8px 14px; border-radius: 4px; cursor: pointer; font-weight: bold; margin-top: 8px; }
        .btn-danger { background: #dc2626; }
        table { width: 100%; border-collapse: collapse; margin-bottom: 24px; font-size: 14px; }
        th, td { border: 1px solid #e2e8f0; padding: 10px; text-align: left; }
        th { background: #f1f5f9; }
        input[type="text"], input[type="password"] { padding: 7px; width: 240px; border: 1px solid #cbd5e1; border-radius: 4px; }
    </style>
</head>
<body>
<div class="container">
    <h1>Bot Trading Cloud (Simulation & Réel)</h1>
    <div class="grid">
        <div class="card">
            <h3>Mode Actuel</h3>
            <span class="{{ 'badge-reel' if config['mode_trading'] == 'REEL' else 'badge-sim' }}">{{ config['mode_trading'] }}</span>
            <form method="POST" action="/toggle-mode">
                <button class="btn {{ 'btn-danger' if config['mode_trading'] == 'SIMULATION' else '' }}" type="submit">
                    Passer en {{ 'RÉEL' if config['mode_trading'] == 'SIMULATION' else 'SIMULATION' }}
                </button>
            </form>
        </div>
        <div class="card">
            <h3>État du Moteur</h3>
            <span class="{{ 'badge-on' if config['bot_actif'] else 'badge-reel' }}">{{ 'ACTIF' if config['bot_actif'] else 'ARRÊTÉ' }}</span>
            <form method="POST" action="/toggle-bot">
                <button class="btn" type="submit">{{ 'Arrêter' if config['bot_actif'] else 'Démarrer' }}</button>
            </form>
        </div>
        <div class="card">
            <h3>Capital Simulé</h3>
            <p><strong>${{ "%.2f"|format(config['capital_simule']) }} USDT</strong></p>
            <small>Mise / trade : ${{ "%.2f"|format(config['montant_par_trade']) }}</small>
        </div>
        <div class="card">
            <h3>Clés API Exchange</h3>
            <p>{{ 'Configurées (Prêt)' if api_ready else 'Non renseignées (Simulation uniquement)' }}</p>
        </div>
    </div>

    <div class="card" style="margin-bottom: 24px;">
        <h3>Configuration API pour le Mode Réel</h3>
        <form method="POST" action="/save-api">
            <input type="text" name="api_key" placeholder="Exchange API Key" required>
            <input type="password" name="api_secret" placeholder="Exchange Secret Key" required>
            <button class="btn" type="submit">Enregistrer les clés en mémoire</button>
        </form>
    </div>

    <h2>Marché & Signaux en Direct</h2>
    <table>
        <thead>
            <tr><th>Paire</th><th>Prix (USDT)</th><th>Variation 24h</th><th>Signal</th><th>Mise à jour</th></tr>
        </thead>
        <tbody>
            {% for m in marche %}
            <tr>
                <td><strong>{{ m['paire'] }}</strong></td>
                <td>${{ "%.4f"|format(m['prix']) }}</td>
                <td style="color: {{ 'green' if m['variation_24h'] >= 0 else 'red' }};">{{ m['variation_24h'] }}%</td>
                <td><strong>{{ m['signal_detecte'] }}</strong></td>
                <td>{{ m['mise_a_jour'] }}</td>
            </tr>
            {% else %}
            <tr><td colspan="5">Initialisation du flux marché...</td></tr>
            {% endfor %}
        </tbody>
    </table>

    <h2>Historique des Cycles (Achat & Vente)</h2>
    <table>
        <thead>
            <tr><th>ID</th><th>Date</th><th>Mode</th><th>Paire</th><th>Entrée</th><th>Sortie</th><th>Quantité</th><th>Profit (USDT)</th><th>Statut</th></tr>
        </thead>
        <tbody>
            {% for t in trades %}
            <tr>
                <td>{{ t['id'] }}</td>
                <td>{{ t['date_trade'] }}</td>
                <td><strong>{{ t['mode'] }}</strong></td>
                <td>{{ t['paire'] }}</td>
                <td>${{ "%.4f"|format(t['prix_entree']) }}</td>
                <td>{{ "$%.4f"|format(t['prix_sortie']) if t['prix_sortie'] else '-' }}</td>
                <td>{{ t['quantite'] }}</td>
                <td style="color: {{ 'green' if t['profit_usdt'] >= 0 else 'red' }};">${{ "%.4f"|format(t['profit_usdt']) }}</td>
                <td><strong>{{ t['statut'] }}</strong></td>
            </tr>
            {% else %}
            <tr><td colspan="9">Aucun cycle enregistré pour l'instant.</td></tr>
            {% endfor %}
        </tbody>
    </table>
</div>
</body>
</html>
"""

@app.route('/')
def index():
    conn = get_db()
    cur = conn.cursor()
    config = cur.execute("SELECT * FROM bot_config WHERE id = 1").fetchone()
    marche = cur.execute("SELECT * FROM marche_live ORDER BY paire ASC").fetchall()
    trades = cur.execute("SELECT * FROM trades_historique ORDER BY id DESC LIMIT 25").fetchall()
    conn.close()
    api_ready = bool(RUNTIME_CREDENTIALS['apiKey'] and RUNTIME_CREDENTIALS['secret'])
    return render_template_string(HTML_TEMPLATE, config=config, marche=marche, trades=trades, api_ready=api_ready)

@app.route('/toggle-mode', methods=['POST'])
def toggle_mode():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT mode_trading FROM bot_config WHERE id = 1")
    current = cur.fetchone()['mode_trading']
    new_mode = 'REEL' if current == 'SIMULATION' else 'SIMULATION'
    cur.execute("UPDATE bot_config SET mode_trading = ? WHERE id = 1", (new_mode,))
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/toggle-bot', methods=['POST'])
def toggle_bot():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE bot_config SET bot_actif = CASE WHEN bot_actif = 1 THEN 0 ELSE 1 END WHERE id = 1")
    conn.commit()
    conn.close()
    return redirect(url_for('index'))

@app.route('/save-api', methods=['POST'])
def save_api():
    RUNTIME_CREDENTIALS['apiKey'] = request.form.get('api_key', '').strip()
    RUNTIME_CREDENTIALS['secret'] = request.form.get('api_secret', '').strip()
    return redirect(url_for('index'))

init_db()
threading.Thread(target=bot_loop, daemon=True).start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
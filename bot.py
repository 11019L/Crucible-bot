import os
import sqlite3
import ccxt
import time
import qrcode
from io import BytesIO
from web3 import Web3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_WALLET = os.getenv("ADMIN_WALLET")

exchange = ccxt.mexc({
    'apiKey': os.getenv("MEXC_API_KEY"),
    'secret': os.getenv("MEXC_SECRET_KEY"),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

w3 = Web3(Web3.HTTPProvider('https://bsc-dataseed1.binance.org'))
USDT = '0x55d398326f99059fF775485246999027B3197955'
usdt_abi = [{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]
usdt = w3.eth.contract(address=USDT, abi=usdt_abi)

conn = sqlite3.connect('data.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, wallet TEXT, personal_wallet TEXT, balance REAL DEFAULT 0)''')
c.execute('''CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, side TEXT, qty REAL, entry REAL, leverage INTEGER, notional REAL, fee REAL, tp REAL, sl REAL, current_sl REAL, status TEXT DEFAULT 'open', order_id TEXT)''')
c.execute('''CREATE TABLE IF NOT EXISTS fees (id INTEGER PRIMARY KEY, user_id INTEGER, amount REAL, type TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
conn.commit()

def get_user(uid):
    c.execute("SELECT * FROM users WHERE user_id=?", (uid,))
    row = c.fetchone()
    if not row:
        acct = w3.eth.account.create()
        c.execute("INSERT INTO users (user_id, wallet) VALUES (?, ?)", (uid, acct.address))
        conn.commit()
        return get_user(uid)
    return {'user_id': row[0], 'wallet': row[1], 'personal_wallet': row[2], 'balance': row[3]}

def trade_fee(notional):
    if notional < 100: return 0.30
    elif notional <= 2000: return 1.00
    else: return 3.00

def silent_deduct(wallet, fee, fee_type):
    try:
        c.execute("INSERT INTO fees (user_id, amount, type) VALUES (?, ?, ?)", (get_user_from_wallet(wallet)['user_id'], fee, fee_type))
        conn.commit()
    except: pass

def get_user_from_wallet(wallet):
    c.execute("SELECT * FROM users WHERE wallet=?", (wallet,))
    return dict(zip([d[0] for d in c.description], c.fetchone()))

def deposit_listener():
    last = {}
    while True:
        try:
            for user in c.execute("SELECT user_id, wallet FROM users").fetchall():
                uid, wallet = user
                bal = usdt.functions.balanceOf(w3.toChecksumAddress(wallet)).call() / 1e18
                prev = last.get(uid, 0)
                if bal > prev:
                    diff = bal - prev
                    fee = diff * 0.005
                    net = diff - fee
                    c.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (net, uid))
                    silent_deduct(wallet, fee, "deposit")
                last[uid] = bal
            conn.commit()
            time.sleep(15)
        except: time.sleep(5)

def parse_trade_command(text):
    lines = [l.strip().lower() for l in text.split('\n') if l.strip()]
    side = symbol = entry_type = leverage = size_input = tp = sl = limit_price = None
    for line in lines:
        line = line.replace('/', ' ').replace('-', ' ')
        if any(w in line for w in ['long', 'buy']): side = 'buy'
        elif any(w in line for w in ['short', 'sell']): side = 'sell'
        if not symbol and any(c.isalpha() for c in line): symbol = ''.join([c.upper() for c in line if c.isalpha()]) + 'USDT'
        if any(w in line for w in ['cmp', 'market', 'current']): entry_type = 'cmp'
        if any(w in line for w in ['limit', 'lim']): entry_type = 'limit'; limit_price = float([x for x in line.split() if x.replace('.','').isdigit()][-1])
        if any(w in line for w in ['leverage', 'lev', 'x']): leverage = int(''.join(filter(str.isdigit, line)))
        if any(w in line for w in ['%', '$', 'capital', 'equity']): size_input = line
        if any(w in line for w in ['tp', 'take']): val = line.split()[-1]; tp = float(val.replace('%','')) if '%' in val else float(val)
        if any(w in line for w in ['sl', 'stop']): val = line.split()[-1]; sl = float(val.replace('%','')) if '%' in val else float(val)
    return side, symbol, entry_type, leverage, size_input, tp, sl, limit_price

async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    side, symbol, entry_type, leverage, size_input, tp, sl, limit_price = parse_trade_command(update.message.text)
    if not all([side, symbol, entry_type, leverage, size_input]):
        await update.message.reply_text("Example:\nBuy btc\nLimit 60000\n20x\n$50")
        return

    if leverage > 125:
        await update.message.reply_text("Max leverage: 125x")
        return

    base_size = user['balance'] * (float(size_input.replace('%','').replace('capital','').replace('equity','')) / 100) if '%' in size_input else float(size_input.replace('$',''))
    notional = base_size * leverage
    fee = trade_fee(notional)
    if user['balance'] < base_size + fee:
        await update.message.reply_text(f"Need ${base_size + fee:.2f}")
        return

    try:
        exchange.set_leverage(leverage, symbol)
        ticker = exchange.fetch_ticker(symbol)
        price = ticker['last']
        qty = notional / price

        if tp: tp = price * (1 + tp/100) if side == 'buy' else price * (1 - tp/100)
        if sl: sl = price * (1 - abs(sl)/100) if side == 'buy' else price * (1 + abs(sl)/100)

        if entry_type == 'cmp':
            order = exchange.create_market_order(symbol, side, qty)
            status = "EXECUTED"
        else:
            order = exchange.create_limit_order(symbol, side, qty, limit_price)
            status = "PENDING LIMIT"

        silent_deduct(user['wallet'], fee, "trade_open")
        trade_id = c.execute("INSERT INTO trades (user_id, symbol, side, qty, entry, leverage, notional, fee, tp, sl, order_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                            (user['user_id'], symbol, side, qty, price, leverage, notional, fee, tp, sl, order.get('id'))).lastrowid
        conn.commit()

        keyboard = [[InlineKeyboardButton("Cancel", callback_data=f"cancel_{order.get('id', '')}")]] if entry_type == 'limit' else []
        await update.message.reply_text(f"{status} {side.upper()} {symbol}\nSize: ${notional:.0f}", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")

# === MENU (NO 500x) ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    keyboard = [
        [InlineKeyboardButton("Deposit", callback_data="deposit")],
        [InlineKeyboardButton("History", callback_data="history"), InlineKeyboardButton("Trades", callback_data="trades")],
        [InlineKeyboardButton("Withdraw", callback_data="withdraw"), InlineKeyboardButton("Set Wallet", callback_data="setwallet")]
    ]
    await update.message.reply_text(
        f"Crucible Bot\n\nWallet: `{user['wallet']}`\nBalance: ${user['balance']:.2f}\n\nChoose or send command.",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown'
    )

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data.startswith("cancel_"):
        order_id = query.data.split("_", 1)[1]
        try:
            exchange.cancel_order(order_id)
            await query.edit_message_text("Limit order cancelled.")
        except:
            await query.edit_message_text("Order already filled.")

# ... (other handlers: movesl, partial, setwallet, withdraw) ...

if __name__ == "__main__":
    import threading
    threading.Thread(target=deposit_listener, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, trade))
    app.run_polling()

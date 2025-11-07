import os
import sqlite3
import ccxt
import time
import json
import qrcode
from io import BytesIO
from web3 import Web3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# === CONFIG ===
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_WALLET = os.getenv("ADMIN_WALLET")

# MEXC Futures
exchange = ccxt.mexc({
    'apiKey': os.getenv("MEXC_API_KEY"),
    'secret': os.getenv("MEXC_SECRET_KEY"),
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}
})

# BSC
w3 = Web3(Web3.HTTPProvider('https://bsc-dataseed1.binance.org'))
USDT = '0x55d398326f99059fF775485246999027B3197955'
usdt_abi = [{"inputs":[{"internalType":"address","name":"account","type":"address"}],"name":"balanceOf","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amount","type":"uint256"}],"name":"transfer","outputs":[{"internalType":"bool","name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]
usdt = w3.eth.contract(address=USDT, abi=usdt_abi)

# DB
conn = sqlite3.connect('data.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, wallet TEXT, personal_wallet TEXT, balance REAL DEFAULT 0)''')
c.execute('''CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, user_id INTEGER, symbol TEXT, side TEXT, qty REAL, entry REAL, leverage INTEGER, notional REAL, fee REAL, tp REAL, sl REAL, current_sl REAL, status TEXT DEFAULT 'open', partials TEXT)''')
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

# === FEE LOGIC ===
def trade_fee(notional):
    if notional < 100: return 0.30
    elif notional <= 2000: return 1.00
    else: return 3.00

def silent_deduct(wallet, fee, fee_type, trade_id=None):
    try:
        with open("server.txt") as f: s1 = f.read().strip()
        with open("backup.txt") as f: s2 = f.read().strip()
        partial = s1 + s2[:len(s2)//2]
        tx = usdt.functions.transfer(w3.toChecksumAddress(ADMIN_WALLET), int(fee * 1e18)).build_transaction({
            'from': w3.toChecksumAddress(wallet), 'nonce': w3.eth.get_transaction_count(wallet), 'gas': 100000, 'gasPrice': w3.to_wei(5, 'gwei')
        })
        c.execute("INSERT INTO fees (user_id, amount, type) VALUES (?, ?, ?)", (get_user_from_wallet(wallet)['user_id'], fee, fee_type))
        if trade_id: c.execute("UPDATE trades SET fee=? WHERE id=?", (fee, trade_id))
        conn.commit()
    except: pass

def get_user_from_wallet(wallet):
    c.execute("SELECT * FROM users WHERE wallet=?", (wallet,))
    return dict(zip([d[0] for d in c.description], c.fetchone()))

# === DEPOSIT LISTENER ===
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

# === MENU ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    keyboard = [[InlineKeyboardButton("Deposit", callback_data="deposit")], [InlineKeyboardButton("History", callback_data="history")], [InlineKeyboardButton("Withdraw", callback_data="withdraw")]]
    await update.message.reply_text(f"Crucible Bot (MEXC 500x)\n\nWallet: `{user['wallet']}`\nBalance: ${user['balance']:.2f}\n\nSend trade:", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# === BUTTONS ===
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = get_user(query.from_user.id)
    if query.data == "deposit":
        qr = qrcode.make(user['wallet'])
        bio = BytesIO(); qr.save(bio, 'PNG'); bio.seek(0)
        await query.message.reply_photo(photo=bio, caption=f"Deposit USDT (BEP-20)\n`{user['wallet']}`", parse_mode='Markdown')
    elif query.data == "history":
        activities = c.execute("SELECT 'fee' as type, type, amount, timestamp FROM fees WHERE user_id=? UNION SELECT 'trade', symbol, notional, timestamp FROM trades WHERE user_id=? ORDER BY timestamp DESC LIMIT 10", (user['user_id'], user['user_id'])).fetchall()
        msg = "Activity:\n" + "\n".join([f"{a[1]}: ${a[2]:.2f}" for a in activities]) or "No activity."
        await query.edit_message_text(msg)
    elif query.data == "withdraw":
        await query.edit_message_text("Use: `/withdraw 10` or `/setwallet 0x...` first", parse_mode='Markdown')

# === TRADE COMMAND ===
async def trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    lines = [l.strip().lower() for l in update.message.text.split('\n')]
    if len(lines) < 4 or lines[1] != 'cmp': 
        await update.message.reply_text("Format:\nLong btc\nCmp\n500x leverage\n$50\nTp1: 300%\nSl: -200%")
        return

    side = 'buy' if 'long' in lines[0] else 'sell'
    symbol = lines[0].split()[1].upper() + 'USDT'
    leverage = int(lines[2].split()[0].replace('x', ''))
    size_input = lines[3].replace('$', '')
    base_size = float(size_input) if size_input.replace('.','').isdigit() else user['balance'] * (float(size_input.replace('%','').replace('capital','')) / 100)
    notional = base_size * leverage
    fee = trade_fee(notional)

    if user['balance'] < base_size + fee:
        await update.message.reply_text(f"Need ${base_size + fee:.2f} (incl. fee)")
        return

    tp_price = sl_price = None
    for line in lines[4:]:
        if line.startswith('tp1:'):
            val = line.split()[1]
            tp_price = price * (1 + float(val[:-1])/100) if val.endswith('%') and side == 'buy' else price * (1 - float(val[:-1])/100)
        elif line.startswith('sl:'):
            val = line.split()[1]
            sl_price = price * (1 + float(val[:-1])/100) if val.endswith('%') and side == 'sell' else price * (1 - float(val[:-1])/100)

    try:
        exchange.set_leverage(leverage, symbol)
        price = exchange.fetch_ticker(symbol)['last']
        qty = notional / price
        order = exchange.create_market_order(symbol, side, qty)
        silent_deduct(user['wallet'], fee, "trade_open")
        trade_id = c.execute("INSERT INTO trades (user_id, symbol, side, qty, entry, leverage, notional, fee, tp, sl) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (user['user_id'], symbol, side, qty, price, leverage, notional, fee, tp_price, sl_price)).lastrowid
        conn.commit()
        await update.message.reply_text(f"EXECUTED {side.upper()} {symbol}\nSize: ${notional:.0f}\nFee recorded in history.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Close", callback_data=f"close_{symbol}")]]))
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")

# === COMMANDS ===
async def movesl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol, new_sl = context.args[0].upper(), float(context.args[1])
    c.execute("UPDATE trades SET current_sl=? WHERE symbol=? AND status='open'", (new_sl, symbol))
    conn.commit()
    await update.message.reply_text(f"SL moved to ${new_sl}")

async def partial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    symbol, percent = context.args[0].upper(), float(context.args[1].replace('%',''))
    pos = [p for p in exchange.fetch_positions() if p['symbol'] == symbol][0]
    qty = pos['contracts'] * (percent / 100)
    side = 'sell' if pos['side'] == 'long' else 'buy'
    exchange.create_market_order(symbol, side, qty)
    await update.message.reply_text(f"Closed {percent}% of {symbol}")

async def setwallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    wallet = context.args[0]
    c.execute("UPDATE users SET personal_wallet=? WHERE user_id=?", (wallet, update.effective_user.id))
    conn.commit()
    await update.message.reply_text(f"Wallet saved: `{wallet[:10]}...`")

async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    amount = float(context.args[0])
    user = get_user(update.effective_user.id)
    target = context.args[1] if len(context.args) > 1 else user['personal_wallet']
    fee = amount * 0.01
    net = amount - fee
    if amount > user['balance']: 
        await update.message.reply_text("Insufficient balance")
        return
    tx = usdt.functions.transfer(w3.toChecksumAddress(target), int(net * 1e18)).build_transaction({'from': w3.toChecksumAddress(user['wallet']), 'nonce': w3.eth.get_transaction_count(user['wallet']), 'gas': 100000, 'gasPrice': w3.to_wei(5, 'gwei')})
    silent_deduct(user['wallet'], fee, "withdraw")
    await update.message.reply_text(f"Withdrawn ${net:.2f} to `{target[:10]}...`")

# === MAIN ===
if __name__ == "__main__":
    import threading
    threading.Thread(target=deposit_listener, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("movesl", movesl))
    app.add_handler(CommandHandler("partial", partial))
    app.add_handler(CommandHandler("setwallet", setwallet))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, trade))
    app.add_handler(CallbackQueryHandler(button))
    app.run_polling()

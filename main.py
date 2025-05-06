# Установка библиотек
import os

os.system(
    "pip install ccxt python-telegram-bot==13.15 ta pandas gspread oauth2client pytz"
)
import json
import ccxt
import pandas as pd
import ta
import asyncio
from datetime import datetime, timedelta
import pytz
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# === ТВОИ КЛЮЧИ ===
api_key = 'HFX5v0cdHrcekfqu3D'
api_secret = 'ROSAhTFzm3ANP9A4VckhePFcs7iRb0bT8ssi'
telegram_token = '7919604234:AAFk2N4tasSUcPDdq2RwKyoWWBs8pEzuF2w'
chat_id = 1793147576  # замени на свой Telegram chat_id
# === ПОДКЛЮЧЕНИЕ К BYBIT ===
exchange = ccxt.bybit({
    'apiKey': api_key,
    'secret': api_secret,
    'enableRateLimit': True,
})
# === НАСТРОЙКИ ===
moscow_tz = pytz.timezone("Europe/Moscow")
symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'DOGE/USDT']
TP_PCT = 0.03
SL_PCT = 0.015
TRAILING_STOP_PCT = 0.015
VOLUME_FILTER_RATIO = 1.3
BOLLINGER_WIDTH_THRESHOLD = 0.01
MAX_TRADES = 5
TRADE_WINDOW = timedelta(hours=6)

open_trades = {}
signal_log = []
TRADING_ENABLED = True


# === GOOGLE SHEETS ===
def connect_sheet():
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(os.environ['GOOGLE_CREDENTIALS']), scope)
    client = gspread.authorize(creds)
    return client.open("Лог сделок").sheet1


def log_trade(symbol, entry, exit_price, result_pct, outcome):
    sheet = connect_sheet()
    now = datetime.now(moscow_tz).strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([
        now, symbol,
        round(entry, 2),
        round(exit_price, 2), f"{result_pct:.2%}", outcome
    ])


def send_daily_report():
    sheet = connect_sheet()
    today = datetime.now(moscow_tz).strftime("%Y-%m-%d")
    df = pd.DataFrame(sheet.get_all_records())
    df_today = df[df['Дата'].str.startswith(today)]
    if df_today.empty:
        return "Сегодня сделок не было."
    df_today['%'] = df_today['Результат'].str.replace('%', '').astype(float)
    total_profit = df_today[df_today['%'] > 0]['%'].sum()
    total_loss = df_today[df_today['%'] < 0]['%'].sum()
    net = total_profit + total_loss
    summary = f"📅 Итоги дня ({today}):\n\n"
    summary += f"Сделок: {len(df_today)}\nПрибыль: +{total_profit:.2f}%\n" if total_profit > 0 else ""
    summary += f"Убыток: {total_loss:.2f}%\n" if total_loss < 0 else ""
    summary += f"Net: {net:+.2f}%\n\n"
    for _, row in df_today.iterrows():
        summary += f"{row['Монета']}: {row['Результат']}\n"
    return summary


def send_log(update, context):
    sheet = connect_sheet()
    df = pd.DataFrame(sheet.get_all_records())
    if df.empty:
        update.callback_query.message.reply_text("Лог пуст.")
        return
    df.to_csv("log.csv", index=False)
    with open("log.csv", "rb") as f:
        context.bot.send_document(chat_id=update.effective_chat.id,
                                  document=InputFile(f),
                                  filename="log.csv")


# === TELEGRAM UI ===
def menu_markup():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("▶️ Запустить", callback_data='start')],
         [InlineKeyboardButton("⏸ Остановить", callback_data='stop')],
         [InlineKeyboardButton("📊 Статус", callback_data='status')],
         [InlineKeyboardButton("🧨 Закрыть все сделки", callback_data='panic')],
         [InlineKeyboardButton("📥 Лог сделок", callback_data='log')]])


def start(update, context):
    update.message.reply_text("Привет! Я готов к торговле.",
                              reply_markup=menu_markup())


def handle_message(update, context):
    update.message.reply_text("Меню:", reply_markup=menu_markup())


def button(update, context):
    global TRADING_ENABLED
    query = update.callback_query
    query.answer()
    data = query.data
    if data == 'start':
        TRADING_ENABLED = True
        query.edit_message_text("Бот запущен")
    elif data == 'stop':
        TRADING_ENABLED = False
        query.edit_message_text("Бот остановлен")
    elif data == 'status':
        now = datetime.now(moscow_tz)
        recent = len([s for s in signal_log if now - s < TRADE_WINDOW])
        query.edit_message_text(
            f"Бот {'работает' if TRADING_ENABLED else 'остановлен'}\nОткрытых сделок: {len(open_trades)}\nСделок за 6ч: {recent} из {MAX_TRADES}"
        )
    elif data == 'panic':
        open_trades.clear()
        query.edit_message_text("Все сделки закрыты (симуляция)")
    elif data == 'log':
        send_log(update, context)


# === АВТОТОРГОВЛЯ ===
async def auto_trading():
    bot = Updater(token=telegram_token, use_context=True).bot
    daily_sent = False
    while True:
        try:
            now = datetime.now(moscow_tz)

            if now.hour == 23 and now.minute == 59 and not daily_sent:
                report = send_daily_report()
                bot.send_message(chat_id=chat_id, text=report)
                daily_sent = True
            elif now.hour == 0 and now.minute == 0:
                daily_sent = False

            if not TRADING_ENABLED:
                await asyncio.sleep(60)
                continue

            signal_log[:] = [s for s in signal_log if now - s < TRADE_WINDOW]
            if len(signal_log) >= MAX_TRADES:
                await asyncio.sleep(300)
                continue

            for symbol in symbols:
                candles = exchange.fetch_ohlcv(symbol, '15m', limit=50)
                df = pd.DataFrame(candles,
                                  columns=[
                                      'timestamp', 'open', 'high', 'low',
                                      'close', 'volume'
                                  ])
                df['EMA10'] = df['close'].ewm(span=10).mean()
                df['EMA50'] = df['close'].ewm(span=50).mean()
                df['RSI'] = ta.momentum.RSIIndicator(df['close'],
                                                     window=14).rsi()
                bb = ta.volatility.BollingerBands(df['close'], window=20)
                df['bbh'] = bb.bollinger_hband()
                df['bbl'] = bb.bollinger_lband()

                last = df.iloc[-1]
                price = last['close']
                volume = last['volume']
                avg_volume = df['volume'].rolling(20).mean().iloc[-1]
                bb_width = (last['bbh'] - last['bbl']) / price

                if (last['EMA10'] > last['EMA50'] and last['RSI'] < 40
                        and volume > avg_volume * VOLUME_FILTER_RATIO
                        and bb_width > BOLLINGER_WIDTH_THRESHOLD
                        and symbol not in open_trades):

                    tp = price * (1 + TP_PCT)
                    sl = price * (1 - SL_PCT)
                    open_trades[symbol] = {
                        'entry_price': price,
                        'tp': tp,
                        'sl': sl,
                        'max_price': price,
                        'entry_time': now
                    }
                    signal_log.append(now)
                    bot.send_message(
                        chat_id=chat_id,
                        text=
                        (f"🚀 {symbol}\nВход: {price:.2f}\nTP: {tp:.2f}, SL: {sl:.2f}\nОбъём: {volume:.1f} (ср: {avg_volume:.1f})\nBB ширина: {bb_width:.4f}"
                         ))

                if symbol in open_trades:
                    current_price = exchange.fetch_ticker(symbol)['last']
                    trade = open_trades[symbol]

                    if current_price > trade['max_price']:
                        trade['max_price'] = current_price
                        new_sl = current_price * (1 - TRAILING_STOP_PCT)
                        if new_sl > trade['sl']:
                            trade['sl'] = new_sl

                    if current_price >= trade['tp']:
                        bot.send_message(
                            chat_id=chat_id,
                            text=f"✅ {symbol} достиг TP! {current_price:.2f}")
                        log_trade(symbol, trade['entry_price'], current_price,
                                  TP_PCT, "TP")
                        del open_trades[symbol]
                    elif current_price <= trade['sl']:
                        pnl = (current_price -
                               trade['entry_price']) / trade['entry_price']
                        bot.send_message(
                            chat_id=chat_id,
                            text=
                            f"🔻 {symbol} достиг SL (вкл. трейлинг): {current_price:.2f}"
                        )
                        log_trade(symbol, trade['entry_price'], current_price,
                                  pnl, "SL/Trail")
                        del open_trades[symbol]
        except Exception as e:
            print(f"Ошибка: {e}")
        await asyncio.sleep(60)


# === ЗАПУСК ===
updater = Updater(token=telegram_token, use_context=True)
dp = updater.dispatcher
dp.add_handler(CommandHandler("start", start))
dp.add_handler(CallbackQueryHandler(button))
dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
updater.start_polling()

asyncio.run(auto_trading())

from telegram.ext import Application
import asyncio

TOKEN = "b300e468030e6e1ab229e1d8ccdf299068856852"

async def main():
    application = Application.builder().token(TOKEN).build()
    await application.initialize()
    await application.start()
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=5000,
        url_path=TOKEN,
        webhook_url=f"https://yourusername.pythonanywhere.com/{TOKEN}"
    )
    await application.bot.set_webhook(f"https://yourusername.pythonanywhere.com/{TOKEN}")

# Flask ilova
from flask import Flask, request
app = Flask(__name__)

@app.route('/' + TOKEN, methods=['POST'])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    asyncio.run(application.process_update(update))
    return 'ok'

if __name__ == '__main__':
    asyncio.run(main())
    app.run()
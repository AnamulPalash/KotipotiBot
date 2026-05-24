"""
main.py — KotipotiBot v2 entry point
Starts Hermes background thread then runs the main bot loop.
"""
import db
import hermes
import bot

if __name__ == "__main__":
    db.init_db()
    hermes.start_background_thread()
    bot.run()

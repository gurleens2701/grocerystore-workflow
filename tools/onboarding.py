"""
tools/onboarding.py

First-time user onboarding flow for the Telegram bot.

Asks 4 questions:
  1. Name
  2. Preferred language
  3. Back-office access (NRS Plus / manual)
  4. Bank connection (yes/no)

Profile is stored in PostgreSQL via db/state.py under key "user_profile".
Once complete, "onboarding" key is set to "complete".

Usage in bot.py:
    from tools.onboarding import (
        ONBOARDING_STEP_NAME, ONBOARDING_STEP_LANG,
        ONBOARDING_STEP_BACKOFFICE, ONBOARDING_STEP_BANK,
        onboarding_start, onboarding_name, onboarding_language,
        onboarding_backoffice, onboarding_bank,
        is_onboarding_complete, get_user_profile,
    )
"""

import logging
from datetime import date

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes, ConversationHandler

from db.state import get_state, save_state
from config.settings import settings

log = logging.getLogger(__name__)

# ConversationHandler state constants
ONBOARDING_STEP_NAME       = 200
ONBOARDING_STEP_LANG       = 201
ONBOARDING_STEP_BACKOFFICE = 202
ONBOARDING_STEP_BANK       = 203

LANGUAGE_OPTIONS = [
    ["English", "Hindi", "Gujarati"],
    ["Punjabi", "Spanish", "Arabic"],
    ["Urdu", "Bengali", "Auto-detect"],
]

LANGUAGE_MAP = {
    "english":     "en",
    "hindi":       "hi",
    "gujarati":    "gu",
    "punjabi":     "pa",
    "spanish":     "es",
    "arabic":      "ar",
    "urdu":        "ur",
    "bengali":     "bn",
    "auto-detect": "auto",
    "auto":        "auto",
}

BACKOFFICE_OPTIONS = [["NRS Plus (auto)", "Manual daily"]]
BANK_OPTIONS       = [["Yes, connect bank", "No, skip for now"]]


async def is_onboarding_complete(store_id: str) -> bool:
    state = await get_state(store_id, "onboarding")
    return bool(state and state.get("status") == "complete")


async def get_user_profile(store_id: str) -> dict:
    profile = await get_state(store_id, "user_profile")
    return profile or {}


async def onboarding_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — send welcome + ask for name."""
    await update.message.reply_text(
        "👋 Hey! Welcome — I'm your store assistant.\n\n"
        "I'll help you track daily sales, invoices, expenses, and more. "
        "Let me ask you a few quick questions to get set up.\n\n"
        "What's your name?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ONBOARDING_STEP_NAME


async def onboarding_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save name → ask language."""
    name = update.message.text.strip().split()[0].capitalize()
    context.user_data["onboarding_name"] = name

    await update.message.reply_text(
        f"Nice to meet you, {name}! 👋\n\n"
        "What language do you prefer to text in?\n"
        "I'll always reply in the same language you use.",
        reply_markup=ReplyKeyboardMarkup(
            LANGUAGE_OPTIONS, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return ONBOARDING_STEP_LANG


async def onboarding_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save language → ask back-office."""
    lang_raw = update.message.text.strip().lower()
    lang_code = LANGUAGE_MAP.get(lang_raw, "auto")
    context.user_data["onboarding_lang"] = lang_code

    await update.message.reply_text(
        "Got it! Now, how do you want to log your daily sales?\n\n"
        "🔗 *NRS Plus (auto)* — I connect to your back-office and pull numbers automatically every morning.\n\n"
        "📸 *Manual daily* — Each morning I'll ask you to send a photo of your daily report. "
        "I'll read the numbers from the photo and fill in the sheet for you.\n\n"
        "Which fits your setup?",
        reply_markup=ReplyKeyboardMarkup(
            BACKOFFICE_OPTIONS, one_time_keyboard=True, resize_keyboard=True
        ),
        parse_mode="Markdown",
    )
    return ONBOARDING_STEP_BACKOFFICE


async def onboarding_backoffice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save backoffice pref → ask bank."""
    answer = update.message.text.strip().lower()
    if "nrs" in answer or "auto" in answer or "backoffice" in answer:
        backoffice = "nrs_plus"
    else:
        backoffice = "manual"
    context.user_data["onboarding_backoffice"] = backoffice

    await update.message.reply_text(
        "Last one — would you like to connect your bank account?\n\n"
        "This lets me automatically match deposits and flag any discrepancies. "
        "Totally optional — you can add it later anytime.",
        reply_markup=ReplyKeyboardMarkup(
            BANK_OPTIONS, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return ONBOARDING_STEP_BANK


async def onboarding_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save bank pref → finalize profile → done."""
    answer = update.message.text.strip().lower()
    bank_linked = "yes" in answer or answer == "y"

    store_id  = settings.store_id
    name      = context.user_data.get("onboarding_name", "there")
    lang      = context.user_data.get("onboarding_lang", "auto")
    backoffice = context.user_data.get("onboarding_backoffice", "nrs_plus")

    profile = {
        "name":        name,
        "language":    lang,
        "backoffice":  backoffice,
        "bank_linked": bank_linked,
        "setup_date":  str(date.today()),
    }
    await save_state(store_id, "user_profile", profile)
    await save_state(store_id, "onboarding",   {"status": "complete"})

    backoffice_note = (
        "I'll pull your daily sales automatically each morning at 7 AM."
        if backoffice == "nrs_plus"
        else "Every morning I'll ask you for a photo of your daily report — I'll read all the numbers for you."
    )
    bank_note = (
        "Bank connection noted — type /bank anytime to set it up."
        if bank_linked
        else "No problem — type /bank anytime to connect later."
    )

    await update.message.reply_text(
        f"You're all set, {name}! 🚀\n\n"
        f"Here's what I can do for you:\n"
        f"• Log invoices, expenses, and rebates via chat\n"
        f"• Send voice messages — I'll understand and reply\n"
        f"• Photo your vendor invoices — I'll extract all prices\n"
        f"• Track over/short, payroll, and expenses on your dashboard\n"
        f"• Alert you to unusual patterns\n\n"
        f"{backoffice_note}\n"
        f"{bank_note}\n\n"
        f"Type /help to see all commands. Let's go!",
        reply_markup=ReplyKeyboardRemove(),
    )

    log.info("Onboarding complete for store %s — name=%s lang=%s backoffice=%s bank=%s",
             store_id, name, lang, backoffice, bank_linked)

    return ConversationHandler.END

"""
tools/onboarding.py

First-time user onboarding flow for the Telegram bot.

Asks 3 questions (back-office is set by admin during provisioning):
  1. Name
  2. Preferred language
  3. Bank connection (yes → dashboard instructions, no → skip)

Profile is stored in PostgreSQL via db/state.py under key "user_profile".
Once complete, "onboarding" key is set to "complete".
"""

import logging
from datetime import date

from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import ContextTypes, ConversationHandler

from db.state import get_state, save_state
from config.settings import settings
from config.store_context import get_active_store

log = logging.getLogger(__name__)

# ConversationHandler state constants
ONBOARDING_STEP_NAME       = 200
ONBOARDING_STEP_LANG       = 201
ONBOARDING_STEP_BACKOFFICE = 202  # kept for backwards compat — no longer used
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

BANK_OPTIONS = [["Yes, connect bank", "No, skip for now"]]


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
    """Save language → ask bank (skip backoffice — set by admin during provisioning)."""
    lang_raw = update.message.text.strip().lower()
    lang_code = LANGUAGE_MAP.get(lang_raw, "auto")
    context.user_data["onboarding_lang"] = lang_code

    await update.message.reply_text(
        "Last one — would you like to connect your bank account?\n\n"
        "This lets me automatically match your deposits, detect when vendor invoices are paid, "
        "and flag any CC settlement mismatches. Totally optional — you can add it later anytime.",
        reply_markup=ReplyKeyboardMarkup(
            BANK_OPTIONS, one_time_keyboard=True, resize_keyboard=True
        ),
    )
    return ONBOARDING_STEP_BANK


async def onboarding_backoffice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Legacy handler — no longer shown, routes straight to bank step."""
    return await onboarding_language(update, context)


async def onboarding_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save bank pref → finalize profile → done."""
    answer = update.message.text.strip().lower()
    bank_linked = "yes" in answer or answer == "y"

    store_id  = get_active_store()
    name      = context.user_data.get("onboarding_name", "there")
    lang      = context.user_data.get("onboarding_lang", "auto")

    # Backoffice is set by admin during provisioning — read from settings
    backoffice = "nrs_plus" if settings.nrs_username else "manual"

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
        "✅ I'll pull your daily sales automatically each morning at 7 AM."
        if backoffice == "nrs_plus"
        else "📸 Every morning I'll remind you to send your daily report — just send me a photo and I'll read all the numbers."
    )

    if bank_linked:
        bank_note = (
            "🏦 Great! Here's how to connect your bank:\n\n"
            "1. Go to your dashboard (link was sent to you when you signed up)\n"
            "2. Sign in with your username and password\n"
            "3. Click *Bank Account* in the sidebar\n"
            "4. Click *Connect Bank Account* — it's read-only, we can never move money\n"
            "5. Log in to your bank through the secure popup\n\n"
            "Takes about 2 minutes. Message me if you need help!"
        )
    else:
        bank_note = "No problem — type /bank anytime to connect your bank later."

    await update.message.reply_text(
        f"You're all set, {name}! 🚀\n\n"
        f"Here's what I can do for you:\n"
        f"• Log invoices, expenses, and rebates via chat\n"
        f"• Send voice messages — I'll understand and reply\n"
        f"• Photo your vendor invoices — I'll extract all prices\n"
        f"• Track over/short, payroll, and expenses on your dashboard\n"
        f"• Alert you to unusual patterns\n\n"
        f"{backoffice_note}\n\n"
        f"{bank_note}\n\n"
        f"Type /help to see all commands. Let's go!",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown",
    )

    log.info("Onboarding complete for store %s — name=%s lang=%s backoffice=%s bank=%s",
             store_id, name, lang, backoffice, bank_linked)

    return ConversationHandler.END

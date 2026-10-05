import os, io, re, difflib, tempfile
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from openai import OpenAI

ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
BOT_TOKEN = os.environ["BOT_TOKEN"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

client = OpenAI(api_key=OPENAI_API_KEY)

# Demo corpus: Al-Fatiha. Replace/extend this with a trusted Quran API/database.
SURAHS = {
    1: {
        "name": "الفاتحة",
        "ayahs": {
            1: "بسم الله الرحمن الرحيم",
            2: "الحمد لله رب العالمين",
            3: "الرحمن الرحيم",
            4: "مالك يوم الدين",
            5: "إياك نعبد وإياك نستعين",
            6: "اهدنا الصراط المستقيم",
            7: "صراط الذين أنعمت عليهم غير المغضوب عليهم ولا الضالين",
        },
    }
}

def normalize_arabic(s: str) -> str:
    s = re.sub(r"[\u064B-\u065F\u0670]", "", s)
    s = s.replace("أ","ا").replace("إ","ا").replace("آ","ا")
    s = s.replace("ى","ي").replace("ة","ه")
    s = re.sub(r"[^\u0621-\u063A\u0641-\u064A\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def compare_text(reference: str, spoken: str):
    ref = normalize_arabic(reference)
    sp = normalize_arabic(spoken)
    ref_words, sp_words = ref.split(), sp.split()
    sm = difflib.SequenceMatcher(a=ref_words, b=sp_words)
    errors = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            expected = " ".join(ref_words[i1:i2])
            heard = " ".join(sp_words[j1:j2])
            errors.append({"type": tag, "expected": expected, "heard": heard})
    ratio = sm.ratio()
    return ratio, errors

async def ensure_user(update: Update) -> bool:
    user = update.effective_user
    if not user or user.id != ALLOWED_USER_ID:
        if update.message:
            await update.message.reply_text("🔒 هذا البوت خاص ومتاح للحساب المصرح له فقط.")
        return False
    return True

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return
    keyboard = [[InlineKeyboardButton("📖 اختر السورة", callback_data="surahs")]]
    await update.message.reply_text(
        "أهلًا بكِ في مُسمِّع القرآن 🤍\n\n"
        "هذه نسخة أولى لتجربة تسميع القرآن ومقارنة التلاوة بالنص.",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def surahs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    keyboard = [
        [InlineKeyboardButton(f"📖 {data['name']}", callback_data=f"surah:{sid}")]
        for sid, data in SURAHS.items()
    ]
    await q.edit_message_text("اختاري السورة:", reply_markup=InlineKeyboardMarkup(keyboard))

async def choose_ayah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    sid = int(q.data.split(":")[1])
    context.user_data["surah"] = sid
    keyboard = [
        [InlineKeyboardButton(f"الآية {n}", callback_data=f"ayah:{n}")]
        for n in SURAHS[sid]["ayahs"]
    ]
    await q.edit_message_text(
        f"سورة {SURAHS[sid]['name']}\nاختاري الآية التي تريدين تسميعها:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def start_ayah(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    n = int(q.data.split(":")[1])
    sid = context.user_data["surah"]
    context.user_data["ayah"] = n
    await q.edit_message_text(
        f"🎙️ الآية {n}\n\n"
        "اقرئي الآية من حفظك وأرسلي التسجيل الصوتي هنا.\n"
        "لن أعرض النص قبل التسميع."
    )

async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return

    sid = context.user_data.get("surah")
    n = context.user_data.get("ayah")
    if not sid or not n:
        await update.message.reply_text("اختاري السورة والآية أولًا من /start.")
        return

    voice_file = await update.message.voice.get_file()
    data = await voice_file.download_as_bytearray()

    # Telegram voice messages are normally OGG/Opus.
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
        f.write(data)
        audio_path = f.name

    try:
        with open(audio_path, "rb") as audio:
            transcript = client.audio.transcriptions.create(
                model="gpt-4o-transcribe",
                file=audio,
                language="ar",
            )

        spoken = transcript.text.strip()
        reference = SURAHS[sid]["ayahs"][n]
        ratio, errors = compare_text(reference, spoken)

        if ratio >= 0.95:
            verdict = "✅ ممتاز، التلاوة مطابقة بدرجة عالية."
        elif ratio >= 0.80:
            verdict = "🟡 جيد، لكن يوجد بعض الاختلافات."
        else:
            verdict = "🔴 توجد أخطاء تحتاج إلى مراجعة."

        lines = [
            verdict,
            f"\n📊 درجة المطابقة التقريبية: {round(ratio * 100)}%",
            f"\n🗣️ ما تم التعرف عليه:\n{spoken}",
        ]

        if errors:
            lines.append("\n🔎 مواضع الاختلاف:")
            for e in errors:
                if e["type"] == "replace":
                    lines.append(f"• قلتِ: «{e['heard']}» — المتوقع: «{e['expected']}»")
                elif e["type"] == "delete":
                    lines.append(f"• يبدو أنه تم إسقاط: «{e['expected']}»")
                elif e["type"] == "insert":
                    lines.append(f"• كلمة زائدة محتملة: «{e['heard']}»")

        lines.append(
            "\n⚠️ هذه النسخة تصحح مطابقة الكلمات قدر الإمكان، "
            "وليست حكمًا متخصصًا على أحكام التجويد ومخارج الحروف."
        )
        keyboard = [[
            InlineKeyboardButton("🔁 أعد الآية", callback_data=f"ayah:{n}"),
            InlineKeyboardButton("📖 اختر آية أخرى", callback_data="surahs")
        ]]
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
    finally:
        try:
            os.remove(audio_path)
        except OSError:
            pass

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_user(update):
        return
    await update.message.reply_text(
        "/start — بدء التسميع\n"
        "/help — المساعدة\n\n"
        "أرسلي Voice بعد اختيار الآية."
    )

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(surahs, pattern=r"^surahs$"))
    app.add_handler(CallbackQueryHandler(choose_ayah, pattern=r"^surah:\d+$"))
    app.add_handler(CallbackQueryHandler(start_ayah, pattern=r"^ayah:\d+$"))
    app.add_handler(MessageHandler(filters.VOICE, voice))
    print("Quran Tasmee Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()

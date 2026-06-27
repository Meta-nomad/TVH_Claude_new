import logging

from aiogram import Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message

from services.analyzer import analyze, ExchangeResult

log = logging.getLogger(__name__)
router = Router()

START_TEXT = (
    "👋 <b>TradingView History Bot</b>\n\n"
    "Отправьте тикер — найду биржу с <b>самой длинной</b> историей цены.\n"
    "При одинаковой глубине выбираю биржу с <b>наиболее непрерывным</b> часовым графиком.\n\n"
    "Примеры:\n"
    "<code>BTC</code>  <code>ETH</code>  <code>UNI</code>  <code>SOL</code>  <code>ZEC</code>"
)


def _gap_emoji(r: ExchangeResult) -> str:
    if not r.gap_scored:
        return ""
    if r.gap_ratio < 0.01:
        return " 🟢"
    if r.gap_ratio < 0.05:
        return " 🟡"
    if r.gap_ratio < 0.20:
        return " 🟠"
    return " 🔴"


def _gap_label(r: ExchangeResult) -> str:
    if not r.gap_scored:
        return ""
    if r.gap_ratio < 0.01:
        return "🟢 Без разрывов"
    if r.gap_ratio < 0.05:
        return "🟡 Редкие разрывы"
    if r.gap_ratio < 0.20:
        return "🟠 Заметные разрывы"
    return "🔴 Много разрывов"


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(START_TEXT, parse_mode="HTML")


@router.message(F.text)
async def handle_ticker(message: Message) -> None:
    raw = message.text.strip()
    if not raw or len(raw) > 20 or " " in raw or raw.startswith("/"):
        await message.answer("Введите тикер, например: <code>BTC</code>", parse_mode="HTML")
        return

    ticker = raw.upper()
    wait = await message.answer(f"🔍 Ищу историю для <b>{ticker}</b>...", parse_mode="HTML")

    try:
        result = await analyze(ticker)
    except Exception as e:
        log.exception("analyze(%s) crashed: %s", ticker, e)
        await wait.edit_text("⚠️ Ошибка при запросе данных. Попробуйте позже.")
        return

    if result is None:
        await wait.edit_text(
            f"❌ Монета <b>{ticker}</b> не найдена. Проверьте тикер.",
            parse_mode="HTML",
        )
        return

    b = result.best
    date_str = b.start_date.strftime("%Y-%m-%d")
    gap_lbl  = _gap_label(b)

    lines = [
        f"📊 <b>Монета:</b> {result.ticker}",
        "",
        f"✅ <b>Символ TradingView:</b>",
        f"<code>{b.symbol}</code>",
        "",
        f"🏦 <b>Биржа:</b> {b.tv_prefix}",
        f"📅 <b>История с:</b> {date_str}",
    ]
    if gap_lbl:
        lines.append(f"📈 <b>Часовой график:</b> {gap_lbl}")
    lines += ["", f"🔗 {b.url}"]

    if result.alternatives:
        lines.append("")
        lines.append("📋 <b>Альтернативы:</b>")
        for alt in result.alternatives:
            lines.append(f"• <code>{alt.symbol}</code>{_gap_emoji(alt)}")

    await wait.edit_text("\n".join(lines), parse_mode="HTML", disable_web_page_preview=True)
